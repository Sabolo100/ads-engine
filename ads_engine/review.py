"""Heti kiértékelés és módosítás: Google + Umami adatok → javaslatok → KÓDBAN lévő korlátok → végrehajtás → napló.

Zárt műveletkészlet (minden más kizárt): negatív kulcsszó hozzáadása · kulcsszó szüneteltetése · RSA-csere (új RSA, a régi
szüneteltetése) · elutasított hirdetés javítása vagy szüneteltetése · a brief változása miatt szabálysértővé vált hirdetés/kulcsszó
szüneteltetése. Az AI csak javasol és magyaráz; a döntést a guardrails.py szabályai szűrik (elég adat, tények, védett
magkifejezések, heti korlátok, „kézben lévő” objektumok). Az éles indulás utáni első 14 napban csak megfigyelés van.
Az AI vagy az Umami hibája soha nem akadályozza a jelentést: a motor ilyenkor kevesebbet módosít, de szól, miért.
"""
import dataclasses
import datetime as dt
from typing import List, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from . import alerts, copywriter, guardrails as g, llm as llmmod, net, pack as packmod, packcheck, reportdata, umami as umamimod, validators
from .executor import Executor, WriteRefused
from .google.client import GoogleAdsError
from .sync import now_in

MIN_LOW_ASSETS = 3             # ennyi „LOW” szöveg alatt nem cserélünk RSA-t (a csere a hirdetés tanulását nullázza)
FIX_WINDOW_DAYS = 60           # elutasított hirdetés javítási kísérletei ennyi napon belül számítanak
MAX_FIX_ATTEMPTS = 2


# ------------------------------------------------------------------ AI-sémák
class TermJudgement(BaseModel):
    term: str = Field(max_length=200)
    verdict: Literal["relevant", "irrelevant", "unsure"]
    reason: str = Field(default="", max_length=240)
    negative: str = Field(default="", max_length=80, description="A legszűkebb negatív kulcsszó (1–3 szó); üres, ha nem kell.")


class TermReview(BaseModel):
    judgements: List[TermJudgement] = Field(default_factory=list, max_length=100)
    summary: str = Field(default="", max_length=800)


@dataclasses.dataclass
class Action:
    kind: str                 # add_negative | pause_keyword | rotate_rsa | fix_disapproved | pause_ad | pack_pause_ad | pack_pause_keyword
    target: str
    reason: str
    status: str = "proposed"  # proposed | applied | validated | rejected | failed
    detail: dict = dataclasses.field(default_factory=dict)
    rejected_because: list = dataclasses.field(default_factory=list)

    def reject(self, *because):
        self.status = "rejected"
        self.rejected_because = list(because)
        return self


@dataclasses.dataclass
class ReviewResult:
    report: dict
    actions: list
    skipped: str = ""


# ------------------------------------------------------------------ segédek
def week_windows(today, tz):
    """Az előző teljes hét (hétfő–vasárnap) és az azelőtti; Umami-ezredmásodpercek az előzőre."""
    s_ms, e_ms, start, end = umamimod.week_bounds(today, tz)
    return {"start": start, "end": end, "prev_start": start - dt.timedelta(days=7), "prev_end": start - dt.timedelta(days=1),
            "start_ms": s_ms, "end_ms": e_ms}


def applied_this_week(store, slug, kind, today):
    """Hány ilyen műveletet hajtottunk végre (élesben) ebben a naptári hétben: a heti korlátok a több futás között is érvényesek."""
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    return len([a for a in store.actions(project=slug, kind=kind, since=monday, limit=500) if a["status"] == "applied"])


def active_hands_off(store, slug, today):
    return {rn for rn, until in (store.get(f"{slug}.hands_off", {}) or {}).items() if until >= today.isoformat()}


def get_brief(project, store, fetch=net.fetch, **fetch_kw):
    """(brief, megjegyzés): az élő brief; ha az oldal nem érhető el, az utolsó ÉRVÉNYES brief; hibás briefnél None. A kreatívok
    érvényessége itt nem számít (az új hirdetések forrása, nem a szabályoké)."""
    slug = project.slug
    try:
        brief, _warns, _changed = packmod.load_brief(project, store, fetch=fetch, **fetch_kw)
        return brief, None
    except packmod.PackError as e:
        if e.kind == "fetch":
            valid = store.get(f"{slug}.valid.brief")
            if valid:
                return valid, f"Az Ads Pack most nem tölthető le ({e.problems[0]}): az utolsó érvényes briefet használom."
        return None, f"Az Ads Pack nem használható: {'; '.join(e.problems[:3])}. Módosítást ezen a héten nem végzek."


