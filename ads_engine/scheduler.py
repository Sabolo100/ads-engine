"""Ütemező: napi szinkron (06:30), heti kör (hétfő 07:00), óránkénti nyitóoldal-őr – pótlással, újrapróbálással és zárral.

Idempotens: minden feladatnak (projekt, fajta, időszak) egyetlen „kész” bejegyzése van a store.jobs táblában; újraindítás után csak a
hiányzót pótolja (a napi szinkront aznap, a heti kört a hét végéig). Hibára 30 perc múlva újrapróbál (legfeljebb 3×) és levelet küld.
A heti kör csak akkor indul, ha aznap a napi szinkron (a védelmek) lefutott. A `tick()` egyszerre egy példányon fut (zár a /data-n:
a Coolify frissítéskor rövid ideig két konténer is élhet, de csak az egyik dolgozhat).
"""
import dataclasses
import datetime as dt
import os
import signal
import socket
import threading
import time
from zoneinfo import ZoneInfo

from . import alerts, http, log, mailer, net, reports, review, runtime, sync, web
from .google import discovery
from .store import LeaseBusy

SYNC_AT = dt.time(6, 30)
WEEKLY_AT = dt.time(7, 0)               # hétfőn
RETRY_AFTER = dt.timedelta(minutes=30)
MAX_ATTEMPTS = 3
LEASE_TTL = 1800
APICHECK_AT = dt.time(6, 0)             # havonta egyszer, a hónap első ticken
LABEL = {"sync": "napi szinkron", "weekly": "heti kiértékelés", "guard": "nyitóoldal-őr", "apicheck": "havi API-ellenőrzés"}


@dataclasses.dataclass
class Deps:
    """A feladatok külső függőségei (a próbák ezeket cserélik)."""
    client: object = None
    llm: object = None
    umami: object = None
    fetch: object = net.fetch
    fetch_kw: dict = dataclasses.field(default_factory=dict)
    send: object = None              # mailer.send felülírása
    sleep: object = None


def default_deps(settings, store):
    return Deps(client=runtime.google_client(settings), llm=runtime.llm(settings, store), umami=runtime.umami(settings), fetch_kw=runtime.fetch_kwargs(settings))


