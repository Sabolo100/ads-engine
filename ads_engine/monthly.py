"""Havi terv: a hónap témája, kulcsszó-bővítés, hirdetési szempontok, kísérletek és tanulságok – Claude javaslata, a kódban lévő szabályokon át.

A havi terv NEM nyúl a pénzhez. Amit a motor maga alkalmaz: új kulcsszavak a MEGLÉVŐ hirdetéscsoportokba (≤ 10 szó, a magkifejezésekhez
közel, nem tiltott szó, nem versenytárs, nem ütközik negatívval, nem duplikátum; havonta legfeljebb MAX_NEW_KEYWORDS), és a téma + a
hirdetési szempontok, amelyek a szövegíró és a kreatív-gyár bemenetei. A kísérletek és a tanulságok a havi levélbe kerülnek: azokról az
ember dönt. A megfigyelési időszak alatt a kulcsszavak nem íródnak, de a terv (téma, szempontok, tanulságok) elkészül.
"""
import datetime as dt
import json
from typing import Annotated, List, Literal

from pydantic import BaseModel, Field, StringConstraints

from . import factory, guardrails as g, llm as llmmod, net, reportdata, review, sync, validators
from .actions import Action

MAX_NEW_KEYWORDS = 10            # havonta
Short = Annotated[str, StringConstraints(max_length=200)]


class KeywordIdea(BaseModel):
    text: str = Field(max_length=80)
    ad_group: str = Field(max_length=80, description="Egy MEGLÉVŐ hirdetéscsoport neve.")
    match: Literal["PHRASE", "EXACT"] = "PHRASE"
    reason: str = Field(default="", max_length=200)


class MonthlyPlan(BaseModel):
    theme: str = Field(max_length=120)
    rationale: str = Field(default="", max_length=600)
    keyword_ideas: List[KeywordIdea] = Field(default_factory=list, max_length=20)
    ad_angles: List[Short] = Field(default_factory=list, max_length=6)
    experiments: List[Short] = Field(default_factory=list, max_length=5)
    learnings: List[Short] = Field(default_factory=list, max_length=6)


# ------------------------------------------------------------------ bemenetek
def month_themes(brief, month):
    for m in brief.get("seasonality", []) or []:
        if m.get("month") == month:
            return list(m.get("themes", []))
    return []


def recent_reports(settings, slug, limit=4):
    """A legutóbbi heti jelentések (JSON a /data/reports alatt) tömör változata: időszak, számok, a legtöbb kattintást hozó keresések és kulcsszavak."""
    folder = settings.data_dir / "reports"
    out = []
    for path in sorted(folder.glob(f"{slug}-*.json"), reverse=True)[:limit] if folder.exists() else []:
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = (r.get("google") or {}).get("this") or {}
        w = r.get("web") or {}
        out.append({"időszak": r.get("period"), "kattintás": t.get("clicks"), "megjelenés": t.get("impressions"), "költés_ft": round((t.get("cost_micros") or 0) / 1e6),
                    "bevont_arány": w.get("engaged_rate"), "költség_bevont_látogatásra_ft": round((w.get("cost_per_engaged_micros") or 0) / 1e6) or None,
                    "top_keresések": [x["term"] for x in (r.get("top_terms") or [])[:6]], "top_kulcsszavak": [x["text"] for x in (r.get("top_keywords") or [])[:5]]})
    return out


