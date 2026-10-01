"""Védelmek: a motor minden kifelé ható művelete ezeken megy át (kódban, nem promptban).

A Google napi kerete ÁTLAG: egy nap akár 2×-t is költhet, havonta legfeljebb napi×30,4-et számláz. A „heti keret” ezért
7 × napi keret, NEM kemény plafon – a védelmek a valódi kockázatot figyelik: elírt keret (extra nulla), túlköltés, ember
általi módosítás. A fékek (szünet) dry üzemmódban is működnek, mert csak csökkenthetnek.
"""
import dataclasses

ENGINE_LABEL = "ads-engine"            # a motor által létrehozott kampányok címkéje (a tulajdonjog kampányszintű)
DAILY_OVERSHOOT = 2.1                  # tegnapi költés > 2,1 × napi keret → szünet (a Google napi 2×-t is költhet)
MONTHLY_FACTOR = 30.4                  # havi számlázási határ a napi keret többszöröse
MONTHLY_TOLERANCE = 1.10               # a keret-előzményből számolt 30 napi összeg + 10 % tűrés
CONFIRM_FACTOR = 2.0                   # a jóváhagyotthoz képest ≥ 2×-es keretemelés → szünet + megerősítés (elírás-védelem)
MICROS = 1_000_000


@dataclasses.dataclass
class Finding:
    code: str
    level: str                 # pause (fék) | alert (levél) | info (tájékoztatás)
    message: str
    data: dict = dataclasses.field(default_factory=dict)


def fmt(micros_value, currency="HUF"):
    return f"{int(round(micros_value / MICROS)):,}".replace(",", " ") + f" {currency}"


def weekly_from_daily(daily_micros):
    return int(daily_micros) * 7


def check_budget(*, approved_daily, live_daily, cost_yesterday=0, cost_30d=0, budget_30d_total=None, currency="HUF"):
    """Napi keret-őr. Minden összeg micros-ban. approved_daily=None: még nincs go-live (nincs jóváhagyott keret).

    live_daily: az ENABLED saját kampányok napi kereteinek összege most. budget_30d_total: az elmúlt 30 nap napi kereteinek
    összege az előzményből (ha nincs, 30,4 × napi keret). Visszaadja a megállapításokat; a hívó hajtja végre (szünet, levél)."""
    out = []
    if approved_daily is None:
        if live_daily > 0:
            out.append(Finding("not_approved", "pause",
                               "A kampányok futnak, de még nincs jóváhagyott keret (go-live) – a motor szünetelteti őket.",
                               {"live_daily": live_daily}))
        return out
    if live_daily >= CONFIRM_FACTOR * approved_daily and approved_daily > 0:
        out.append(Finding("budget_confirm", "pause",
                           f"A napi keret {fmt(live_daily, currency)} lett a jóváhagyott {fmt(approved_daily, currency)} helyett "
                           f"(legalább 2×). Elírás ellen szüneteltetek, amíg meg nem erősíted.",
                           {"approved": approved_daily, "live": live_daily}))
        basis = approved_daily
    else:
        basis = max(live_daily, approved_daily)
        if live_daily > approved_daily:
            out.append(Finding("budget_raised", "info", f"A keretet felemelted ({fmt(approved_daily, currency)} → {fmt(live_daily, currency)} naponta), átvettem.",
                               {"approved": approved_daily, "live": live_daily}))
        elif 0 < live_daily < approved_daily:
            out.append(Finding("budget_lowered", "info", f"A keretet csökkentetted ({fmt(approved_daily, currency)} → {fmt(live_daily, currency)} naponta), átvettem.",
                               {"approved": approved_daily, "live": live_daily}))
    if basis > 0 and cost_yesterday > DAILY_OVERSHOOT * basis:
        out.append(Finding("daily_overspend", "pause",
                           f"Tegnap {fmt(cost_yesterday, currency)} ment el a napi keret ({fmt(basis, currency)}) több mint 2,1-szerese – szünet.",
                           {"cost": cost_yesterday, "basis": basis}))
    limit = (budget_30d_total * MONTHLY_TOLERANCE) if budget_30d_total else MONTHLY_FACTOR * basis
    if basis > 0 and cost_30d > limit:
        out.append(Finding("monthly_overspend", "pause",
                           f"Az elmúlt 30 nap költése ({fmt(cost_30d, currency)}) meghaladja a keretből várhatót ({fmt(limit, currency)}) – szünet.",
                           {"cost_30d": cost_30d, "limit": limit}))
    return out


def next_approved(approved_daily, live_daily, findings):
    """Mi legyen a jóváhagyott napi keret a napi szinkron után? Csak a megerősítésre váró emelés NEM íródik át."""
    if any(f.code == "budget_confirm" for f in findings):
        return approved_daily
    if live_daily > 0 and approved_daily is not None:
        return live_daily
    return approved_daily


# ------------------------------------------------------------------ tulajdonjog
def owned_campaign(campaign_row, label_resource_name):
    """A kampány a motoré-e: a címkéi között van az `ads-engine` címke."""
    return label_resource_name in (campaign_row.get("campaign", {}).get("labels") or [])


# ------------------------------------------------------------------ ember általi módosítás
TRACKED_FIELDS = {"status", "name", "amountMicros"}
API_CLIENT_TYPES = {"GOOGLE_ADS_API", "GOOGLE_ADS_SCRIPTS"}


def diff_snapshots(prev, cur):
    """Két pillanatkép ({erőforrásnév: {mező: érték}}) különbsége: ami az utolsó saját írásunk óta megváltozott."""
    changes = []
    for name, fields in (prev or {}).items():
        now = (cur or {}).get(name)
        if now is None:
            changes.append({"resource": name, "field": "*", "before": "létezett", "after": "eltűnt"})
            continue
        for k, v in fields.items():
            if k in TRACKED_FIELDS and now.get(k) != v:
                changes.append({"resource": name, "field": k, "before": v, "after": now.get(k)})
    return changes