def web_stats(umami_client, project, windows, brief):
    """A hirdetésekből jövő látogatások az Umamiból (a hét): összesítés + UTM szerinti bontás. None: nincs Umami; {"error": True}: hiba."""
    if umami_client is None or not project.umami_website_id:
        return None
    tr = (brief or {}).get("tracking", {}) or {}
    wid, s, e = project.umami_website_id, windows["start_ms"], windows["end_ms"]
    try:
        visits = umami_client.events(wid, s, e, event=tr.get("visit_event") or "inditas")
        engaged = [r for ev in (tr.get("engaged_events") or ["bevont"]) for r in umami_client.events(wid, s, e, event=ev)]
        keys = [r for ev in (tr.get("key_events") or []) for r in umami_client.events(wid, s, e, event=ev)]
    except umamimod.UmamiError:
        return {"error": True}
    return umamimod.aggregate_ads(visits, engaged, keys, campaign_prefix=project.slug)


# ------------------------------------------------------------------ 1) keresési kifejezések → negatív kulcsszavak
def judge_terms(llm, slug, brief, rows, existing_negatives):
    system = (f"Te a(z) {brief['project']['name']} Google Ads kampányának keresésikifejezés-elemzője vagy. Minden megadott keresési "
              "kifejezésről döntsd el, hogy a termékhez illő szándékot fejez-e ki (relevant), egyértelműen más szándékot (irrelevant: pl. kutya "
              "vásárlása, kutyás szolgáltatás vagy termék, más téma), vagy bizonytalan (unsure).\n"
              "- Irrelevant esetén javasolj negatív kulcsszót: a LEGSZŰKEBB 1–3 szavas kifejezés, amely az irreleváns szándékot megfogja, és "
              "szó szerint benne van a keresési kifejezésben. Soha ne javasolj általános szót, és olyat sem, ami a termékhez illő keresést is "
              "kizárna.\n- Ha bizonytalan vagy, ne javasolj negatívot.\n- Minden megadott kifejezést add vissza.\n\n" + llmmod.DATA_RULE +
              "\nCsak a kért JSON-t add vissza.")
    user = "\n\n".join([
        llmmod.wrap_data("termék", {"összefoglaló": brief["product"]["summary"], "ajánlat": brief["product"].get("offer", ""),
                                    "magkulcsszavak": brief.get("keywords", {}).get("core", []), "célcsoportok": brief["product"].get("audiences", [])}),
        llmmod.wrap_data("meglévő_negatívok", existing_negatives[:150]),
        llmmod.wrap_data("keresési_kifejezések", [{"kifejezés": r["term"], "kattintás": r["clicks"], "megjelenés": r["impressions"]} for r in rows]),
        "Értékeld az összes kifejezést."])
    return llm.ask(project=slug, purpose="terms", system=system, user=user, schema=TermReview, effort="medium", max_tokens=16000)


def propose_negatives(review, rows, brief, positives, existing, budget_left):
    """TermReview → Action-ök; az elutasítottak okokkal. A negatívot csak megfigyelt kifejezés alapján, szűrve fogadjuk el."""
    by_term = {validators.norm(r["term"]): r for r in rows}
    relevant = [validators.tokens_of(j.term) for j in review.judgements if j.verdict == "relevant"]
    out, seen, accepted = [], set(), 0
    for j in review.judgements:
        if j.verdict != "irrelevant" or not j.negative.strip():
            continue
        row = by_term.get(validators.norm(j.term))
        text = " ".join(j.negative.lower().split())
        a = Action("add_negative", text, f"keresési kifejezés: „{j.term}” – {j.reason}".strip(" –"),
                   detail={"text": text, "term": j.term, "campaign_id": row["campaign_id"] if row else "",
                           "clicks": row["clicks"] if row else 0, "cost_micros": row["cost_micros"] if row else 0})
        if row is None:
            out.append(a.reject("a kifejezés nem szerepel a riportban"))
            continue
        reasons = g.vet_negative(text, row["term"], brief, positives, existing, validators)
        nt = validators.tokens_of(text)
        if any(nt and any(rt[i:i + len(nt)] == nt for i in range(len(rt) - len(nt) + 1)) for rt in relevant):
            reasons.append("kizárna egy másik, releváns keresési kifejezést is")
        if validators.norm(text) in seen:
            reasons.append("ugyanezt a negatívot már javasoltuk ebben a körben")
        if not reasons and accepted >= budget_left:
            reasons.append(f"heti korlát ({g.MAX_NEGATIVES_PER_WEEK} negatív)")
        if reasons:
            out.append(a.reject(*reasons))
            continue
        seen.add(validators.norm(text))
        accepted += 1
        out.append(a)
    return out


