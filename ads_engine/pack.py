"""Ads Pack: a projekt brief-je (brief.json) és kész hirdetései (creatives.json), a projekt SAJÁT oldaláról húzva.

A szerződés a docs/ADS_ENGINE_BEKOTES.md-ben van. Itt: letöltés (SSRF-védelemmel, ETag-gyorsítótárral), séma szerinti és
tartalmi ellenőrzés. Hibás csomagot a motor nem használ: a hibalista a heti levélbe és a `plan` kimenetére kerül.
"""
import dataclasses
import json
import pathlib
import urllib.parse

from . import net, packcheck

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
MAX_JSON_BYTES = 2_000_000
MAX_IMAGE_BYTES = 12_000_000


class PackError(Exception):
    def __init__(self, problems, kind="invalid"):
        self.problems = list(problems)
        self.kind = kind                      # "fetch": az oldal nem érhető el (átmeneti) · "invalid": a csomag hibás
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
    """Séma + tartalmi ellenőrzés a projekt konfigjával (slug, engedélyezett gazdagépek, Umami-azonosító). (hibák, figyelmeztetések)"""
    kw = {}
    if project:
        kw = {"slug": project.slug, "allowed_hosts": project.allowed_hosts, "umami_website_id": project.umami_website_id}
    return packcheck.validate_brief(brief, load_schema("ads-brief.schema.json"), **kw)


def validate_creatives(creatives, brief):
    return packcheck.validate_creatives(creatives, brief, load_schema("creative-pack.schema.json"))


content_hash = packcheck.content_hash
host_of = packcheck.host_of


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


def load_brief(project, store=None, fetch=net.fetch, **fetch_kw):
    """Csak a brief (a szabályok és tények forrása): letöltés ETag-gyorsítótárral + ellenőrzés. (brief, figyelmeztetések, változott-e).
    PackError, ha nem tölthető le (kind="fetch") vagy hibás (kind="invalid"). Az érvényes brief az „utolsó érvényes” példány lesz."""
    try:
        brief, changed = _get_json(project.brief_url, project, store, "brief", fetch, **fetch_kw)
    except net.FetchError as e:
        raise PackError([f"A brief nem tölthető le: {e}"], kind="fetch") from None
    errs, warns = validate_brief(brief, project)
    if errs:
        raise PackError([f"brief: {e}" for e in errs])
    if store:
        store.put(f"{project.slug}.valid.brief", brief)             # ha az oldal később nem elérhető vagy a brief elromlik, ezzel dolgozunk tovább
    return brief, warns, changed


def load(project, store=None, fetch=net.fetch, **fetch_kw):
    """A projekt csomagja: brief + creatives, ellenőrizve. PackError, ha bármi hibás (a hibalistával)."""
    brief, warns, c1 = load_brief(project, store, fetch, **fetch_kw)
    creatives, c2, creatives_url = {"schema_version": 1, "version": "motor-generalt", "adsets": []}, False, ""
    if brief.get("creatives_url"):
        creatives_url = resolve(project.brief_url, brief["creatives_url"])
        try:
            creatives, c2 = _get_json(creatives_url, project, store, "creatives", fetch, **fetch_kw)
        except net.FetchError as e:
            raise PackError([f"A creatives.json nem tölthető le: {e}"], kind="fetch") from None
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