def build_prompt(brief, today, status_rows, negatives, reports, registry_scenes, last_plan):
    forbidden = ", ".join(brief.get("voice", {}).get("forbidden_words", [])[:30]) or "nincs"
    system = (f"Te a(z) {brief['project']['name']} Google Ads kampányának havi stratégája vagy. A kapott adatok alapján havi tervet írsz magyarul: a hónap témája, "
              "új kulcsszavak a MEGLÉVŐ hirdetéscsoportokba, hirdetési szempontok (a szövegírónak), kísérletek (az embernek) és tanulságok.\n"
              "- Új kulcsszó: a magkifejezésekhez közel álló, valódi keresési szándékot kifejező (legfeljebb 10 szó), a megadott hirdetéscsoportok egyikébe; "
              "ne ismételj meglévőt, ne javasolj általános egyszavas kifejezést, versenytárs nevét, és semmit, ami a negatív listába ütközne.\n"
              f"- Tiltott szavak (kulcsszóban és szempontban sem): {forbidden}.\n"
              "- A szempontok (ad_angles) rövid irányok a hirdetésszövegekhez (pl. szezonális hangulat, konkrét előny); csak a brief tényeire építsenek, számot "
              "és kényes állítást ne tartalmazzanak.\n- A kísérletek az embernek szóló javaslatok (pl. új hirdetéscsoport, új célzás); a motor ezeket nem hajtja végre.\n"
              "- A tanulságok az elmúlt hetek adataiból levonható, óvatos megállapítások; ha kevés az adat, mondd ki.\n\n" + llmmod.DATA_RULE + "\nCsak a kért JSON-t add vissza.")
    groups = {}
    for r in status_rows:
        if r["status"] == "ENABLED":
            groups.setdefault(r["ad_group"], []).append(r["text"])
    user = "\n\n".join([
        llmmod.wrap_data("termék", {"összefoglaló": brief["product"]["summary"], "tények": [f["text"] for f in brief["product"]["facts"]],
                                    "magkulcsszavak": brief.get("keywords", {}).get("core", []), "célcsoportok": brief["product"].get("audiences", [])}),
        llmmod.wrap_data("idő", {"mai_nap": today.isoformat(), "e_havi_témák": month_themes(brief, today.month),
                                 "következő_havi_témák": month_themes(brief, today.month % 12 + 1)}),
        llmmod.wrap_data("hirdetéscsoportok_és_kulcsszavak", groups),
        llmmod.wrap_data("negatív_kulcsszavak", negatives[:150]),
        llmmod.wrap_data("elmúlt_hetek", reports or "még nincs heti jelentés"),
        llmmod.wrap_data("korábbi_képjelenetek", registry_scenes[-8:]),
        llmmod.wrap_data("előző_havi_terv", last_plan or "nincs"),
        "Írd meg a havi tervet."])
    return system, user


def make_plan(llm, slug, brief, today, status_rows, negatives, reports, registry_scenes, last_plan):
    system, user = build_prompt(brief, today, status_rows, negatives, reports, registry_scenes, last_plan)
    return llm.ask(project=slug, purpose="monthly_plan", system=system, user=user, schema=MonthlyPlan, effort="medium", max_tokens=8000)


# ------------------------------------------------------------------ javaslatok szűrése (kódban)
def applied_this_month(store, slug, today):
    first = today.replace(day=1).isoformat()
    return len([a for a in store.actions(project=slug, kind="add_keyword", since=first, limit=500) if a["status"] == "applied"])


def propose_keywords(plan, brief, status_rows, negatives, hands_off, budget_left, customer_id):
    """A havi terv kulcsszó-ötletei → Action-ök; az elutasítottak okokkal. Csak meglévő hirdetéscsoportba, szabályos, új, nem ütköző kulcsszó."""
    groups = {}
    for r in status_rows:
        groups.setdefault(validators.norm(r["ad_group"]), (r["ad_group_id"], r["ad_group"]))
    positives = {validators.norm(r["text"]) for r in status_rows}
    neg_tokens = [validators.tokens_of(n) for n in negatives]
    forbidden = brief.get("voice", {}).get("forbidden_words", [])
    out, seen, accepted = [], set(), 0
    for idea in plan.keyword_ideas:
        text = " ".join(idea.text.lower().split())
        a = Action("add_keyword", f"{text} [{idea.match}]", idea.reason or "a havi terv javaslata", detail={"text": text, "match": idea.match, "ad_group": idea.ad_group})
        gid = groups.get(validators.norm(idea.ad_group))
        reasons = []
        if not gid:
            reasons.append(f"ismeretlen hirdetéscsoport: {idea.ad_group}")
        else:
            a.detail["ad_group_id"] = gid[0]
            if f"customers/{customer_id}/adGroups/{gid[0]}" in hands_off:
                reasons.append("a hirdetéscsoport „kézben van” (ember módosította)")
        reasons += [i.message for i in validators.errors(validators.check_keyword(text, brief, require_close_to_core=True))]
        reasons += [i.message for i in validators.errors(validators.check_forbidden(text, forbidden))]
        nt = validators.tokens_of(text)
        if len(nt) < 2:
            reasons.append("túl általános (legalább két szó kell)")
        if validators.norm(text) in positives or validators.norm(text) in seen:
            reasons.append("már van ilyen kulcsszó")
        if any(n and any(nt[i:i + len(n)] == n for i in range(len(nt) - len(n) + 1)) for n in neg_tokens):
            reasons.append("ütközne egy negatív kulcsszóval")
        if not reasons and accepted >= budget_left:
            reasons.append(f"havi korlát ({MAX_NEW_KEYWORDS} új kulcsszó)")
        if reasons:
            out.append(a.reject(*dict.fromkeys(reasons)))
            continue
        seen.add(validators.norm(text))
        accepted += 1
        out.append(a)
    return out


