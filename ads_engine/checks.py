"""`check`: végigmegy a beállításon, és KIÍRJA A KÖVETKEZŐ HIÁNYZÓ LÉPÉST.

Szintek: ok · warn (figyelmeztetés, nem akadály) · todo (a te lépésed hiányzik) · fail (hiba, ami megakadályozza az indulást).
A lépések számai a docs/GOOGLE_ADS_BEALLITAS.md számozását követik.
"""
import dataclasses
import os

from . import http, log
from .google import discovery
from .google.auth import AuthError
from .google.client import GoogleAdsError

DOC = "docs/GOOGLE_ADS_BEALLITAS.md"


@dataclasses.dataclass
class Check:
    id: str
    level: str
    title: str
    detail: str = ""
    hint: str = ""


def _gads_error(e):
    return f"{e}"


def run_checks(settings, project, client=None, sa=None, request=http.request, offline=False):
    """A beállítás ellenőrzése. client/sa: a már összeállított Google-kliens és szolgáltatásfiók (tesztelhetőség)."""
    out = []
    add = lambda *a, **k: out.append(Check(*a, **k))

    # --- üzemmód, adatmappa
    add("config.mode", "ok", f"Üzemmód: {settings.mode}",
        "éles: a motor ír a Google Ads-be" if settings.live else "dry: semmit nem ír (a validateOnly próbát meghívja); a biztonsági fékek élesek")
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("config.data", "ok", f"Adatmappa írható: {settings.data_dir}")
    except OSError as e:
        add("config.data", "fail", f"Az adatmappa nem írható: {settings.data_dir}", str(e), "Coolify: állíts be egy tartós kötetet a /data útvonalra.")
    stop = (settings.data_dir / "STOP").exists()
    if stop:
        add("config.stop", "warn", "A STOP fájl létezik: a motor nem ír (csak a fékek futnak)", hint="Töröld a STOP fájlt az adatmappából az újraindításhoz.")

    # --- Google: kulcs, belépés
    if sa is None and client is None:
        add("google.key", "todo", "Hiányzik a Google szolgáltatásfiók-kulcs (GADS_SA_JSON_B64)",
            hint=f"{DOC} 3. és 6. lépés: szolgáltatásfiók + JSON-kulcs, majd a Coolify titkos változójába.")
        client = None
    else:
        if sa is not None:
            add("google.key", "ok", f"Szolgáltatásfiók: {sa.client_email}", f"Cloud-projekt: {sa.project_id or '?'}")
        if client is not None and not offline:
            try:
                client.tokens.token()
                add("google.token", "ok", "A Google belépés működik (JWT → access token)")
            except AuthError as e:
                add("google.token", "fail", "A Google belépés nem sikerült", str(e), "Új JSON-kulcs kellhet (3. lépés).")
                client = None
    if client is None or offline:
        _tail_checks(out, settings, project, request, offline)
        return out

    # --- Google: hozzáférés
    try:
        ids = client.list_accessible_customers()
    except GoogleAdsError as e:
        hint = "Apply for access (Explorer) a Cloud Console-on, a Google Ads API Overview oldalán (2. lépés)." if e.status in (401, 403) else ""
        add("google.access", "fail", "A Google Ads API nem elérhető", _gads_error(e), hint)
        _tail_checks(out, settings, project, request, offline)
        return out
    if not ids:
        add("google.access", "todo", "A szolgáltatásfióknak még nincs hozzáférése egyetlen Google Ads-fiókhoz",
            hint=f"{DOC} 4. lépés: Google Ads → Admin → Access and security → + → a szolgáltatásfiók e-mail-címe (az MCC-n).")
    else:
        add("google.access", "ok", f"Közvetlenül elérhető fiókok: {', '.join(ids)}")
        if settings.login_customer_id and settings.login_customer_id not in ids:
            add("google.login", "fail", f"A GADS_LOGIN_CUSTOMER_ID ({settings.login_customer_id}) nincs a hozzáférhető fiókok között",
                hint="A kezelői (MCC) fiók azonosítóját add meg, kötőjel nélkül, és add hozzá a szolgáltatásfiókot (4. lépés).")
        elif project.customer_id and project.customer_id not in ids and not settings.login_customer_id:
            add("google.login", "warn", "Az ügyfélfiók nem közvetlenül elérhető, de nincs GADS_LOGIN_CUSTOMER_ID",
                hint="Állítsd be a kezelői (MCC) fiók azonosítóját a GADS_LOGIN_CUSTOMER_ID változóban.")

    # --- Google: a projekt ügyfélfiókja
    cid = project.customer_id
    if not cid:
        add("google.account", "todo", f"A „{project.name}” Google Ads ügyfélfiók azonosítója még nincs megadva",
            hint=f"{DOC} 1. lépés: hozd létre az ügyfélfiókot (Magyarország, Europe/Budapest, {project.currency}), és írd a config/projects.toml-ba (customer_id).")
    else:
        _account_checks(add, client, project, cid)
    _tail_checks(out, settings, project, request, offline)
    return out


