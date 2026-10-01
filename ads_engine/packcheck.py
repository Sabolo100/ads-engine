"""Az Ads Pack tartalmi ellenőrzése – csak szabványos Python, I/O nélkül (a sémákat paraméterként kapja).

Ugyanezt a kódot használja a motor (pack.py) és az önálló bekötési validátor (validator/ads_pack_validator.py, a
tools/build_validator.py állítja elő), így a másik projekt pontosan azt a szabályt látja, amit a motor érvényesít.
"""
import hashlib
import json
import urllib.parse

from . import jsonschema_lite, validators


def host_of(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def content_hash(brief, creatives):
    blob = json.dumps({"brief": brief, "creatives": creatives}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def validate_brief(brief, schema, *, slug=None, allowed_hosts=None, umami_website_id=None):
    """Séma + tartalmi ellenőrzés. (hibák, figyelmeztetések) – str listák.

    slug / allowed_hosts / umami_website_id: a motor konfigjából (projects.toml); az önálló validátor a briefből vezeti le."""
    errs = jsonschema_lite.validate(schema, brief)
    warns = []
    if errs:
        return errs, warns
    ids = [p["id"] for p in brief["landing_pages"]]
    if len(set(ids)) != len(ids):
        errs.append("landing_pages: az azonosítók nem lehetnek ismétlődők")
    fids = [f["id"] for f in brief["product"]["facts"]]
    if len(set(fids)) != len(fids):
        errs.append("product.facts: az azonosítók nem lehetnek ismétlődők")
    if slug and brief["project"]["slug"] != slug:
        errs.append(f"project.slug ({brief['project']['slug']}) nem egyezik a motor konfigjával ({slug})")
    if allowed_hosts is not None:
        allowed = {h.lower() for h in allowed_hosts}
        if host_of(brief["project"]["site"]) not in allowed:
            errs.append(f"project.site gazdagépe nincs az engedélyezettek között: {host_of(brief['project']['site'])}")
        for p in brief["landing_pages"]:
            if host_of(p["url"]) not in allowed:
                errs.append(f"landing_pages[{p['id']}].url gazdagépe nincs az engedélyezettek között: {host_of(p['url'])}")
    wid = brief.get("tracking", {}).get("umami_website_id")
    if wid and umami_website_id and wid != umami_website_id:
        warns.append("tracking.umami_website_id eltér a motor konfigjában lévőtől (a konfig az irányadó)")
    return errs, warns


def validate_creatives(creatives, brief, schema):
    """Séma + a hirdetésszövegek, kulcsszavak, hivatkozások tartalmi ellenőrzése. (hibák, figyelmeztetések) – str listák."""
    errs = jsonschema_lite.validate(schema, creatives)
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

    def add(issues, tag):
        for i in issues:
            (errs if i.level == "error" else warns).append(f"{tag}: {i}")

    for a in creatives["adsets"]:
        tag = f"adsets[{a['id']}]"
        if a["landing"] not in landing_ids:
            errs.append(f"{tag}.landing ({a['landing']}) nincs a brief landing_pages között")
        add(validators.check_rsa(a["headlines"], a["descriptions"], a.get("path1", ""), a.get("path2", ""), brief), tag)
        positives = [k["text"] for k in a["keywords"]]
        for k in a["keywords"]:
            add(validators.check_keyword(k["text"], brief), f"{tag}.keywords")
        for n in a.get("negatives", []):
            add(validators.check_negative(n, brief, positives), f"{tag}.negatives")
    for n in brief["keywords"].get("negatives", []):
        add(validators.check_negative(n, brief, all_positive), "brief.keywords.negatives")
    seen = set()
    for s in creatives.get("sitelinks", []):
        if s["landing"] not in landing_ids:
            errs.append(f"sitelinks[{s['text']}].landing ({s['landing']}) nincs a brief landing_pages között")
        if validators.norm(s["text"]) in seen:
            errs.append(f"sitelinks: ismétlődő szöveg: {s['text']}")
        seen.add(validators.norm(s["text"]))
        for text, kind in ((s["text"], "sitelink"), (s.get("description1", ""), "sitelink_desc"), (s.get("description2", ""), "sitelink_desc")):
            if text:
                add(validators.check_text(text, kind, brief, where="hivatkozás"), "sitelinks")
    for c in creatives.get("callouts", []):
        add(validators.check_text(c, "callout", brief, where="kiemelés"), "callouts")
    img_ids = [i["id"] for i in creatives.get("images", [])]
    if len(set(img_ids)) != len(img_ids):
        errs.append("images: az azonosítók nem lehetnek ismétlődők")
    return errs, warns