def human_change_events(rows, own_resource_names, sa_email=""):
    """A Google change_event sorai közül azok, amelyeket NEM API-kliens (tehát ember, automatikus szabály, ajánlás) végzett
    a motor saját objektumain."""
    out = []
    for r in rows:
        ev = r.get("changeEvent", {})
        if ev.get("clientType") in API_CLIENT_TYPES or (sa_email and ev.get("userEmail") == sa_email):
            continue
        if ev.get("changeResourceName") in own_resource_names or ev.get("campaign") in own_resource_names:
            out.append({"resource": ev["changeResourceName"], "who": ev.get("userEmail") or ev.get("clientType", "?"),
                        "client": ev.get("clientType", ""), "when": ev.get("changeDateTime", ""),
                        "fields": ev.get("changedFields", "")})
    return out


def never_reenable(prev_status, cur_status, changed_by_non_api):
    """Amit nem API-kliens szüneteltetett (te, automatikus szabály, ajánlás), azt a motor nem kapcsolja vissza."""
    return prev_status == "ENABLED" and cur_status == "PAUSED" and changed_by_non_api


# ------------------------------------------------------------------ heti módosítások szabályai (a motor MINDEN javaslata ezeken megy át)
OBSERVATION_DAYS = 14                  # az éles indulás után 14 napig csak megfigyelés és fékek
MIN_CLICKS_PAUSE = 25                  # ennyi kattintás előtt nem ítélünk kulcsszó felett (kis adatnál nem hajszolunk zajt)
MIN_WEB_VISITS_TRUST = 5               # ennyi Umami-látogatás alatt a webes adat nem megbízható a döntéshez
MAX_PAUSES_PER_WEEK = 3
MAX_NEGATIVES_PER_WEEK = 15
ROTATE_INTERVAL_DAYS = 14              # RSA-forgatás legfeljebb kéthetente, hirdetéscsoportonként
MAX_ROTATIONS_PER_WEEK = 2
HANDS_OFF_DAYS = 28


def observation_days_left(go_live_date, today):
    """Hány napig tart még a megfigyelési időszak (0 = vége). go_live_date: date vagy None (nincs go-live → nincs mit optimalizálni)."""
    if go_live_date is None:
        return OBSERVATION_DAYS
    return max(0, OBSERVATION_DAYS - (today - go_live_date).days)


def vet_negative(text, evidence_term, brief, positives, existing, validators):
    """Egy javasolt negatív kulcsszó ellenőrzése. Üres lista = elfogadható; különben az elutasítás okai.

    · a negatív szavai egybefüggően szerepelnek a megfigyelt keresési kifejezésben (nem találhat ki a motor semmit)
    · nem zárja ki a mag- és a pozitív kulcsszavakat (pl. a „kutya” tiltott negatív), és már nincs meg."""
    reasons = [i.message for i in validators.errors(validators.check_negative(text, brief, positives))]
    nt, et = validators.tokens_of(text), validators.tokens_of(evidence_term)
    if len(text.strip()) < 3:
        reasons.append("túl rövid negatív kulcsszó")
    if len(nt) > 4:
        reasons.append("túl hosszú negatív kulcsszó (legfeljebb 4 szó)")
    if not any(et[i:i + len(nt)] == nt for i in range(len(et) - len(nt) + 1)) or not nt:
        reasons.append("a negatív nem szerepel a megfigyelt keresési kifejezésben")
    if validators.norm(text) in {validators.norm(x) for x in existing}:
        reasons.append("már van ilyen negatív kulcsszó")
    return reasons


def vet_keyword_pause(kw, ad_group_rows, web, brief, validators, *, hands_off=()):
    """Kulcsszó szüneteltetése: elég adat, bizonyíthatóan nem hoz bevont látogatást, nem a mag, nem az utolsó a csoportban, nincs „kézben”."""
    reasons = []
    if kw["status"] != "ENABLED":
        reasons.append("nem aktív")
    if kw["clicks"] < MIN_CLICKS_PAUSE:
        reasons.append(f"kevés adat ({kw['clicks']} kattintás < {MIN_CLICKS_PAUSE})")
    if web is None or web.get("visits", 0) < MIN_WEB_VISITS_TRUST:
        reasons.append("nincs megbízható webes adat a kulcsszóhoz")
    elif web.get("engaged", 0) > 0:
        reasons.append("hoz bevont látogatást")
    core = {validators.norm(c) for c in brief.get("keywords", {}).get("core", [])}
    if validators.norm(kw["text"]) in core:
        reasons.append("magkifejezés (a brief védi)")
    enabled_in_group = [r for r in ad_group_rows if r["ad_group_id"] == kw["ad_group_id"] and r["status"] == "ENABLED"]
    if len(enabled_in_group) <= 1:
        reasons.append("az utolsó aktív kulcsszó a hirdetéscsoportban")
    if kw.get("resource_name") in hands_off or kw.get("campaign_rn") in hands_off:
        reasons.append("kézben van (ember módosította)")
    return reasons


def iso_week(d):
    """ISO-hét azonosítója (pl. 2026-W42): a heti számlálók (AI-képkeret) és a heti feladat időszaka."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def requires_human(brief):
    """Szabályozott terület (compliance.category = regulated): a motor nem módosít magától, csak jelent; a fékek (csökkentés) élnek."""
    return (brief or {}).get("compliance", {}).get("category") == "regulated"