def _account_checks(add, client, project, cid):
    q = ("SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.status, "
         "customer.manager, customer.test_account, customer.auto_tagging_enabled FROM customer")
    try:
        rows = client.search(cid, q)
    except GoogleAdsError as e:
        hint = "Add hozzá a szolgáltatásfiókot a fiókhoz vagy az MCC-hez, és ellenőrizd a GADS_LOGIN_CUSTOMER_ID-t (4. lépés)." if e.has("USER_PERMISSION_DENIED") else ""
        add("google.account", "fail", f"A(z) {cid} ügyfélfiók nem olvasható", _gads_error(e), hint)
        return
    c = (rows[0]["customer"] if rows else {})
    add("google.account", "ok", f"Ügyfélfiók: {c.get('descriptiveName', '?')} ({cid})",
        f"állapot: {c.get('status', '?')} · pénznem: {c.get('currencyCode', '?')} · időzóna: {c.get('timeZone', '?')}")
    if c.get("manager"):
        add("google.account_type", "fail", "Ez kezelői (MCC) fiók, nem ügyfélfiók", hint="A projekthez egy ügyfélfiók azonosítója kell.")
    if c.get("status") and c["status"] != "ENABLED":
        add("google.account_status", "fail", f"Az ügyfélfiók állapota: {c['status']}", hint="A Google Ads-ben nézd meg a fiók állapotát (számlázás, felfüggesztés).")
    if c.get("currencyCode") and c["currencyCode"] != project.currency:
        add("google.currency", "warn", f"A fiók pénzneme {c['currencyCode']}, a projektben {project.currency}",
            hint="A pénznem a fiók létrehozása után nem módosítható; a keretszámítások a fiók pénznemében készülnek.")
    if c.get("timeZone") and c["timeZone"] != project.timezone:
        add("google.timezone", "warn", f"A fiók időzónája {c['timeZone']}, a projektben {project.timezone}")
    if c.get("testAccount"):
        add("google.test", "warn", "Tesztfiók: éles költés nincs, csak a hívások próbálhatók")
    if c.get("autoTaggingEnabled"):
        add("google.autotag", "warn", "Az automatikus címkézés (gclid) be van kapcsolva",
            hint="Kapcsold ki (Admin → Account settings → Auto-tagging), hogy a „személyes adat nélkül” ígéret tiszta maradjon; az UTM elég.")
    # EU politikai hirdetési nyilatkozat hiánya a meglévő kampányokon
    try:
        miss = client.search(cid, "SELECT campaign.id, campaign.name FROM campaign WHERE campaign.missing_eu_political_advertising_declaration = TRUE")
        if miss:
            add("google.eu", "warn", f"{len(miss)} kampányból hiányzik az EU politikai hirdetési nyilatkozat",
                ", ".join(r["campaign"].get("name", "?") for r in miss[:5]), "Töltsd ki a Google Ads-ben (ezek nem a motor kampányai).")
    except GoogleAdsError as e:
        add("google.eu", "warn", "Az EU-nyilatkozat állapota nem ellenőrizhető", str(e)[:200])
    # automatikus ajánlás-alkalmazás
    try:
        subs = client.search(cid, "SELECT recommendation_subscription.type, recommendation_subscription.status FROM recommendation_subscription "
                                  "WHERE recommendation_subscription.status = 'ENABLED'")
        if subs:
            types = ", ".join(r["recommendationSubscription"].get("type", "?") for r in subs)
            add("google.autoapply", "warn", "Automatikus ajánlás-alkalmazás be van kapcsolva", types,
                "Kapcsold ki (Ajánlások → Automatikus alkalmazás): a Google magától módosítaná a kampányokat, összeütközne a motorral.")
    except GoogleAdsError:
        pass                     # nem kritikus: Explorer szinten vagy új fiókon nem mindig érhető el
    # hirdetőazonosítás
    try:
        iv = client.get_identity_verification(cid).get("identityVerification", [])
        if iv:
            prog = iv[0].get("verificationProgress", {})
            status = prog.get("programStatus", "?")
            if status == "SUCCESS":
                add("google.identity", "ok", "Hirdetőazonosítás: kész")
            else:
                req = iv[0].get("identityVerificationRequirement", {})
                add("google.identity", "todo", f"Hirdetőazonosítás: {status}",
                    f"határidő: {req.get('verificationCompletionDeadlineTime') or req.get('verificationStartDeadlineTime') or '?'}",
                    f"Fejezd be az azonosítást: {prog.get('actionUrl', 'Google Ads → Admin → Advertiser verification')}")
    except GoogleAdsError as e:
        add("google.identity", "warn", "A hirdetőazonosítás állapota nem ellenőrizhető", str(e)[:200])
    # a motor címkéje
    try:
        lab = client.search(cid, "SELECT label.id, label.name FROM label WHERE label.name = 'ads-engine'")
        add("google.label", "ok" if lab else "ok", "Az „ads-engine” címke " + ("megvan" if lab else "még nincs (az első kampánnyal jön létre)"))
    except GoogleAdsError:
        pass