# ------------------------------------------------------------------ 2) gyenge kulcsszavak szüneteltetése
def propose_pauses(kw_rows, status_rows, web_by_term, brief, hands_off, budget_left):
    """Szüneteltetés: csak elég kattintás után, és csak ha a webes adat bizonyítja, hogy a kulcsszó nem hoz bevont látogatást."""
    out, accepted = [], 0
    ranked = sorted([k for k in kw_rows if k["status"] == "ENABLED" and k["clicks"] >= g.MIN_CLICKS_PAUSE],
                    key=lambda r: (-r["cost_micros"], -r["clicks"]))
    for kw in ranked:
        web = web_by_term.get(validators.norm(kw["text"]))
        reasons = g.vet_keyword_pause(kw, status_rows, web, brief, validators, hands_off=hands_off)
        a = Action("pause_keyword", f"{kw['text']} [{kw['match']}]",
                   f"{kw['clicks']} kattintás, bevont látogatás nincs" + (f" ({web['visits']} látogatásból)" if web else ""),
                   detail={"resource_name": kw["resource_name"], "text": kw["text"], "clicks": kw["clicks"], "cost_micros": kw["cost_micros"],
                           "visits": (web or {}).get("visits", 0)})
        if not reasons and accepted >= budget_left:
            reasons.append(f"heti korlát ({g.MAX_PAUSES_PER_WEEK} szüneteltetés)")
        if reasons:
            out.append(a.reject(*reasons))
            continue
        accepted += 1
        out.append(a)
    return out


# ------------------------------------------------------------------ műveletek (egy atomi mutate mindegyik)
def neg_op(campaign_rn, text, match="PHRASE"):
    return [{"campaignCriterionOperation": {"create": {"campaign": campaign_rn, "negative": True, "keyword": {"text": text, "matchType": match}}}}]


def pause_keyword_ops(resource_name):
    return [{"adGroupCriterionOperation": {"update": {"resourceName": resource_name, "status": "PAUSED"}, "updateMask": "status"}}]


def pause_ad_ops(resource_name):
    return [{"adGroupAdOperation": {"update": {"resourceName": resource_name, "status": "PAUSED"}, "updateMask": "status"}}]


def new_rsa_op(ad_group_rn, final_urls, headlines, descriptions, path1, path2, pinned):
    rsa = {"headlines": [({"text": h, "pinnedField": pinned[h]} if h in pinned else {"text": h}) for h in headlines],
           "descriptions": [{"text": d} for d in descriptions]}
    for k, v in (("path1", path1), ("path2", path2)):
        if v:
            rsa[k] = v
    return {"adGroupAdOperation": {"create": {"adGroup": ad_group_rn, "status": "ENABLED", "ad": {"finalUrls": final_urls, "responsiveSearchAd": rsa}}}}


def rotate_rsa_ops(ad_group_rn, old_ad_rn, final_urls, headlines, descriptions, path1, path2, pinned):
    """Az RSA szövegei a Google-ben nem szerkeszthetők: új RSA jön, és a régi szünetel (egy atomi kérésben)."""
    return [new_rsa_op(ad_group_rn, final_urls, headlines, descriptions, path1, path2, pinned)] + pause_ad_ops(old_ad_rn)


# ------------------------------------------------------------------ 3) RSA-csere és elutasított hirdetések
def rewrite_rsa(llm, slug, brief, c, remove, kw_rows, performance):
    """Egy RSA új változata: a `remove` szövegek kihullnak, helyükre új, a validátorokon átment szöveg jön. (dict | None, hibák)"""
    rm = {validators.norm(t) for t in remove}
    keep_h = [h for h in c["headlines"] if validators.norm(h) not in rm or h in c["pinned"]]
    keep_d = [d for d in c["descriptions"] if validators.norm(d) not in rm]
    n_h, n_d = len(c["headlines"]) - len(keep_h), len(c["descriptions"]) - len(keep_d)
    if not (n_h or n_d):
        return None, ["nincs mit cserélni"]
    theme = next((k["ad_group"] for k in kw_rows if k["ad_group_id"] == c["ad_group_id"]), c["ad_group_id"])
    kws = [{"text": k["text"]} for k in kw_rows if k["ad_group_id"] == c["ad_group_id"] and k["status"] == "ENABLED"]
    url = (c["final_urls"] or [""])[0].split("?")[0].rstrip("/")
    landing = next((p["id"] for p in brief["landing_pages"] if p["url"].split("?")[0].rstrip("/") == url), brief["landing_pages"][0]["id"])
    adset = {"id": c["ad_group_id"], "theme": theme, "landing": landing, "keywords": kws, "headlines": c["headlines"], "descriptions": c["descriptions"]}
    try:
        res = copywriter.generate_copy(llm, slug, brief, adset, n_headlines=n_h, n_descriptions=n_d, performance=performance)
    except llmmod.LLMError as e:
        return None, [f"az AI nem adott szöveget: {e}"]
    if not (res.headlines or res.descriptions):
        return None, ["az AI egyik új szövege sem felelt meg a szabályoknak, ezért nem cserélek"] + [f"{t}: {'; '.join(r)}" for t, r in res.rejected[:3]]
    heads, descs = keep_h + res.headlines, keep_d + res.descriptions
    errs = validators.errors(validators.check_rsa(heads, descs, c["path1"], c["path2"], brief))
    if errs:
        return None, [e.message for e in errs[:4]] + [f"{t}: {'; '.join(r)}" for t, r in res.rejected[:2]]
    return {"headlines": heads, "descriptions": descs, "added": res.headlines + res.descriptions,
            "removed": [h for h in c["headlines"] if h not in keep_h] + [d for d in c["descriptions"] if d not in keep_d]}, []