# ------------------------------------------------------------------ a havi kör
def save_plan(settings, slug, month, data):
    folder = settings.data_dir / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug}-{month}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def run_monthly(settings, project, store, client, run_id, *, llm=None, today=None, now=None, fetch=net.fetch, **fetch_kw):
    """A havi terv: elkészíti, a szabályokon átengedett kulcsszavakat végrehajtja (egy atomi kérés mindegyik), elmenti. Visszaad: (jelentés, Action-ök)."""
    slug, cid = project.slug, project.customer_id
    now = now or sync.now_in(project)
    today = today or now.date()
    month = today.strftime("%Y-%m")
    base = {"project": slug, "name": project.name, "mode": settings.mode, "month": month, "generated": now.isoformat(timespec="seconds"),
            "notes": [], "keywords": {"added": [], "rejected": []}}
    if llm is None:
        base["skipped"] = "no_llm"
        base["notes"].append("A havi terv az AI-kulcs (ANTHROPIC_API_KEY) nélkül nem készül el.")
        return base, []
    campaigns = [c for c in reportdata.owned_campaigns(client, cid)]
    if not campaigns:
        base["skipped"] = "no_campaigns"
        base["notes"].append("Még nincs motor-kampány: a havi terv a kampány indulása után készül.")
        return base, []
    brief, brief_note = review.get_brief(project, store, fetch=fetch, **fetch_kw)
    if brief is None:
        base["skipped"] = "no_brief"
        base["notes"].append(brief_note)
        return base, []
    if brief_note:
        base["notes"].append(brief_note)
    ids = [c["id"] for c in campaigns]
    status_rows = reportdata.keyword_status_rows(client, cid, ids)
    negatives = [n["text"] for n in reportdata.negative_rows(client, cid, ids)]
    scenes = [e["detail"].get("scene", "") for e in factory.Registry(store, settings, project).entries() if e["source"] == "ai"]
    try:
        plan = make_plan(llm, slug, brief, today, status_rows, negatives, recent_reports(settings, slug), scenes, store.get(f"{slug}.plan.last"))
    except llmmod.LLMError as e:
        base["skipped"] = "llm_error"
        base["notes"].append(f"A havi terv most nem készült el: {e}")
        return base, []
    base.update({"theme": plan.theme, "rationale": plan.rationale, "ad_angles": plan.ad_angles, "experiments": plan.experiments, "learnings": plan.learnings})
    store.put(f"{slug}.plan.theme", plan.theme)
    store.put(f"{slug}.plan.angles", plan.ad_angles)
    store.put(f"{slug}.plan.last", {"month": month, "theme": plan.theme, "learnings": plan.learnings, "experiments": plan.experiments})

    # --- kulcsszavak: csak a megfigyelési időszak után, nem szabályozott területen, nem kézben lévő hirdetéscsoportba
    go_live = store.get(f"{slug}.go_live") or {}
    left = g.observation_days_left(dt.date.fromisoformat(go_live["date"]) if go_live.get("date") else None, today)
    hands = review.active_hands_off(store, slug, today)
    budget_left = max(0, MAX_NEW_KEYWORDS - applied_this_month(store, slug, today))
    actions = propose_keywords(plan, brief, status_rows, negatives, hands, budget_left, cid)
    blocked = None
    if store.get(f"{slug}.approved_daily_micros") is None:
        blocked = "még nincs go-live"
    elif left > 0:
        blocked = f"megfigyelési időszak (még {left} nap)"
    elif g.requires_human(brief):
        blocked = "szabályozott terület: csak emberi jóváhagyással"
    elif not any(c["status"] == "ENABLED" for c in campaigns):
        blocked = "nincs bekapcsolt kampány"
    if blocked:
        for a in actions:
            if a.status == "proposed":
                a.reject(blocked)
        base["notes"].append(f"Új kulcsszó ebben a hónapban nem íródik: {blocked}.")
    enabled = [c for c in campaigns if c["status"] == "ENABLED"]
    review._execute(settings, project, store, client, run_id, actions, enabled, today)
    finalize(base, actions)
    base["path"] = str(save_plan(settings, slug, month, base))
    return base, actions


def finalize(report, actions):
    """A végrehajtás után: a kulcsszó-találatok a jelentésbe (alkalmazott/ellenőrzött és elutasított)."""
    report["keywords"] = {"added": [{"text": a.target, "ad_group": a.detail.get("ad_group", ""), "status": a.status, "reason": a.reason}
                                    for a in actions if a.status in ("applied", "validated")],
                          "failed": [{"text": a.target, "because": a.rejected_because} for a in actions if a.status == "failed"],
                          "rejected": [{"text": a.target, "ad_group": a.detail.get("ad_group", ""), "because": a.rejected_because} for a in actions if a.status == "rejected"]}
    return report
