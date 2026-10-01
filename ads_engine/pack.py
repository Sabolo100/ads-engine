"""Ads Pack: a projekt brief-je (brief.json) és kész hirdetései (creatives.json), a projekt SAJÁT oldaláról húzva.

A szerződés a docs/ADS_ENGINE_BEKOTES.md-ben van. Itt: letöltés (SSRF-védelemmel, ETag-gyorsítótárral), séma szerinti és
tartalmi ellenőrzés. Hibás csomagot a motor nem használ: a hibalista a heti levélbe és a `plan` kimenetére kerül.
"""
import dataclasses
import hashlib
import json
import pathlib
import urllib.parse

from . import jsonschema_lite, net, validators

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
MAX_JSON_BYTES = 2_000_000
MAX_IMAGE_BYTES = 12_000_000


class PackError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        head = "; ".join(self.problems[:4])
        super().__init__(f"Az Ads Pack nem használható ({len(self.problems)} hiba): {head}")


@dataclasses.dataclass
class Pack:
    brief: dict
    creatives: dict
    brief_url: str
    creatives_url: str
    warnings: list
    content_hash: str
    changed: bool = True


def load_schema(name):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def host_of(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def resolve(base, ref):
    """Relatív hivatkozás feloldása a brief/creatives címéhez képest."""
    return urllib.parse.urljoin(base, ref)


# ------------------------------------------------------------------ ellenőrzés
def validate_brief(brief, project=None):
    """Séma + tartalmi ellenőrzés. Visszaadja a hibák (str) és a figyelmeztetések (str) listáját."""
    errs = jsonschema_lite.validate(load_schema("ads-brief.schema.json"), brief)
    warns = []
    if errs:
        return errs, warns
    ids = [p["id"] for p in brief["landing_pages"]]
    if len(set(ids)) != len(ids):
        errs.append("landing_pages: az azonosítók nem lehetnek ismétlődők")
    fids = [f["id"] for f in brief["product"]["facts"]]
    if len(set(fids)) != len(fids):
        errs.append("product.facts: az azonosítók nem lehetnek ismétlődők")
    if project:
        if brief["project"]["slug"] != project.slug:
            errs.append(f"project.slug ({brief['project']['slug']}) nem egyezik a motor konfigjával ({project.slug})")
        allowed = {h.lower() for h in project.allowed_hosts}
        if host_of(brief["project"]["site"]) not in allowed:
            errs.append(f"project.site gazdagépe nincs az engedélyezettek között: {host_of(brief['project']['site'])}")
        for p in brief["landing_pages"]:
            if host_of(p["url"]) not in allowed:
                errs.append(f"landing_pages[{p['id']}].url gazdagépe nincs az engedélyezettek között: {host_of(p['url'])}")
        wid = brief.get("tracking", {}).get("umami_website_id")
        if wid and project.umami_website_id and wid != project.umami_website_id:
            warns.append("tracking.umami_website_id eltér a motor konfigjában lévőtől (a konfig az irányadó)")
    return errs, warns


def validate_creatives(creatives, brief):
    """Séma + a hirdetésszövegek, kulcsszavak, hivatkozások tartalmi ellenőrzése. (hibák, figyelmeztetések) – str listák."""
    errs = jsonschema_lite.validate(load_schema("creative-pack.schema.json"), creatives)
    warns = []
    if errs:
        return errs, warns
    landing_ids = {p["id"] for p in brief["landing_pages"]}
    ids = [a["id"] for a in creatives["adsets"]]
    if len(set(ids)) != len(ids):
        errs.append("adsets: az azonosítók nem lehetnek ismétlődők")
    all_positive = []
    for a in creatives["adsets"]:
        all_positive += [k["text"] for k in a["keywords"]]
    for a in creatives["adsets"]:
        tag = f"adsets[{a['id']}]"
        if a["landing"] not in landing_ids:
            errs.append(f"{tag}.landing ({a['landing']}) nincs a brief landing_pages között")
        for i in validators.check_rsa(a["headlines"], a["descriptions"], a.get("path1", ""), a.get("path2", ""), brief):
            (errs if i.level == "error" else warns).append(f"{tag}: {i}")
        positives = [k["text"] for k in a["keywords"]]
        for k in a["keywords"]:
            for i in validators.check_keyword(k["text"], brief):
                (errs if i.level == "error" else warns).append(f"{tag}.keywords: {i}")
        for n in a.get("negatives", []):
            for i in validators.check_negative(n, brief, positives):
                (errs if i.level == "error" else warns).append(f"{tag}.negatives: {i}")
    for n in brief["keywords"].get("negatives", []):
        for i in validators.check_negative(n, brief, all_positive):
            (errs if i.level == "error" else warns).append(f"brief.keywords.negatives: {i}")
    seen = set()
    for s in creatives.get("sitelinks", []):
        if s["landing"] not in landing_ids:
            errs.append(f"sitelinks[{s['text']}].landing ({s['landing']}) nincs a brief landing_pages között")
        if validators.norm(s["text"]) in seen:
            errs.append(f"sitelinks: ismétlődő szöveg: {s['text']}")
        seen.add(validators.norm(s["text"]))
        for text, kind in ((s["text"], "sitelink"), (s.get("description1", ""), "sitelink_desc"), (s.get("description2", ""), "sitelink_desc")):
            if text:
                for i in validators.check_text(text, kind, brief, where="hivatkozás"):
                    (errs if i.level == "error" else warns).append(f"sitelinks: {i}")
    for c in creatives.get("callouts", []):
        for i in validators.check_text(c, "callout", brief, where="kiemelés"):
            (errs if i.level == "error" else warns).append(f"callouts: {i}")
    img_ids = [i["id"] for i in creatives.get("images", [])]
    if len(set(img_ids)) != len(img_ids):
        errs.append("images: az azonosítók nem lehetnek ismétlődők")
    return errs, warns


def content_hash(brief, creatives):
    blob = json.dumps({"brief": brief, "creatives": creatives}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# ------------------------------------------------------------------ betöltés
def _get_json(url, project, store, key, fetch, **fetch_kw):
    """JSON letöltése ETag-gyorsítótárral (store.kv); 304 esetén a tárolt példányt adja."""
    headers = {"Accept": "application/json"}
    etag = store.get(f"{project.slug}.etag.{key}") if store else None
    if etag and store.get(f"{project.slug}.cache.{key}") is not None:
        headers["If-None-Match"] = etag
    r = fetch(url, project.allowed_hosts, max_bytes=MAX_JSON_BYTES, headers=headers, **fetch_kw)
    if r.status == 304 and store:
        return store.get(f"{project.slug}.cache.{key}"), False
    try:
        data = json.loads(r.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise PackError([f"{url}: nem érvényes JSON ({e})"]) from None
    if store:
        old = store.get(f"{project.slug}.cache.{key}")
        store.put(f"{project.slug}.cache.{key}", data)
        if r.headers.get("etag"):
            store.put(f"{project.slug}.etag.{key}", r.headers["etag"])
        return data, old != data
    return data, True


def load(project, store=None, fetch=net.fetch, **fetch_kw):
    """A projekt csomagja: brief + creatives, ellenőrizve. PackError, ha bármi hibás (a hibalistával)."""
    try:
        brief, c1 = _get_json(project.brief_url, project, store, "brief", fetch, **fetch_kw)
    except net.FetchError as e:
        raise PackError([f"A brief nem tölthető le: {e}"]) from None
    errs, warns = validate_brief(brief, project)
    if errs:
        raise PackError([f"brief: {e}" for e in errs])
    creatives, c2, creatives_url = {"schema_version": 1, "version": "motor-generalt", "adsets": []}, False, ""
    if brief.get("creatives_url"):
        creatives_url = resolve(project.brief_url, brief["creatives_url"])
        try:
            creatives, c2 = _get_json(creatives_url, project, store, "creatives", fetch, **fetch_kw)
        except net.FetchError as e:
            raise PackError([f"A creatives.json nem tölthető le: {e}"]) from None
        cerrs, cwarns = validate_creatives(creatives, brief)
        warns += cwarns
        if cerrs:
            raise PackError([f"creatives: {e}" for e in cerrs])
    return Pack(brief=brief, creatives=creatives, brief_url=project.brief_url, creatives_url=creatives_url, warnings=warns,
                content_hash=content_hash(brief, creatives), changed=bool(c1 or c2))


def fetch_image(pack, project, ref, fetch=net.fetch, **fetch_kw):
    """Egy kép (relatív a creatives.json-hoz) – csak az image_hosts-ról, méretkorláttal. Bájtokat ad vissza."""
    base = pack.creatives_url or pack.brief_url
    url = resolve(base, ref)
    hosts = project.image_hosts or project.allowed_hosts
    r = fetch(url, hosts, max_bytes=MAX_IMAGE_BYTES, headers={"Accept": "image/*"}, **fetch_kw)
    ctype = r.headers.get("content-type", "")
    if ctype and not ctype.startswith("image/"):
        raise net.FetchError(f"nem kép típusú válasz ({ctype}): {url[:80]}")
    return r.body, url
