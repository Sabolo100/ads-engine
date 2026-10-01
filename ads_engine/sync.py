"""Napi szinkron + védelmek. NEM optimalizál: csak megfigyel és szükség esetén FÉKEZ (kampányt szüneteltet), majd értesít.

Mit néz: a motor kampányainak napi keretét és költését (a Google napi kerete átlag: védelmek a guardrails.py-ban), a hirdetések
jóváhagyási állapotát, azt, hogy ember (vagy automatikus szabály/ajánlás) nyúlt-e a kampányokhoz, és hogy a nyitóoldal elérhető-e.
A fékek dry üzemmódban is élesek (csak csökkenthetnek költést). Minden levél ismétlés ellen védett (alerts.notify).
"""
import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

from . import alerts, guardrails as g, net, reportdata
from .executor import Executor, enable_campaign_ops


@dataclasses.dataclass
class SyncResult:
    campaigns: list = dataclasses.field(default_factory=list)
    configured_daily: int = 0
    approved_daily: int = None
    cost_yesterday: int = 0
    cost_30d: int = 0
    findings: list = dataclasses.field(default_factory=list)
    paused: list = dataclasses.field(default_factory=list)
    human_changes: list = dataclasses.field(default_factory=list)
    disapproved: list = dataclasses.field(default_factory=list)
    google_added: list = dataclasses.field(default_factory=list)
    notes: list = dataclasses.field(default_factory=list)


def now_in(project):
    return dt.datetime.now(ZoneInfo(project.timezone))


def budget_history(store, slug, today, configured_daily):
    """A napi keret naponkénti előzménye (a 30 napi összeghatárhoz): [[dátum, micros], …], legfeljebb 60 nap."""
    hist = [h for h in (store.get(f"{slug}.budget_history", []) or []) if h[0] != today.isoformat()]
    hist.append([today.isoformat(), int(configured_daily)])
    hist = sorted(hist)[-60:]
    store.put(f"{slug}.budget_history", hist)
    return hist


def budget_30d_total(hist, today, fallback):
    """Az elmúlt 30 nap (a tegnapig) napi kereteinek összege; ahol nincs rögzítés, a jóváhagyott/tartalék érték."""
    by = {d: v for d, v in hist}
    total = 0
    for i in range(1, 31):
        d = (today - dt.timedelta(days=i)).isoformat()
        total += by.get(d, fallback)
    return total


def landing_urls(project, store):
    urls = [project.site.rstrip("/") + "/"]
    brief = store.get(f"{project.slug}.cache.brief") if store else None
    for p in (brief or {}).get("landing_pages", [])[:8]:
        if p["url"] not in urls:
            urls.append(p["url"])
    return urls


def landing_check(project, store, fetch=net.fetch, attempts=2, sleep=None, **fetch_kw):
    """Elérhető-e a nyitóoldal (és megmarad-e rajta az UTM)? (ok, problémák). Egy átmeneti hibára egy újrapróba."""
    import time
    sleep = sleep or time.sleep
    problems = []
    for url in landing_urls(project, store):
        probe = url + ("&" if "?" in url else "?") + "utm_source=google&utm_medium=cpc&utm_campaign=guard"
        last = None
        for a in range(attempts):
            try:
                r = fetch(probe, project.allowed_hosts, max_bytes=4096, truncate=True, timeout=15, **fetch_kw)       # csak az állapot és a végső cím kell
                last = None
                if "utm_source" not in r.url:
                    problems.append(f"{url}: az átirányítás elveszíti az UTM-paramétereket (a hirdetések követése megszakad)")
                break
            except net.FetchError as e:
                last = str(e)
                if a + 1 < attempts:
                    sleep(5)
        if last:
            problems.append(f"{url}: nem érhető el ({last})")
    hard = [p for p in problems if "nem érhető el" in p]
    return (not hard), problems