def propose_rotations(contents, labels, kw_rows, brief, llm, slug, store, today, hands_off, budget_left):
    """Ahol a Google legalább MIN_LOW_ASSETS szöveget „LOW”-nak jelöl, ott új RSA készül (a LOW szövegek nélkül, új szövegekkel)."""
    low_by_ad, best_by_ad = {}, {}
    for L in labels:
        if L["label"] == "LOW":
            low_by_ad.setdefault(L["ad_id"], []).append(L)
        elif L["label"] == "BEST":
            best_by_ad.setdefault(L["ad_id"], []).append(L["text"])
    out, accepted = [], 0
    for c in contents:
        low = [x for x in low_by_ad.get(c["ad_id"], []) if x["text"] and x["text"] not in c["pinned"]]
        if c["status"] != "ENABLED" or len(low) < MIN_LOW_ASSETS:
            continue
        a = Action("rotate_rsa", f"hirdetés {c['ad_id']} (csoport {c['ad_group_id']})", f"a Google {len(low)} szöveget gyengének (LOW) jelzett",
                   detail={"ad_group_id": c["ad_group_id"], "ad_id": c["ad_id"]})
        last = store.get(f"{slug}.rsa_rotated.{c['ad_group_id']}")
        reasons = []
        if last and (today - dt.date.fromisoformat(last)).days < g.ROTATE_INTERVAL_DAYS:
            reasons.append(f"a legutóbbi csere {last}-án volt (legalább {g.ROTATE_INTERVAL_DAYS} nap kell két csere között)")
        if c["resource_name"] in hands_off:
            reasons.append("kézben van (ember módosította)")
        if llm is None:
            reasons.append("nincs AI-kulcs: új szöveget nem tudok írni")
        if not reasons and accepted >= budget_left:
            reasons.append(f"heti korlát ({g.MAX_ROTATIONS_PER_WEEK} csere)")
        if reasons:
            out.append(a.reject(*reasons))
            continue
        new, errs = rewrite_rsa(llm, slug, brief, c, [x["text"] for x in low], kw_rows,
                                {"gyenge_szövegek": [x["text"] for x in low], "legjobb_szövegek": best_by_ad.get(c["ad_id"], [])})
        if errs:
            out.append(a.reject(*errs))
            continue
        a.detail.update({**new, "path1": c["path1"], "path2": c["path2"], "final_urls": c["final_urls"], "pinned": c["pinned"], "old_rn": c["resource_name"]})
        accepted += 1
        out.append(a)
    return out


def propose_disapproved(ad_rows, details, contents, kw_rows, brief, llm, slug, store, today, hands_off, budget_left):
    """Elutasított hirdetés: szöveg okú elutasításnál új RSA a kifogásolt szövegek nélkül (60 napon belül legfeljebb 2 kísérlet
    hirdetéscsoportonként), utána a hirdetés szünetel. Nem szöveg okú (nyitóoldal, szabályzat) elutasítást nem tudunk kódból javítani: szólunk."""
    out, accepted = [], 0
    by_ad = {c["ad_id"]: c for c in contents}
    for r in ad_rows:
        c = by_ad.get(r["ad_id"])
        if r["approval"] != "DISAPPROVED" or r["status"] != "ENABLED" or not c:
            continue
        entries = details.get(r["ad_id"], [])
        texts = [t for e in entries for t in e["texts"]]
        topics = sorted({e["topic"] for e in entries if e.get("topic")})
        a = Action("fix_disapproved", f"hirdetés {r['ad_id']} (csoport {r['ad_group_id']})",
                   f"a Google elutasította ({', '.join(topics) or 'az ok ismeretlen'})", detail={"ad_group_id": r["ad_group_id"], "ad_id": r["ad_id"], "topics": topics})
        if c["resource_name"] in hands_off:
            out.append(a.reject("kézben van (ember módosította)"))
            continue
        key = f"{slug}.fix_attempts.{r['ad_group_id']}"
        recent = [d for d in (store.get(key, []) or []) if (today - dt.date.fromisoformat(d)).days <= FIX_WINDOW_DAYS]
        if len(recent) >= MAX_FIX_ATTEMPTS:
            a.kind, a.reason = "pause_ad", f"a Google elutasította ({', '.join(topics) or 'az ok ismeretlen'}), {len(recent)} javítás sem segített: szüneteltetem"
            a.detail["old_rn"] = c["resource_name"]
            out.append(a)
            continue
        if not texts:
            out.append(a.reject("az elutasítás oka nem szöveg (nyitóoldal vagy szabályzat): ezt kódból nem javítom, nézd meg a Google Ads-ben (Policy manager)"))
            continue
        if llm is None:
            out.append(a.reject("nincs AI-kulcs: új szöveget nem tudok írni"))
            continue
        if accepted >= budget_left:
            out.append(a.reject(f"heti korlát ({g.MAX_ROTATIONS_PER_WEEK} csere)"))
            continue
        new, errs = rewrite_rsa(llm, slug, brief, c, texts, kw_rows, {"elutasított_szövegek": texts, "szabály": topics})
        if errs:
            out.append(a.reject(*errs))
            continue
        a.detail.update({**new, "path1": c["path1"], "path2": c["path2"], "final_urls": c["final_urls"], "pinned": c["pinned"], "old_rn": c["resource_name"],
                         "attempt_key": key})
        accepted += 1
        out.append(a)
    return out