def _tail_checks(out, settings, project, request, offline=False):
    add = lambda *a, **k: out.append(Check(*a, **k))
    # --- Umami, AI, levél
    if settings.umami_url and settings.umami_user and settings.umami_password:
        add("umami", "ok", f"Umami: {settings.umami_url} ({settings.umami_user})", "a bejelentkezést az első kiértékelés ellenőrzi")
    else:
        add("umami", "todo", "Az Umami csak-olvasó hozzáférés hiányzik (UMAMI_URL, UMAMI_USER, UMAMI_PASSWORD)",
            hint=f"{DOC} 5. lépés: Umami csapat + „View only” felhasználó.")
    add("llm", "ok" if settings.anthropic_key else "todo", f"AI (elemzés, szöveg): {settings.llm_model}" if settings.anthropic_key
        else "Hiányzik az ANTHROPIC_API_KEY", hint="" if settings.anthropic_key else f"{DOC} 6. lépés: Anthropic API-kulcs a Coolify-ban.")
    add("openai", "ok" if settings.openai_key else "warn", "Képgenerálás (OpenAI): kulcs megvan" if settings.openai_key
        else "Nincs OPENAI_API_KEY: új AI-képek nem készülnek (a meglévő képek használhatók)")
    if settings.smtp_host and settings.smtp_user and settings.smtp_password and settings.report_to:
        add("smtp", "ok", f"Heti levél: {settings.smtp_from or settings.smtp_user} → {', '.join(settings.report_to)}")
    else:
        add("smtp", "todo", "A heti levélhez hiányzik az SMTP-beállítás vagy a címzett (SMTP_*, REPORT_TO)", hint=f"{DOC} 6. lépés.")
    # --- API-leíró, nyitóoldal
    d = discovery.Discovery.load_cached(settings.api_version)
    if d:
        left = discovery.days_until(discovery.KNOWN_SUNSETS[settings.api_version]) if settings.api_version in discovery.KNOWN_SUNSETS else None
        add("api", "warn" if (left is not None and left < 120) else "ok", f"Google Ads API {settings.api_version}, leíró revízió {d.revision}",
            f"{left} nap a lejáratig" if left is not None else "")
    else:
        add("api", "warn", f"A {settings.api_version} leíró-dokumentum nincs a helyi gyorsítótárban", hint="Futtasd: python -m ads_engine api-check")
    if offline:
        return
    try:
        r = request("GET", project.site, timeout=15, retries=1)
        add("site", "ok", f"A nyitóoldal elérhető: {project.site} ({r.status})")
    except http.HttpError as e:
        add("site", "warn", f"A nyitóoldal nem érhető el: {project.site}", str(e)[:160])


def next_step(checks):
    """Az első nyitott lépés (todo vagy fail), vagy None."""
    for c in checks:
        if c.level in ("fail", "todo"):
            return c
    return None


SYMBOL = {"ok": "✓", "warn": "!", "todo": "→", "fail": "✗"}


def render(checks):
    lines = []
    for c in checks:
        lines.append(f" {SYMBOL.get(c.level, '?')} {c.title}")
        if c.detail:
            lines.append(f"     {c.detail}")
        if c.hint and c.level != "ok":
            lines.append(f"     ↳ {c.hint}")
    n = next_step(checks)
    lines.append("")
    if n:
        lines.append(f"KÖVETKEZŐ LÉPÉS: {n.title}")
        if n.hint:
            lines.append(f"  {n.hint}")
    else:
        lines.append("Minden rendben: az éles indulás előtti beállítás kész.")
    return "\n".join(lines)