def run_landing_guard(settings, project, store, client, run_id, *, fetch=net.fetch, send=None, sleep=None, **fetch_kw):
    """Óránkénti őr: ha a nyitóoldal két egymás utáni ellenőrzésnél nem elérhető, a motor kampányai szünetelnek (és visszaállnak, ha
    az oldal két ellenőrzésen át rendben van). Visszaadja: {"ok", "problems", "action"}."""
    slug = project.slug
    ok, problems = landing_check(project, store, fetch=fetch, sleep=sleep, **fetch_kw)
    fails = int(store.get(f"{slug}.landing_fail", 0) or 0)
    ex = Executor(client, store, settings, project, run_id)
    action = None
    if ok:
        oks = int(store.get(f"{slug}.landing_ok", 0) or 0) + 1
        store.put(f"{slug}.landing_fail", 0)
        store.put(f"{slug}.landing_ok", oks)
        held = store.get(f"{slug}.paused_by_landing") or []
        if held and oks >= 2 and not store.get(f"{slug}.guard_pause"):
            ex.apply("landing_resume", enable_campaign_ops(held), target=",".join(h.rsplit("/", 1)[-1] for h in held),
                     reason="a nyitóoldal újra elérhető", before={"status": "PAUSED"}, after={"status": "ENABLED"})
            if settings.live:
                store.put(f"{slug}.paused_by_landing", [])
                action = "resumed"
    else:
        store.put(f"{slug}.landing_ok", 0)
        fails += 1
        store.put(f"{slug}.landing_fail", fails)
        if fails >= 2:
            enabled = [c["resource_name"] for c in reportdata.owned_campaigns(client, project.customer_id) if c["status"] == "ENABLED"]
            if enabled:
                ex.pause_campaigns(enabled, "a nyitóoldal nem elérhető – a hirdetés felesleges költséget termelne")
                store.put(f"{slug}.paused_by_landing", enabled)
                action = "paused"
                alerts.notify(settings, store, project, "landing_down", "A nyitóoldal nem elérhető: a hirdetések szünetelnek",
                              ["A motor két egymás utáni ellenőrzésnél sem érte el a nyitóoldalt, ezért szüneteltette a kampányokat.", ""] + problems +
                              ["", "Az oldal helyreállása után a motor magától újraindítja őket."], dedupe_days=1, **({"send": send} if send else {}))
    return {"ok": ok, "problems": problems, "action": action}


def detect_human_changes(client, project, store, campaigns, now, sa_email):
    """Ember (vagy automatikus szabály/ajánlás) általi módosítás a motor kampányain: a pillanatkép és a Google változás-eseményei alapján."""
    slug = project.slug
    cur = reportdata.snapshot(client, project.customer_id, campaigns)
    prev = store.snapshot_get(slug, "live")
    stale = bool(store.get(f"{slug}.snapshot_stale", False))
    changes = [] if (prev is None or stale) else g.diff_snapshots(prev["data"], cur)
    store.snapshot_put(slug, "live", cur)
    store.put(f"{slug}.snapshot_stale", False)
    since = store.get(f"{slug}.last_sync_ts")
    since_dt = dt.datetime.fromisoformat(since) if since else now - dt.timedelta(days=2)
    fmt = lambda t: t.astimezone(ZoneInfo(project.timezone)).strftime("%Y-%m-%d %H:%M:%S")
    own = {c["resource_name"] for c in campaigns} | {c["budget_rn"] for c in campaigns if c.get("budget_rn")}
    events = []
    try:
        events = g.human_change_events(reportdata.change_event_rows(client, project.customer_id, fmt(since_dt), fmt(now)), own, sa_email)
    except Exception:           # a változás-esemény nem kritikus (korlátozott hozzáférés, késés): a pillanatkép-különbség így is jelez
        pass
    seen, out = set(), []
    for c in changes:
        key = (c["resource"], c["field"])
        seen.add(key)
        out.append({"resource": c["resource"], "what": f"{c['field']}: {c['before']} → {c['after']}", "who": "ismeretlen (a pillanatképből)", "when": ""})
    for e in events:
        if (e["resource"], "*") in seen:
            continue
        out.append({"resource": e["resource"], "what": e.get("fields") or "módosítás", "who": e["who"], "when": e["when"]})
    hands = store.get(f"{slug}.hands_off", {}) or {}
    until = (now.date() + dt.timedelta(days=28)).isoformat()
    for o in out:
        hands[o["resource"]] = until
    store.put(f"{slug}.hands_off", {k: v for k, v in hands.items() if v >= now.date().isoformat()})
    return out