# ------------------------------------------------------------------ a heti kör
def _kw_view(r):
    return {k: r[k] for k in ("text", "match", "ad_group", "status", "clicks", "impressions", "cost_micros", "quality_score")}


def run_weekly(settings, project, store, client, run_id, *, llm=None, umami_client=None, today=None, now=None, fetch=net.fetch, **fetch_kw):
    """A heti kör. ReviewResult-ot ad (jelentés + műveletek); a levelet a hívó (scheduler/CLI) küldi."""
    slug, cid = project.slug, project.customer_id
    now = now or now_in(project)
    today = today or now.date()
    w = week_windows(today, ZoneInfo(project.timezone))
    approved = store.get(f"{slug}.approved_daily_micros")
    base = {"project": slug, "name": project.name, "currency": project.currency, "mode": settings.mode, "generated": now.isoformat(timespec="seconds"),
            "period": {"start": w["start"].isoformat(), "end": w["end"].isoformat()}, "approved_daily_micros": approved,
            "weekly_budget_micros": g.weekly_from_daily(approved) if approved else None,
            "actions": [], "rejected": [], "notes": [], "todo": [], "notices": list(store.get(f"{slug}.notices", []) or [])}
    campaigns = reportdata.owned_campaigns(client, cid)
    if not campaigns:
        base["notes"].append("Még nincs motor-kampány: a launch parancs hozza létre.")
        return ReviewResult(base, [], "no_campaigns")
    ids = [c["id"] for c in campaigns]
    base["campaigns"] = [{"name": c["name"], "status": c["status"], "daily_micros": c["daily_micros"]} for c in campaigns]

    # --- Google-adatok: ez a hét és az előző
    daily = reportdata.daily_cost(client, cid, ids, w["prev_start"], w["end"])
    this = reportdata.totals({d: v for d, v in daily.items() if w["start"].isoformat() <= d <= w["end"].isoformat()})
    prev = reportdata.totals({d: v for d, v in daily.items() if w["prev_start"].isoformat() <= d <= w["prev_end"].isoformat()})
    base["google"] = {"this": this, "prev": prev}
    if approved:
        base["budget"] = {"weekly_micros": g.weekly_from_daily(approved), "spent_micros": this["cost_micros"],
                          "used": this["cost_micros"] / g.weekly_from_daily(approved)}
    kw_rows = reportdata.keyword_rows(client, cid, ids, w["start"], w["end"])
    status_rows = reportdata.keyword_status_rows(client, cid, ids)
    term_rows = reportdata.term_rows(client, cid, ids, w["start"], w["end"])
    ad_rows = reportdata.ad_rows(client, cid, ids, w["start"], w["end"])
    group_names = {k["ad_group_id"]: k["ad_group"] for k in status_rows}

    # --- brief és Umami
    brief, brief_note = get_brief(project, store, fetch=fetch, **fetch_kw)
    if brief_note:
        base["notes"].append(brief_note)
        if brief is None:
            alerts.notify(settings, store, project, "pack_error", "Az Ads Pack hibás: a motor nem módosít", [brief_note], dedupe_days=3)
    web = web_stats(umami_client, project, w, brief)
    web_ok = bool(web) and not web.get("error")
    base["web"] = {"available": web_ok, "total": web["total"] if web_ok else None}
    if web is None:
        base["notes"].append("Az Umami nincs beállítva (UMAMI_URL, UMAMI_USER, UMAMI_PASSWORD, projekt umami_website_id): webes minőségmérés nincs, "
                             "kulcsszó-szüneteltetés sem.")
    elif not web_ok:
        base["notes"].append("Az Umami adatai most nem érhetők el: a webes minőségmérés kimaradt, kulcsszót ebben a körben nem szüneteltetek.")
    if web_ok and web["total"]["visits"]:
        t = web["total"]
        base["web"].update({"cost_per_visit_micros": this["cost_micros"] / t["visits"], "engaged_rate": t["engaged"] / t["visits"],
                            "cost_per_engaged_micros": (this["cost_micros"] / t["engaged"]) if t["engaged"] else None})
    by_content = web["by"]["content"] if web_ok else {}
    base["top_keywords"] = [_kw_view(r) for r in sorted(kw_rows, key=lambda r: (-r["clicks"], -r["impressions"])) if r["clicks"] or r["impressions"]][:8]
    base["top_terms"] = [{k: r[k] for k in ("term", "clicks", "impressions", "cost_micros")} for r in sorted(term_rows, key=lambda r: -r["clicks"])][:10]
    base["ads"] = [{"ad_id": a["ad_id"], "ad_group": group_names.get(a["ad_group_id"], a["ad_group_id"]), "status": a["status"], "approval": a["approval"],
                    "clicks": a["clicks"], "impressions": a["impressions"], "cost_micros": a["cost_micros"], "web": by_content.get(a["ad_id"])}
                   for a in sorted(ad_rows, key=lambda r: -r["clicks"])]

    # --- teendők, kézben lévő objektumok
    n_dis = len([a for a in ad_rows if a["approval"] == "DISAPPROVED"])
    if n_dis:
        base["todo"].append(f"{n_dis} hirdetést a Google elutasított: a szöveg okú elutasítást a motor javítja (legfeljebb kétszer), a többit kézzel kell megnézni.")
    if store.get(f"{slug}.needs_budget_confirmation"):
        base["todo"].append("A kampány a kereted gyanús megemelése miatt SZÜNETEL: ha szándékos, erősítsd meg (python -m ads_engine confirm-budget --yes --enable).")
    hands = active_hands_off(store, slug, today)
    if hands:
        base["notes"].append(f"{len(hands)} objektum „kézben van” (ember módosította): ezekhez {g.HANDS_OFF_DAYS} napig nem nyúlok.")

    # --- megfigyelési időszak és módosítások
    go_live = store.get(f"{slug}.go_live") or {}
    if approved is not None and not go_live.get("date"):
        go_live = {**go_live, "date": today.isoformat()}                          # régi rekord: a számolás a mai naptól indul (óvatos)
        store.put(f"{slug}.go_live", go_live)
    left = g.observation_days_left(dt.date.fromisoformat(go_live["date"]) if go_live.get("date") else None, today)
    base["observation"] = {"active": left > 0, "days_left": left}
    actions = []
    enabled = [c for c in campaigns if c["status"] == "ENABLED" and c["resource_name"] not in hands]
    if approved is None:
        base["notes"].append("Még nincs go-live: a kampányok szüneteltetve vannak, nincs mit optimalizálni.")
    elif brief is None:
        pass
    elif not enabled:
        base["notes"].append("Nincs bekapcsolt (és nem „kézben lévő”) motor-kampány: nincs mit módosítani.")
    else:
        # az elutasított hirdetések javítása karbantartás, ezért a megfigyelési időszakban is mehet
        actions += _maintain(project, store, client, ids, brief, ad_rows, kw_rows, llm, today, hands)
        if left > 0:
            base["notes"].append(f"Megfigyelési időszak: még {left} nap. Ez alatt csak a biztonsági fékek és az elutasított hirdetések javítása működik, "
                                 "a kulcsszavakhoz és a hirdetésszövegekhez nem nyúlok.")
        else:
            actions += _optimize(project, store, client, brief, enabled, ids, kw_rows, status_rows, term_rows, web, web_ok, llm, today, hands)
    _execute(settings, project, store, client, run_id, actions, enabled, today)
    base["actions"] = [action_dict(a) for a in actions if a.status in ("applied", "validated", "failed")]
    base["rejected"] = [action_dict(a) for a in actions if a.status == "rejected"]
    return ReviewResult(base, actions)