def iso_week(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def local_now(project):
    return dt.datetime.now(ZoneInfo(project.timezone))


# ------------------------------------------------------------------ mi esedékes?
def due(project, store, now):
    """A projekt esedékes feladatai sorrendben: [(fajta, időszak)]. Csak olyan projekt, aminek van ügyfélfiókja."""
    slug, today = project.slug, now.date()
    out = []
    if not project.customer_id:
        return out

    def pending(kind, period):
        j = store.job(slug, kind, period)
        if not j or j["status"] == "running":
            return True                                # még nem futott, vagy megszakadt futás (a zár miatt nem futhat mellette másik): újra
        if j["status"] == "ok":
            return False
        if int(store.get(f"{slug}.attempts.{kind}.{period}", 0) or 0) >= MAX_ATTEMPTS:
            return False
        return now - dt.datetime.fromisoformat(j["updated_at"]) >= RETRY_AFTER

    sync_period = today.isoformat()
    sync_ok = (store.job(slug, "sync", sync_period) or {}).get("status") == "ok"
    if now.time() >= SYNC_AT and pending("sync", sync_period):
        out.append(("sync", sync_period))
    monday = today - dt.timedelta(days=today.weekday())
    weekly_start = dt.datetime.combine(monday, WEEKLY_AT, tzinfo=now.tzinfo)
    if now >= weekly_start and pending("weekly", iso_week(today)):
        if sync_ok or ("sync", sync_period) in out:    # a védelmek előbb: a heti kör a szinkron után fut (ugyanebben a tickben is)
            out.append(("weekly", iso_week(today)))
    if store.get(f"{slug}.approved_daily_micros") is not None:
        period = f"{today.isoformat()}T{now.hour:02d}"
        if pending("guard", period):
            out.append(("guard", period))
    month = today.strftime("%Y-%m")
    if now.time() >= APICHECK_AT and pending("apicheck", month):
        out.append(("apicheck", month))
    return out


# ------------------------------------------------------------------ feladatok
def heartbeat(settings):
    """Opcionális életjel egy külső figyelőnek (HEARTBEAT_URL, pl. healthchecks.io): a napi szinkron után. Az URL-t soha nem naplózzuk."""
    url = settings.env.get("HEARTBEAT_URL")
    if not url:
        return
    try:
        http.request("GET", url, timeout=10, retries=1)
    except http.HttpError as e:
        log.warn("heartbeat.failed", status=e.status)


def job_sync(settings, store, project, run_id, now, deps):
    res = sync.run_sync(settings, project, store, deps.client, run_id, now=now, fetch=deps.fetch, send=deps.send, sleep=deps.sleep, **deps.fetch_kw)
    try:
        watch = review.pack_watch(settings, project, store, deps.client, run_id, now=now, fetch=deps.fetch, send=deps.send, **deps.fetch_kw)["status"]
    except Exception as e:                                 # a brief-figyelő hibája ne vigye el a szinkront; de legyen látható
        watch = f"hiba: {str(e)[:200]}"
        alerts.notify(settings, store, project, "job_failed:pack", "A brief-figyelő hibára futott", [str(e)[:500]], dedupe_days=1, send=deps.send or mailer.send)
    heartbeat(settings)
    return {"campaigns": len(res.campaigns), "findings": [f.code for f in res.findings], "paused": len(res.paused), "human_changes": len(res.human_changes),
            "disapproved": len(res.disapproved), "pack": watch}


def job_weekly(settings, store, project, run_id, now, deps):
    slug = project.slug
    send = deps.send or mailer.send
    pending = store.get(f"{slug}.pending_report")
    if pending and pending.get("week") == iso_week(now.date()):          # az előző próbálkozáskor a levél nem ment el: ugyanazt küldjük újra
        report, narrative = reports.load_saved(pending["path"])
        summary = {"resent": True}
    else:
        res = review.run_weekly(settings, project, store, deps.client, run_id, llm=deps.llm, umami_client=deps.umami, now=now, fetch=deps.fetch, **deps.fetch_kw)
        if res.skipped == "no_campaigns":
            return {"skipped": "no_campaigns"}
        report = res.report
        narrative = reports.make_narrative(deps.llm, slug, report)
        summary = {"actions": len(report["actions"]), "rejected": len(report["rejected"])}
    out = reports.deliver(settings, project, store, report, narrative, send=send)
    if not out["mailed"]:
        store.put(f"{slug}.pending_report", {"week": iso_week(now.date()), "path": out["path"]})
        raise RuntimeError(f"a heti jelentés elkészült ({out['path']}), de a levél nem ment el: {out['mail_error']}")
    store.delete(f"{slug}.pending_report")
    store.put(f"{slug}.notices", [])                                      # a jelentésben megjelentek: nem ismétlődnek
    return {**summary, "report": out["path"], "mailed": True}


def job_guard(settings, store, project, run_id, now, deps):
    r = sync.run_landing_guard(settings, project, store, deps.client, run_id, fetch=deps.fetch, send=deps.send, sleep=deps.sleep, **deps.fetch_kw)
    return {"ok": r["ok"], "action": r["action"]}


def job_apicheck(settings, store, project, run_id, now, deps):
    """Havi: a Google Ads API verziója él-e még, közeleg-e a lejárat, van-e újabb főverzió, változott-e a leíró. A hálózati hiba nem hiba."""
    res = discovery.api_check(settings.api_version, refresh=True)
    if res["notes"]:
        alerts.notify(settings, store, project, "api_check", "A Google Ads API-ról figyelmeztetés van",
                      [f"Ellenőrzött verzió: {res['version']}", ""] + [f"  • {n}" for n in res["notes"]] +
                      ["", "A motor a beállított verzióval dolgozik tovább; a verzióváltás a GADS_API_VERSION változóval és egy új leíróval (python -m ads_engine api-check) történik."],
                      dedupe_days=25, send=deps.send or mailer.send)
    return {"ok": res["ok"], "notes": res["notes"]}


JOBS = {"sync": job_sync, "weekly": job_weekly, "guard": job_guard, "apicheck": job_apicheck}


def run_job(settings, store, project, kind, period, now, deps):
    slug = project.slug
    run_id = store.start_run(slug, kind, settings.mode)
    key = f"{slug}.attempts.{kind}.{period}"
    store.put(key, int(store.get(key, 0) or 0) + 1)
    store.job_mark(slug, kind, period, "running", run_id)
    try:
        summary = JOBS[kind](settings, store, project, run_id, now, deps)
    except Exception as e:                                  # egy feladat hibája nem állítja le az ütemezőt; levél + újrapróba
        store.job_mark(slug, kind, period, "failed", run_id)
        store.finish_run(run_id, "failed", {"error": str(e)[:500]})
        log.error("job.failed", project=slug, kind=kind, period=period, error=str(e)[:300])
        attempts = int(store.get(key, 0) or 0)
        final = attempts >= MAX_ATTEMPTS                    # az utolsó próbálkozás hibájáról külön levél megy (az ismétlődés-védelem ne nyelje le)
        alerts.notify(settings, store, project, f"job_failed_final:{kind}:{period}" if final else f"job_failed:{kind}", f"A(z) {LABEL[kind]} hibára futott",
                      [f"A(z) {LABEL[kind]} feladat ({period}) nem sikerült ({attempts}. próba / {MAX_ATTEMPTS}):", "", str(e)[:800], "",
                       "A motor 30 perc múlva újrapróbálja." if attempts < MAX_ATTEMPTS else "A motor több próbát nem tesz erre az időszakra: nézd meg a naplót."],
                      dedupe_days=1, send=deps.send or mailer.send)
        return {"kind": kind, "period": period, "status": "failed", "error": str(e)[:300]}
    store.job_mark(slug, kind, period, "ok", run_id)
    store.finish_run(run_id, "ok", summary)
    return {"kind": kind, "period": period, "status": "ok", "summary": summary}


def tick(settings, store, *, now=None, deps=None, owner=None):
    """Egy ütemezői lépés: az összes projekt esedékes feladata, zár alatt. Visszaadja a lefutott feladatokat.
    LeaseBusy: egy másik példány dolgozik (a hívó kihagyja a lépést). A függőségek csak akkor jönnek létre, ha van esedékes feladat."""
    owner = owner or f"{socket.gethostname()}-{os.getpid()}"
    done = []
    with store.lease("engine", owner, ttl=LEASE_TTL):
        for project in settings.projects.values():
            n = now or local_now(project)
            jobs = due(project, store, n)
            if not jobs:
                continue
            deps = deps or default_deps(settings, store)
            if deps.client is None:
                log.warn("tick.no_google_key", project=project.slug)
                continue
            for kind, period in jobs:
                store.acquire("engine", owner, LEASE_TTL)           # a zár megújítása hosszú feladatok között
                if kind == "weekly" and (store.job(project.slug, "sync", n.date().isoformat()) or {}).get("status") != "ok":
                    log.warn("tick.weekly_waits_for_sync", project=project.slug)   # védelmek nélkül nem módosítunk: előbb a napi szinkron kell
                    continue
                done.append(run_job(settings, store, project, kind, period, n, deps))
    return done


# ------------------------------------------------------------------ szolgáltatás
def serve(settings, *, port=None, tick_seconds=60, stop_event=None, deps=None, install_signals=True, now_fn=None):
    """A konténer fő ciklusa: /healthz + percenként egy tick. SIGTERM/SIGINT: szép leállás (a futó feladat befejeződik)."""
    store = runtime.open_store(settings)
    state = web.HealthState()
    server = web.start(state, store, settings, port=port if port is not None else int(settings.env.get("PORT") or 8080))
    stop = stop_event or threading.Event()
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):
                pass
    deps = deps or default_deps(settings, store)
    log.info("serve.start", mode=settings.mode, projects=list(settings.projects), port=server.server_address[1])
    try:
        while not stop.is_set():
            error = ""
            try:
                tick(settings, store, deps=deps, now=now_fn() if now_fn else None)
            except LeaseBusy as e:
                log.info("tick.lease_busy", holder=str(e))
            except Exception as e:                           # a ciklus nem állhat le egy váratlan hiba miatt
                error = str(e)[:300]
                log.error("tick.failed", error=error)
            state.touch(error)
            stop.wait(tick_seconds)
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        log.info("serve.stop")
    return 0