def run_sync(settings, project, store, client, run_id, *, today=None, now=None, fetch=net.fetch, send=None, sleep=None, **fetch_kw):
    """A napi szinkron és védelmek. Visszaad egy SyncResult-ot; a fékeket végre is hajtja."""
    slug, cid = project.slug, project.customer_id
    now = now or now_in(project)
    today = today or now.date()
    notify = (lambda *a, **k: alerts.notify(settings, store, project, *a, **({"send": send} if send else {}), **k))
    res = SyncResult()
    campaigns = reportdata.owned_campaigns(client, cid)
    res.campaigns = campaigns
    if not campaigns:
        res.notes.append("Még nincs motor-kampány (a launch parancs hozza létre).")
        store.put(f"{slug}.last_sync", {"date": today.isoformat(), "campaigns": 0})
        store.put(f"{slug}.last_sync_ts", now.isoformat(timespec="seconds"))
        return res
    ids = [c["id"] for c in campaigns]
    enabled = [c for c in campaigns if c["status"] == "ENABLED"]
    res.configured_daily = sum(c["daily_micros"] for c in campaigns)
    enabled_daily = sum(c["daily_micros"] for c in enabled)
    approved = store.get(f"{slug}.approved_daily_micros")
    res.approved_daily = approved

    daily = reportdata.daily_cost(client, cid, ids, today - dt.timedelta(days=30), today - dt.timedelta(days=1))
    res.cost_yesterday = daily.get((today - dt.timedelta(days=1)).isoformat(), {}).get("cost_micros", 0)
    res.cost_30d = sum(v["cost_micros"] for v in daily.values())
    hist = budget_history(store, slug, today, res.configured_daily)
    total_30 = budget_30d_total(hist, today, fallback=approved or res.configured_daily)
    live_for_check = res.configured_daily if approved is not None else enabled_daily
    res.findings = g.check_budget(approved_daily=approved, live_daily=live_for_check, cost_yesterday=res.cost_yesterday, cost_30d=res.cost_30d,
                                  budget_30d_total=total_30 if approved is not None else None, currency=project.currency)
    new_approved = g.next_approved(approved, live_for_check, res.findings)
    if approved is not None and new_approved != approved:
        store.put(f"{slug}.approved_daily_micros", new_approved)
        res.approved_daily = new_approved

    notices = store.get(f"{slug}.notices", []) or []
    for f in res.findings:
        if f.level == "info":
            notices.append({"ts": now.isoformat(timespec="seconds"), "text": f.message})
    store.put(f"{slug}.notices", notices[-50:])

    ex = Executor(client, store, settings, project, run_id)
    pause = [f for f in res.findings if f.level == "pause"]
    if pause and enabled:
        ex.pause_campaigns([c["resource_name"] for c in enabled], "; ".join(f.message for f in pause)[:500])
        res.paused = [c["resource_name"] for c in enabled]
        store.put(f"{slug}.guard_pause", {"ts": now.isoformat(timespec="seconds"), "codes": [f.code for f in pause], "campaigns": res.paused})
        if any(f.code == "budget_confirm" for f in pause):
            store.put(f"{slug}.needs_budget_confirmation", {"configured_daily": res.configured_daily, "approved": approved})
        for f in pause:
            notify(f"guard:{f.code}", f"FÉK: {f.message[:70]}",
                   [f.message, "", "A motor szüneteltette a kampányokat. Visszakapcsolás: ellenőrizd a keretet a Google Ads-ben, majd:",
                    "  python -m ads_engine confirm-budget --yes --enable   (ha a keret szándékos)"], dedupe_days=1)
    elif pause:
        res.notes.append("Fék-feltétel áll fenn, de nincs bekapcsolt motor-kampány: " + "; ".join(f.message for f in pause))
        for f in pause:
            notify(f"guard:{f.code}", f"Figyelmeztetés: {f.message[:70]}", [f.message], dedupe_days=1)
    elif store.get(f"{slug}.guard_pause") and not pause:
        store.delete(f"{slug}.guard_pause")

    # hirdetések jóváhagyási állapota
    ads = reportdata.ad_rows(client, cid, ids, today - dt.timedelta(days=1), today - dt.timedelta(days=1))
    res.disapproved = [a for a in ads if a["approval"] == "DISAPPROVED"]
    res.google_added = [a for a in ads if a["added_by_google"]]
    if res.disapproved:
        notify("disapproved", f"A Google elutasított {len(res.disapproved)} hirdetést",
               ["A Google nem hagyta jóvá az alábbi hirdetéseket (az azonosítók a Google Ads-ben megkereshetők):"] +
               [f"  hirdetés {a['ad_id']} (hirdetéscsoport {a['ad_group_id']})" for a in res.disapproved] +
               ["", "A motor a heti körben legfeljebb kétszer próbálja javítani, utána szünetelteti és szól."], dedupe_days=3)
    if res.google_added:
        res.notes.append(f"A Google {len(res.google_added)} hirdetést magától adott hozzá (automatikus ajánlás): kapcsold ki az automatikus alkalmazást.")

    # ember általi módosítás (a saját fékünk után friss állapotból, hogy a saját szünetet ne vegyük emberi módosításnak)
    if res.paused:
        campaigns = reportdata.owned_campaigns(client, cid)
    sa_email = getattr(getattr(client.tokens, "sa", None), "client_email", "")
    res.human_changes = detect_human_changes(client, project, store, campaigns, now, sa_email)
    if res.human_changes:
        notify("human_changes", "Kézzel módosítottál a kampányokon: a motor ezekhez 28 napig nem nyúl",
               ["A motor észlelte, hogy a következő objektumokat nem ő módosította:"] +
               [f"  {c['resource'].rsplit('/', 2)[-2]}/{c['resource'].rsplit('/', 1)[-1]}: {c['what']} ({c['who']})" for c in res.human_changes[:20]] +
               ["", "Ezeket nem írja felül (28 napig 'kézben vannak'); a motor a többi objektumon dolgozik tovább."], dedupe_days=1)

    # nyitóoldal
    lg = run_landing_guard(settings, project, store, client, run_id, fetch=fetch, send=send, sleep=sleep, **fetch_kw)
    if lg["problems"]:
        res.notes += [f"Nyitóoldal: {p}" for p in lg["problems"]]

    store.put(f"{slug}.last_sync", {"date": today.isoformat(), "campaigns": len(campaigns), "paused": len(res.paused), "findings": [f.code for f in res.findings]})
    store.put(f"{slug}.last_sync_ts", now.isoformat(timespec="seconds"))
    return res