def action_dict(a):
    keep = ("text", "term", "clicks", "cost_micros", "visits", "removed", "added", "topics")
    return {"kind": a.kind, "target": a.target, "reason": a.reason, "status": a.status, "because": a.rejected_because,
            "detail": {k: v for k, v in a.detail.items() if k in keep}}


def _maintain(project, store, client, ids, brief, ad_rows, kw_rows, llm, today, hands):
    """Karbantartás: elutasított hirdetések (új RSA vagy szünet)."""
    cid = project.customer_id
    if not [a for a in ad_rows if a["approval"] == "DISAPPROVED"]:
        return []
    contents = reportdata.ad_content_rows(client, cid, ids)
    details = reportdata.disapproval_details(client, cid, ids)
    left = max(0, g.MAX_ROTATIONS_PER_WEEK - applied_this_week(store, project.slug, "fix_disapproved", today))
    return propose_disapproved(ad_rows, details, contents, kw_rows, brief, llm, project.slug, store, today, hands, left)


def _optimize(project, store, client, brief, enabled, ids, kw_rows, status_rows, term_rows, web, web_ok, llm, today, hands):
    slug, cid = project.slug, project.customer_id
    rn_by_id = {c["id"]: c["resource_name"] for c in enabled}
    kw_rows = [dict(r, campaign_rn=rn_by_id[r["campaign_id"]]) for r in kw_rows if r["campaign_id"] in rn_by_id]
    term_rows = [r for r in term_rows if r["campaign_id"] in rn_by_id]
    actions = []
    # 1) negatív kulcsszavak a keresési kifejezésekből (AI-javaslat, szűrve)
    left = max(0, g.MAX_NEGATIVES_PER_WEEK - applied_this_week(store, slug, "add_negative", today))
    cand = sorted([r for r in term_rows if r["clicks"] or r["impressions"] >= 3], key=lambda r: (-r["cost_micros"], -r["clicks"], -r["impressions"]))[:60]
    if cand and llm is not None:
        existing = [n["text"] for n in reportdata.negative_rows(client, cid, ids)]
        try:
            review = judge_terms(llm, slug, brief, cand, existing)
            actions += propose_negatives(review, cand, brief, [k["text"] for k in status_rows], existing, left)
        except llmmod.LLMError as e:
            actions.append(Action("add_negative", "(kimaradt)", "az AI-elemzés nem sikerült").reject(str(e)))
    elif cand:
        actions.append(Action("add_negative", "(kimaradt)", "nincs AI-kulcs").reject("ANTHROPIC_API_KEY nélkül nem elemzem a keresési kifejezéseket"))
    # 2) gyenge kulcsszavak szüneteltetése (csak megbízható webes adattal)
    if web_ok:
        web_terms = {validators.norm(k): v for k, v in web["by"]["term"].items()}
        left = max(0, g.MAX_PAUSES_PER_WEEK - applied_this_week(store, slug, "pause_keyword", today))
        actions += propose_pauses(kw_rows, status_rows, web_terms, brief, hands, left)
    # 3) RSA-csere (a Google LOW címkéi alapján)
    labels = reportdata.asset_label_rows(client, cid, ids)
    if any(L["label"] == "LOW" for L in labels):
        contents = reportdata.ad_content_rows(client, cid, ids)
        left = max(0, g.MAX_ROTATIONS_PER_WEEK - applied_this_week(store, slug, "rotate_rsa", today))
        actions += propose_rotations(contents, labels, kw_rows, brief, llm, slug, store, today, hands, left)
    return actions


