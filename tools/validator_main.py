# -*- coding: utf-8 -*-
# Az önálló validátor főprogramja. A tools/build_validator.py ezt fűzi a beágyazott modulok után; önmagában nem futtatható
# (a jsonschema_lite, validators, packcheck modulok, a SCHEMAS és a VERSION a generált fájlban vannak).
import argparse
import json
import os
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

UTM = {"utm_source": "google", "utm_medium": "cpc", "utm_campaign": "validator", "utm_content": "1", "utm_term": "teszt"}
IMG_KINDS = {"1.91:1": (1.91, (600, 314)), "1:1": (1.0, (300, 300)), "4:5": (0.8, (480, 600))}
GOOGLE_MAX_IMAGE = 5 * 1024 * 1024
SOURCE_MAX_IMAGE = 12 * 1024 * 1024


def relax_https(node):
    """--allow-http (fejlesztéshez): a séma https-mintái http-t is elfogadnak. Éles ellenőrzésnél NEM használjuk."""
    if isinstance(node, dict):
        return {k: (v.replace("^https://", "^https?://") if k == "pattern" and isinstance(v, str) else relax_https(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [relax_https(v) for v in node]
    return node


def read_text(source, allow_http, timeout=20, max_bytes=2_000_000):
    if os.path.exists(source):
        with open(source, "rb") as f:
            return f.read(max_bytes + 1).decode("utf-8"), source
    if not (source.startswith("https://") or (allow_http and source.startswith("http://"))):
        raise SystemExit(f"Nem található ilyen fájl, és nem https cím: {source}")
    req = urllib.request.Request(source, headers={"User-Agent": "ads-pack-validator", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes + 1).decode("utf-8"), r.geturl()


def fetch(url, timeout=20, max_bytes=SOURCE_MAX_IMAGE):
    req = urllib.request.Request(url, headers={"User-Agent": "ads-pack-validator"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), dict((k.lower(), v) for k, v in r.headers.items()), r.read(max_bytes + 1)


def image_size(data):
    """(szélesség, magasság, formátum) a fájl fejlécéből – külső csomag nélkül. PNG, JPEG, GIF."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "PNG"
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h, "GIF"
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h, "JPEG"
            i += 2 + seg
    return None


def check_landing(page, add):
    sep = "&" if "?" in page["url"] else "?"
    url = page["url"] + sep + urllib.parse.urlencode(UTM)
    try:
        status, final, _, _ = fetch(url, max_bytes=500_000)
    except urllib.error.HTTPError as e:
        add("error", f"landing_pages[{page['id']}]: HTTP {e.code} ({page['url']})")
        return
    except Exception as e:
        add("error", f"landing_pages[{page['id']}]: nem érhető el ({e})")
        return
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query)
    lost = [k for k in UTM if k not in q]
    if lost:
        add("error", f"landing_pages[{page['id']}]: az átirányítás elveszíti a követő paramétereket ({', '.join(lost)}) – a végső cím: {final}")
    else:
        add("ok", f"landing_pages[{page['id']}]: elérhető, az utm_* paraméterek megmaradnak")


def check_image(base, ref, add, label):
    url = urllib.parse.urljoin(base, ref["file"] if isinstance(ref, dict) else ref)
    try:
        status, final, headers, data = fetch(url)
    except Exception as e:
        add("error", f"{label}: a kép nem tölthető le ({url}): {e}")
        return
    ctype = headers.get("content-type", "")
    if ctype and not ctype.startswith("image/"):
        add("error", f"{label}: nem kép típusú válasz ({ctype})")
        return
    if len(data) > SOURCE_MAX_IMAGE:
        add("error", f"{label}: a kép túl nagy (> {SOURCE_MAX_IMAGE // (1024 * 1024)} MB)")
        return
    size = image_size(data)
    if not size:
        add("error", f"{label}: nem JPEG, PNG vagy GIF (a Google csak ezeket fogadja)")
        return
    w, h, fmt = size
    ratio = w / h
    wanted = ref.get("ratio") if isinstance(ref, dict) else None
    if wanted and wanted in IMG_KINDS:
        target, (mw, mh) = IMG_KINDS[wanted]
        if abs(ratio - target) / target > 0.01:
            add("warn", f"{label}: {w}×{h} ({ratio:.2f}), a megadott arány {wanted}: a motor a Google méreteire vágja")
        if w < mw or h < mh:
            add("error", f"{label}: {w}×{h} túl kicsi ({wanted}: legalább {mw}×{mh})")
    msg = f"{label}: {fmt} {w}×{h}, {len(data) // 1024} KB"
    if len(data) > GOOGLE_MAX_IMAGE:
        add("warn", msg + " – 5 MB fölött, a motor tömöríti")
    else:
        add("ok", msg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ads_pack_validator", description="Ads Engine – Ads Pack ellenőrző (brief.json + creatives.json). Verzió: " + VERSION)
    ap.add_argument("source", nargs="?", help="a brief.json URL-je (https://…/ads/brief.json) vagy helyi fájlja")
    ap.add_argument("--online", action="store_true", help="a nyitóoldalakat és a képeket is lekéri (UTM-megőrzés, képméretek)")
    ap.add_argument("--allow-http", action="store_true", help="http és helyi cím engedése (fejlesztéshez)")
    ap.add_argument("--json", action="store_true", help="géppel olvasható kimenet")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    if args.version:
        print(f"ads_pack_validator {VERSION}")
        return 0
    if not args.source:
        ap.print_help()
        return 2
    results = []
    add = lambda level, msg: results.append((level, msg))
    schemas = relax_https(SCHEMAS) if args.allow_http else SCHEMAS

    try:
        text, base = read_text(args.source, args.allow_http)
        brief = json.loads(text)
    except (OSError, ValueError, urllib.error.URLError, UnicodeDecodeError) as e:
        print(f"✗ A brief nem olvasható: {e}")
        return 1
    host = packcheck.host_of(brief.get("project", {}).get("site", "")) if isinstance(brief, dict) else ""
    allowed = {host, "www." + host} if host else None
    errs, warns = packcheck.validate_brief(brief, schemas["brief"], allowed_hosts=allowed)
    for e in errs:
        add("error", f"brief: {e}")
    for w in warns:
        add("warn", f"brief: {w}")
    creatives = None
    if not errs and brief.get("creatives_url"):
        curl = urllib.parse.urljoin(base, brief["creatives_url"]) if "://" in base else os.path.join(os.path.dirname(os.path.abspath(base)), brief["creatives_url"])
        try:
            ctext, cbase = read_text(curl, args.allow_http)
            creatives = json.loads(ctext)
            cerrs, cwarns = packcheck.validate_creatives(creatives, brief, schemas["creatives"])
            for e in cerrs:
                add("error", f"creatives: {e}")
            for w in cwarns:
                add("warn", f"creatives: {w}")
            if not cerrs:
                n = len(creatives["adsets"])
                add("ok", f"creatives: {n} hirdetéscsoport, {sum(len(a['keywords']) for a in creatives['adsets'])} kulcsszó, "
                          f"{len(creatives.get('images', []))} kép – a szövegek, tények és kulcsszavak rendben")
        except (OSError, ValueError, urllib.error.URLError, UnicodeDecodeError) as e:
            add("error", f"creatives: nem olvasható ({curl}): {e}")
    elif not errs:
        add("warn", "Nincs creatives_url: a motor maga ír szöveget a briefből (kész hirdetések nélkül).")
    if not errs:
        add("ok", f"brief: séma és tartalom rendben ({brief['project']['name']}, {len(brief['product']['facts'])} tény, {len(brief['landing_pages'])} nyitóoldal)")
    if args.online and not errs:
        for page in brief["landing_pages"]:
            check_landing(page, add)
        if creatives:
            cbase = urllib.parse.urljoin(base, brief["creatives_url"]) if "://" in base else base
            for im in creatives.get("images", []):
                check_image(cbase, im, add, f"images[{im['id']}]")
            for lg in creatives.get("logos", []):
                check_image(cbase, lg, add, f"logos[{lg['id']}]")
    n_err = sum(1 for lvl, _ in results if lvl == "error")
    n_warn = sum(1 for lvl, _ in results if lvl == "warn")
    if args.json:
        print(json.dumps({"ok": n_err == 0, "errors": n_err, "warnings": n_warn, "results": [{"level": a, "message": b} for a, b in results]},
                         ensure_ascii=False, indent=1))
    else:
        sym = {"ok": "✓", "warn": "!", "error": "✗"}
        for lvl, msg in results:
            print(f" {sym[lvl]} {msg}")
        print()
        print("A csomag megfelel a motor szabályainak." if not n_err else f"{n_err} hiba van: javítsd, mielőtt a motor használná.")
        if not args.online:
            print("(A nyitóoldalak és képek élő ellenőrzéséhez: --online)")
    return 1 if n_err else 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