def _execute(settings, project, store, client, run_id, actions, enabled, today):
    """Az elfogadott javaslatok végrehajtása: mindegyik külön atomi kérés (validateOnly + éles); az elutasítottak a naplóba kerülnek."""
    slug, cid = project.slug, project.customer_id
    ex = Executor(client, store, settings, project, run_id)
    camp_rn = {c["id"]: c["resource_name"] for c in enabled}
    first_rn = enabled[0]["resource_name"] if enabled else None
    stop = None
    for a in actions:
        if a.status != "proposed":
            store.log_action(run_id, slug, a.kind, a.target, None, a.detail, "rejected", "; ".join(a.rejected_because) or a.reason, settings.mode)
            continue
        if stop:
            a.reject(stop)
            store.log_action(run_id, slug, a.kind, a.target, None, a.detail, "rejected", stop, settings.mode)
            continue
        d = a.detail
        try:
            if a.kind == "add_negative":
                ex.apply(a.kind, neg_op(camp_rn.get(d.get("campaign_id"), first_rn), d["text"]), target=a.target, reason=a.reason, after={"negative": d["text"]})
            elif a.kind == "pause_keyword":
                ex.apply(a.kind, pause_keyword_ops(d["resource_name"]), target=a.target, reason=a.reason, before={"status": "ENABLED"}, after={"status": "PAUSED"})
            elif a.kind == "pause_ad":
                ex.apply(a.kind, pause_ad_ops(d["old_rn"]), target=a.target, reason=a.reason, before={"status": "ENABLED"}, after={"status": "PAUSED"})
            elif a.kind in ("rotate_rsa", "fix_disapproved"):
                ag_rn = f"customers/{cid}/adGroups/{d['ad_group_id']}"
                ex.apply(a.kind, rotate_rsa_ops(ag_rn, d["old_rn"], d["final_urls"], d["headlines"], d["descriptions"], d["path1"], d["path2"], d["pinned"]),
                         target=a.target, reason=a.reason, before={"removed": d["removed"]}, after={"added": d["added"]})
                if settings.live:
                    if a.kind == "rotate_rsa":
                        store.put(f"{slug}.rsa_rotated.{d['ad_group_id']}", today.isoformat())
                    else:
                        store.put(d["attempt_key"], (store.get(d["attempt_key"], []) or []) + [today.isoformat()])
            a.status = "applied" if settings.live else "validated"
        except WriteRefused as e:
            stop = str(e)
            a.reject(stop)
        except GoogleAdsError as e:
            a.status, a.rejected_because = "failed", [str(e)[:300]]


# ------------------------------------------------------------------ napi: a projekt csomagja változott-e?
def pack_watch(settings, project, store, client, run_id, *, today=None, now=None, fetch=net.fetch, send=None, **fetch_kw):
    """Ha a brief megváltozott, az ÉLŐ hirdetéseket és kulcsszavakat újraellenőrzi az új szabályokkal (tiltott szó, tény, versenytárs-márka);
    a szabálysértőket szünetelteti (a fék dry módban is él, mert csak csökkent) és szól. Hibás briefnél nem módosít, csak szól (3 naponta
    egyszer). A kreatívok (creatives.json) érvényessége itt nem számít: a szabályokat a brief adja."""
    slug, cid = project.slug, project.customer_id
    notify = (lambda *a, **k: alerts.notify(settings, store, project, *a, **({"send": send} if send else {}), **k))
    try:
        brief, warns, _changed = packmod.load_brief(project, store, fetch=fetch, **fetch_kw)
    except packmod.PackError as e:
        notify("pack_error", "Az Ads Pack hibás vagy nem elérhető", ["A motor a legutóbbi érvényes briefel dolgozik tovább.", ""] + e.problems[:8], dedupe_days=3)
        return {"status": "error", "problems": e.problems, "actions": []}
    digest = packcheck.content_hash(brief, None)
    if store.get(f"{slug}.brief_hash") == digest:
        return {"status": "unchanged", "actions": []}
    campaigns = reportdata.owned_campaigns(client, cid) if cid else []
    actions = []
    enabled = [c for c in campaigns if c["status"] == "ENABLED"]
    if enabled:
        ids = [c["id"] for c in enabled]
        for c in reportdata.ad_content_rows(client, cid, ids):
            if c["status"] != "ENABLED":
                continue
            errs = validators.errors(validators.check_rsa(c["headlines"], c["descriptions"], c["path1"], c["path2"], brief))
            if errs:
                actions.append(Action("pack_pause_ad", f"hirdetés {c['ad_id']} (csoport {c['ad_group_id']})", "az új brief szabályai szerint szabálysértő: " +
                                      "; ".join(e.message for e in errs[:3]), detail={"old_rn": c["resource_name"], "removed": [e.text for e in errs[:3]]}))
        for k in reportdata.keyword_status_rows(client, cid, ids):
            errs = validators.errors(validators.check_keyword(k["text"], brief)) if k["status"] == "ENABLED" else []
            if errs:
                actions.append(Action("pack_pause_keyword", f"{k['text']} [{k['match']}]", "az új brief szabályai szerint szabálysértő: " + "; ".join(e.message for e in errs[:2]),
                                      detail={"resource_name": k["resource_name"], "text": k["text"]}))
        ex = Executor(client, store, settings, project, run_id)
        for a in actions:
            try:
                ops = pause_ad_ops(a.detail["old_rn"]) if a.kind == "pack_pause_ad" else pause_keyword_ops(a.detail["resource_name"])
                ex.apply(a.kind, ops, target=a.target, reason=a.reason, before={"status": "ENABLED"}, after={"status": "PAUSED"}, safety=True)
                a.status = "applied"
            except (GoogleAdsError, WriteRefused) as e:
                a.status, a.rejected_because = "failed", [str(e)[:300]]
    store.put(f"{slug}.brief_hash", digest)
    if actions:
        notify(f"pack_violations:{digest}", "Az új brief miatt hirdetést vagy kulcsszót szüneteltettem",
               ["A projekt briefje megváltozott, és az élő anyag egy része már nem felel meg az új szabályoknak:", ""] +
               [f"  {a.target}: {a.reason} ({'szüneteltetve' if a.status == 'applied' else 'nem sikerült: ' + '; '.join(a.rejected_because)})" for a in actions] +
               ["", "Javítsd a briefet/kreatívokat, vagy a heti kör készít új szöveget (a szüneteltetett hirdetéseket a motor nem kapcsolja vissza)."], dedupe_days=7)
    return {"status": "changed", "actions": [action_dict(a) for a in actions], "warnings": warns}
