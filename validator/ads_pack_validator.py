#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ads Engine – Ads Pack ellenőrző (önálló, külső csomag nélküli). Verzió: v0.2.0 · b21e458

GENERÁLT FÁJL – ne szerkeszd. Forrás: ads-engine / tools/build_validator.py (a motor jsonschema_lite, validators és packcheck
moduljából, a sémákból és a tools/validator_main.py-ból). Ugyanazt a szabályt érvényesíti, mint a motor.

Használat:
  python ads_pack_validator.py https://pelda.hu/ads/brief.json            séma + tartalom (szövegek, tények, kulcsszavak)
  python ads_pack_validator.py https://pelda.hu/ads/brief.json --online   + nyitóoldalak (UTM megmarad-e), képek (méret, arány)
  python ads_pack_validator.py ads/brief.json                             helyi fájl
Kilépési kód: 0 = rendben, 1 = hiba van, 2 = hibás használat. Python 3.9+, csak szabványos könyvtár.
"""
import json
import sys
import types

VERSION = 'v0.2.0 · b21e458'
MODULE_ORDER = ('jsonschema_lite', 'validators', 'packcheck')
SCHEMAS = json.loads('{"brief": {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "ads-engine/ads-brief@1", "title": "Ads Engine – projekt-brief (brief.json)", "description": "A projekt oldalán (pl. https://pacsit.hu/ads/brief.json) kitett leírás: mit hirdessen a motor, kinek, milyen hangon, milyen tényekkel. A JSON-kulcsok angolok, az értékek az ország nyelvén (itt magyarul) vannak.", "type": "object", "required": ["schema_version", "project", "product", "voice", "landing_pages", "keywords"], "additionalProperties": false, "properties": {"schema_version": {"const": 1, "description": "A séma verziója."}, "project": {"type": "object", "required": ["slug", "name", "site", "language", "country"], "additionalProperties": false, "properties": {"slug": {"type": "string", "pattern": "^[a-z0-9-]{2,30}$", "description": "Rövid azonosító, kisbetű (pl. pacsi). Megegyezik a motor projects.toml-jában lévővel."}, "name": {"type": "string", "minLength": 1, "maxLength": 40, "description": "A termék/márka neve, ahogy a hirdetésekben szerepel."}, "site": {"type": "string", "pattern": "^https://[^/]+/?$", "description": "A kezdőoldal https-címe (pl. https://pacsit.hu)."}, "language": {"type": "string", "pattern": "^[a-z]{2}$", "description": "A hirdetések nyelve, kétbetűs kód (hu)."}, "country": {"type": "string", "pattern": "^[A-Z]{2}$", "description": "A célország kétbetűs kódja (HU). A hirdetés csak itt jelenik meg."}}}, "product": {"type": "object", "required": ["summary", "facts"], "additionalProperties": false, "properties": {"summary": {"type": "string", "minLength": 20, "maxLength": 600, "description": "Mit csinál a termék, kinek, 2–4 mondatban."}, "offer": {"type": "string", "maxLength": 300, "description": "A fő ajánlat egy mondatban (pl. ingyenes, regisztráció nélküli kvíz)."}, "audiences": {"type": "array", "maxItems": 8, "items": {"type": "object", "required": ["id", "name"], "additionalProperties": false, "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_-]{1,30}$"}, "name": {"type": "string", "maxLength": 80}, "needs": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 160}, "description": "Mire van szüksége (a hirdetés ezt szólítja meg)."}, "search_intents": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 100}, "description": "Mit keresnek a Google-ben (kulcsszó-ötletek)."}}}}, "usps": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 80}, "description": "Rövid előnyök (kiemelések alapja)."}, "facts": {"type": "array", "minItems": 1, "maxItems": 60, "description": "Bizonyítható tények. A hirdetésben szereplő minden szám és kényes állítás (ingyenes, regisztráció nélkül, garantált…) csak ezekből jöhet; a motor kódban ellenőrzi.", "items": {"type": "object", "required": ["id", "text"], "additionalProperties": false, "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_]{1,40}$"}, "text": {"type": "string", "minLength": 3, "maxLength": 200, "description": "A tény mondatban."}, "numbers": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]+([.,][0-9]+)?$"}, "description": "Azok a számok, amelyek a hirdetésben szerepelhetnek (pl. 124, 10)."}, "tokens": {"type": "array", "items": {"type": "string", "minLength": 2, "maxLength": 40}, "description": "Azok a kényes szavak/kifejezések, amelyeket ez a tény igazol (pl. ingyenes, regisztráció nélkül)."}}}}}}, "voice": {"type": "object", "required": ["tone"], "additionalProperties": false, "properties": {"tone": {"type": "string", "maxLength": 300, "description": "Hangnem (pl. barátságos, könnyed, tegező)."}, "avoid": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 160}, "description": "Mit kerüljön a szöveg (stílus)."}, "forbidden_words": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 2, "maxLength": 40}, "description": "Tiltott szavak a hirdetésszövegekben (ékezet- és kisbetű-függetlenül ellenőrizzük)."}, "forbidden_claims": {"type": "array", "maxItems": 30, "items": {"type": "string", "maxLength": 200}, "description": "Tiltott állítások (szabad szöveg, az AI-szövegíró ezt kapja; a kódos ellenőrzés a forbidden_words-ön fut)."}}}, "compliance": {"type": "object", "additionalProperties": false, "properties": {"category": {"enum": ["none", "regulated"], "description": "regulated: szabályozott terület (pénzügy, egészség, gyógyszer, szerencsejáték, politika…): ilyenkor a motor csak emberi jóváhagyással ír."}, "notes": {"type": "string", "maxLength": 600}}}, "landing_pages": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object", "required": ["id", "url", "purpose"], "additionalProperties": false, "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_-]{1,30}$"}, "url": {"type": "string", "pattern": "^https://", "description": "A végső URL UTM nélkül; a motor a kampányon ad hozzá követő paramétereket. A lekérdezés-szöveg (?utm_…) átmegy az átirányításokon (a validátor ellenőrzi)."}, "purpose": {"type": "string", "maxLength": 160, "description": "Mire való az oldal, milyen szándékú keresőnek jó."}}}}, "keywords": {"type": "object", "required": ["core"], "additionalProperties": false, "properties": {"core": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "string", "minLength": 2, "maxLength": 80}, "description": "Magkifejezések: a motor ezeket soha nem zárja ki negatívként, és az új kulcsszavaknak ezekhez közel kell lenniük."}, "negatives": {"type": "array", "maxItems": 500, "items": {"type": "string", "minLength": 2, "maxLength": 80}, "description": "Kezdő negatív kulcsszavak (kifejezés típusú)."}, "competitor_brands": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 60}, "description": "Versenytárs-márkanevek: a motor nem használja őket (kulcsszóban sem)."}}}, "tracking": {"type": "object", "additionalProperties": false, "properties": {"umami_website_id": {"type": "string", "maxLength": 64}, "visit_event": {"type": "string", "maxLength": 50, "description": "Az az esemény, ami látogatásonként egyszer tüzel (alap: inditas). Ebből számolja a motor a hirdetésekből jövő látogatásokat."}, "engaged_events": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 50}, "description": "Az az esemény, ami „bevont látogatást” jelent (oldalbetöltésenként egyszer; Pacsi: bevont)."}, "key_events": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 50}, "description": "A legfontosabb cselekvések (Pacsi: kviz-kesz, kartya)."}}}, "ads": {"type": "object", "additionalProperties": false, "properties": {"max_cpc": {"type": "number", "minimum": 1, "description": "A kattintásonkénti költség plafonja a fiók pénznemében (Maximize clicks mellett)."}, "business_name": {"type": "string", "maxLength": 25, "description": "Cégnév a hirdetésekben (Demand Gen)."}}}, "brand": {"type": "object", "additionalProperties": false, "properties": {"colors": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}, "description": "Arculati színek (pl. primary, background, text)."}, "fonts": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 60}}, "logo": {"type": "string", "maxLength": 300, "description": "A logó fájlja (relatív a brief-hez vagy abszolút https)."}, "image_style_prompt": {"type": "string", "maxLength": 1500, "description": "Képstílus-leírás az AI-képgeneráláshoz (angolul is jó)."}, "reference_images": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 300}}}}, "seasonality": {"type": "array", "maxItems": 12, "items": {"type": "object", "required": ["month", "themes"], "additionalProperties": false, "properties": {"month": {"type": "integer", "minimum": 1, "maximum": 12}, "themes": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 80}}}}}, "creatives_url": {"type": "string", "maxLength": 300, "description": "A kész hirdetések (creatives.json) helye; relatív a brief-hez vagy abszolút https. Ha nincs, a motor maga ír szöveget a briefből."}}}, "creatives": {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "ads-engine/creative-pack@1", "title": "Ads Engine – kreatív-csomag (creatives.json)", "description": "Kész hirdetések, amelyeket a projekt ad a motornak. A szövegek hossza itt is ellenőrzött (a Google korlátai szerint); a tartalmi szabályokat (tények, tiltott szavak, írásjelek) a motor és a validátor kódban ellenőrzi.", "type": "object", "required": ["schema_version", "version", "adsets"], "additionalProperties": false, "properties": {"schema_version": {"const": 1}, "version": {"type": "string", "minLength": 1, "maxLength": 40, "description": "A csomag verziója (pl. 2026-10-05.1). Változáskor a motor új adatot lát."}, "adsets": {"type": "array", "minItems": 1, "maxItems": 20, "description": "Hirdetéscsoportok (egy téma/keresési szándék = egy csoport).", "items": {"type": "object", "required": ["id", "channel", "theme", "landing", "headlines", "descriptions", "keywords"], "additionalProperties": false, "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_-]{1,30}$"}, "channel": {"enum": ["search"], "description": "Egyelőre: search (reszponzív keresési hirdetés)."}, "theme": {"type": "string", "maxLength": 60, "description": "A hirdetéscsoport neve/témája."}, "landing": {"type": "string", "pattern": "^[a-z0-9_-]{1,30}$", "description": "A brief landing_pages egyik azonosítója."}, "headlines": {"type": "array", "minItems": 3, "maxItems": 15, "items": {"type": "string", "minLength": 1, "maxLength": 30}, "description": "Címek, legfeljebb 30 karakter, felkiáltójel nélkül."}, "descriptions": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string", "minLength": 1, "maxLength": 90}, "description": "Leírások, legfeljebb 90 karakter, legfeljebb egy felkiáltójellel."}, "path1": {"type": "string", "maxLength": 15}, "path2": {"type": "string", "maxLength": 15}, "keywords": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "object", "required": ["text", "match"], "additionalProperties": false, "properties": {"text": {"type": "string", "minLength": 2, "maxLength": 80}, "match": {"enum": ["PHRASE", "EXACT"], "description": "Kifejezés vagy pontos egyezés (széles egyezés nincs)."}}}}, "negatives": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 2, "maxLength": 80}, "description": "Csak erre a csoportra érvényes negatív kulcsszavak."}}}}, "sitelinks": {"type": "array", "maxItems": 8, "items": {"type": "object", "required": ["text", "landing"], "additionalProperties": false, "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 25}, "description1": {"type": "string", "maxLength": 35}, "description2": {"type": "string", "maxLength": 35}, "landing": {"type": "string", "pattern": "^[a-z0-9_-]{1,30}$"}}}}, "callouts": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1, "maxLength": 25}, "description": "Kiemelések (pl. Ingyenes, Regisztráció nélkül)."}, "images": {"type": "array", "maxItems": 20, "items": {"type": "object", "required": ["id", "file", "alt"], "additionalProperties": false, "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_-]{1,40}$"}, "file": {"type": "string", "maxLength": 300, "description": "A kép fájlja (relatív a creatives.json-hoz vagy abszolút https), JPEG vagy PNG, legfeljebb 5 MB."}, "ratio": {"enum": ["1.91:1", "1:1", "4:5"], "description": "Az arány; ha nincs, a motor a méretből állapítja meg, és ha kell, a Google méreteire vágja."}, "alt": {"type": "string", "minLength": 3, "maxLength": 125, "description": "Alternatív szöveg (akadálymentesség, a kép tartalma)."}}}}, "logos": {"type": "array", "maxItems": 4, "items": {"type": "object", "required": ["id", "file"], "additionalProperties": false, "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_-]{1,40}$"}, "file": {"type": "string", "maxLength": 300}}}}, "videos": {"type": "array", "maxItems": 10, "items": {"type": "object", "required": ["youtube_id", "orientation"], "additionalProperties": false, "properties": {"youtube_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{11}$"}, "orientation": {"enum": ["vertical", "horizontal", "square"]}, "title": {"type": "string", "maxLength": 100}}}}}}}')
SOURCES = {'jsonschema_lite': '"""Kis JSON-Schema ellenőrző (csak szabványos Python) – a brief és a kreatív-csomag sémáihoz.\n\nA használt részhalmaz: type (több típus is), required, properties, additionalProperties (bool vagy séma), items, minItems,\nmaxItems, uniqueItems, enum, const, minLength, maxLength, pattern, minimum, maximum, anyOf, oneOf, $ref (#/$defs/…).\nA hibák magyarul, JSON-útvonallal (pl. $.product.facts[2].text) jönnek, hogy a másik projekt gyorsan javíthassa.\nEz a fájl önállóan is használható (a bekötési validátor ezt ágyazza be).\n"""\nimport re\n\n\ndef validate(schema, value, root=None):\n    """Hibák listája (üres = megfelel)."""\n    errs = []\n    _check(schema, value, "$", errs, root or schema)\n    return errs\n\n\ndef _types(t):\n    return t if isinstance(t, list) else [t]\n\n\ndef _is(value, t):\n    if t == "object":\n        return isinstance(value, dict)\n    if t == "array":\n        return isinstance(value, list)\n    if t == "string":\n        return isinstance(value, str)\n    if t == "boolean":\n        return isinstance(value, bool)\n    if t == "integer":\n        return isinstance(value, int) and not isinstance(value, bool)\n    if t == "number":\n        return isinstance(value, (int, float)) and not isinstance(value, bool)\n    if t == "null":\n        return value is None\n    return True\n\n\nTYPE_HU = {"object": "objektum", "array": "lista", "string": "szöveg", "boolean": "igaz/hamis érték", "integer": "egész szám",\n           "number": "szám", "null": "üres érték"}\n\n\ndef _name(value):\n    for t in ("null", "boolean", "integer", "number", "string", "array", "object"):\n        if _is(value, t):\n            return TYPE_HU[t]\n    return type(value).__name__\n\n\ndef _resolve(ref, root):\n    if not ref.startswith("#/"):\n        raise ValueError(f"nem támogatott $ref: {ref}")\n    node = root\n    for part in ref[2:].split("/"):\n        node = node[part]\n    return node\n\n\ndef _check(schema, v, path, errs, root):\n    if "$ref" in schema:\n        _check(_resolve(schema["$ref"], root), v, path, errs, root)\n        return\n    if "const" in schema and v != schema["const"]:\n        errs.append(f"{path}: az értéknek ennek kell lennie: {schema[\'const\']!r}")\n        return\n    if "enum" in schema and v not in schema["enum"]:\n        errs.append(f"{path}: érvénytelen érték {v!r} (lehet: {\', \'.join(map(str, schema[\'enum\']))})")\n        return\n    if "type" in schema:\n        ts = _types(schema["type"])\n        if not any(_is(v, t) for t in ts):\n            errs.append(f"{path}: {\' vagy \'.join(TYPE_HU.get(t, t) for t in ts)} kell, ez: {_name(v)}")\n            return\n    for key in ("anyOf", "oneOf"):\n        if key in schema:\n            ok = [s for s in schema[key] if not validate(s, v, root)]\n            if (key == "anyOf" and not ok) or (key == "oneOf" and len(ok) != 1):\n                errs.append(f"{path}: nem felel meg a megadott változatok egyikének sem")\n    if isinstance(v, dict):\n        props = schema.get("properties", {})\n        for req in schema.get("required", []):\n            if req not in v:\n                errs.append(f"{path}: hiányzik a kötelező mező: {req}")\n        for k, val in v.items():\n            if k in props:\n                _check(props[k], val, f"{path}.{k}", errs, root)\n            else:\n                ap = schema.get("additionalProperties", True)\n                if ap is False:\n                    errs.append(f"{path}.{k}: ismeretlen mező")\n                elif isinstance(ap, dict):\n                    _check(ap, val, f"{path}.{k}", errs, root)\n    elif isinstance(v, list):\n        if "minItems" in schema and len(v) < schema["minItems"]:\n            errs.append(f"{path}: legalább {schema[\'minItems\']} elem kell, most {len(v)} van")\n        if "maxItems" in schema and len(v) > schema["maxItems"]:\n            errs.append(f"{path}: legfeljebb {schema[\'maxItems\']} elem lehet, most {len(v)} van")\n        if schema.get("uniqueItems") and len({repr(x) for x in v}) != len(v):\n            errs.append(f"{path}: az elemek nem ismétlődhetnek")\n        if "items" in schema:\n            for i, item in enumerate(v):\n                _check(schema["items"], item, f"{path}[{i}]", errs, root)\n    elif isinstance(v, str):\n        if "minLength" in schema and len(v) < schema["minLength"]:\n            errs.append(f"{path}: legalább {schema[\'minLength\']} karakter kell, most {len(v)}")\n        if "maxLength" in schema and len(v) > schema["maxLength"]:\n            errs.append(f"{path}: legfeljebb {schema[\'maxLength\']} karakter lehet, most {len(v)}")\n        if "pattern" in schema and not re.search(schema["pattern"], v):\n            errs.append(f"{path}: nem megfelelő formátum ({schema[\'pattern\']}): {v[:60]!r}")\n    elif isinstance(v, (int, float)) and not isinstance(v, bool):\n        if "minimum" in schema and v < schema["minimum"]:\n            errs.append(f"{path}: legalább {schema[\'minimum\']} kell, ez: {v}")\n        if "maximum" in schema and v > schema["maximum"]:\n            errs.append(f"{path}: legfeljebb {schema[\'maximum\']} lehet, ez: {v}")\n', 'validators': '"""Szöveg- és kulcsszó-ellenőrzők – KÓDBAN, nem promptban.\n\nElv: a hirdetésszöveg minden számát és kényes állítását (ingyenes, regisztráció nélkül, garantált, allergia, ár, felsőfok…)\na brief tényei (`product.facts`) igazolják. Amit nem igazol tény, azt a motor nem teszi ki: nem számít, ki írta a szöveget\n(ember, a projekt skillje vagy az AI). Ugyanez a fájl önállóan is használható (a bekötési validátor ezt ágyazza be).\n\nGoogle szerkesztési szabályok, amelyeket itt kódolunk: cím ≤ 30, leírás ≤ 90, útvonal ≤ 15 karakter; cím nem tartalmazhat\nfelkiáltójelet, a leírásban legfeljebb egy lehet; nincs ismétlődő írásjel, három pont, emoji; nincs csupa nagybetűs szó\n(rövidítés kivételével); nincs URL, e-mail, telefonszám; a címek nem ismétlődhetnek.\n"""\nimport dataclasses\nimport re\nimport unicodedata\n\nHEADLINE_MAX, DESCRIPTION_MAX, PATH_MAX = 30, 90, 15\nSITELINK_TEXT_MAX, SITELINK_DESC_MAX, CALLOUT_MAX = 25, 35, 25\nKEYWORD_MAX_CHARS, KEYWORD_MAX_WORDS = 80, 10\nALLOWED_CAPS = {"PWA", "FCI", "SOS", "AI", "USA", "EU", "SMS", "GPS"}\n\n\n@dataclasses.dataclass\nclass Issue:\n    level: str          # error | warn\n    code: str\n    text: str\n    message: str\n\n    def __str__(self):\n        return f"[{\'HIBA\' if self.level == \'error\' else \'figyelmeztetés\'}] {self.message} → „{self.text}”"\n\n\ndef norm(s):\n    """Kisbetű, ékezet nélkül (ő→o, ű→u), egységes szóköz: az összehasonlításokhoz."""\n    s = unicodedata.normalize("NFKD", (s or "").lower())\n    s = "".join(ch for ch in s if not unicodedata.combining(ch))\n    return re.sub(r"\\s+", " ", s).strip()\n\n\ndef words(s):\n    return re.findall(r"[a-z0-9]+(?:[\'’-][a-z0-9]+)*", norm(s))\n\n\n# ------------------------------------------------------------------ állítások és tények\n# (tő, címke): a szöveg egy szava a tővel kezdődik → kényes állítás; tény nélkül tilos\nCLAIM_STEMS = [\n    ("ingyen", "ingyenesség"), ("regisztracio nelkul", "regisztráció nélkül"), ("regisztraciomentes", "regisztráció nélkül"),\n    ("garant", "garancia"), ("bizonyit", "bizonyítottság"), ("hatasos", "hatásosság"), ("hatekony", "hatékonyság"),\n    ("megbizhato", "megbízhatóság"), ("tudomanyos", "tudományosság"), ("szakerto", "szakértői állítás"),\n    ("olcso", "ár"), ("akcio", "ár"), ("kedvezmeny", "ár"), ("leertekel", "ár"), ("forint", "ár"),\n    ("allergi", "allergia"), ("hipoallergen", "allergia"), ("gyogy", "egészség"), ("betegseg", "egészség"),\n    ("egeszseg", "egészség"), ("terapi", "egészség"), ("elso", "egyediség"), ("egyetlen", "egyediség"),\n    ("szamu egy", "egyediség"), ("nr.1", "egyediség"), ("100%", "teljesség"),\n]\nSUPERLATIVE_OK = {"legalabb", "legfeljebb", "legutobb", "legkozelebb", "legalul", "legfelul", "legelejen", "legvegen"}\n\n\ndef claims_in(text):\n    """A szövegben talált kényes állítások: [(tő, címke)]."""\n    n = norm(text)\n    found = []\n    for stem, label in CLAIM_STEMS:\n        if " " in stem or "%" in stem or "." in stem:\n            if stem in n:\n                found.append((stem, label))\n        elif any(w.startswith(stem) for w in words(text)):\n            found.append((stem, label))\n    for w in words(text):\n        if len(w) >= 6 and re.fullmatch(r"leg[a-z]*bb(?:an|i|ik)?", w) and w not in SUPERLATIVE_OK:\n            found.append((w, "felsőfok"))\n    return found\n\n\ndef fact_tokens(facts):\n    out = set()\n    for f in facts or []:\n        for t in f.get("tokens", []) or []:\n            out.add(norm(t))\n    return out\n\n\ndef fact_numbers(facts):\n    out = set()\n    for f in facts or []:\n        for n in f.get("numbers", []) or []:\n            out.add(str(n).replace(",", "."))\n    return out\n\n\ndef check_facts(text, facts, *, where="szöveg"):\n    """Minden szám és kényes állítás igazolt-e a brief tényeivel."""\n    issues = []\n    nums = fact_numbers(facts)\n    for m in re.findall(r"\\d+(?:[.,]\\d+)?", text):\n        if m.replace(",", ".") not in nums:\n            issues.append(Issue("error", "unproven_number", text, f"A(z) {where} „{m}” számát nem igazolja tény a briefben (product.facts[].numbers)"))\n    tokens = fact_tokens(facts)\n    for stem, label in claims_in(text):\n        if label == "felsőfok":\n            supported = stem in tokens\n        else:\n            supported = any(t.startswith(stem) or stem.startswith(t) for t in tokens if len(t) >= 3)\n        if not supported:\n            issues.append(Issue("error", "unproven_claim", text,\n                                f"A(z) {where} „{label}” állítást tartalmaz, amit nem igazol tény a briefben (product.facts[].tokens)"))\n    return issues\n\n\n# ------------------------------------------------------------------ stílus- és szabályellenőrzés\n_URL = re.compile(r"(https?://|www\\.|\\b[a-z0-9-]+\\.(?:hu|com|org|net|eu)\\b)", re.I)\n_EMAIL = re.compile(r"\\S+@\\S+\\.\\S+")\n_PHONE = re.compile(r"(?<!\\d)(?:\\+?36|06)[\\s\\-/]?\\d{1,2}[\\s\\-/]?\\d{3}[\\s\\-/]?\\d{3,4}(?!\\d)")\n_REPEATED = re.compile(r"[!?.,;:]{2,}|\\.{3}|…")\n\n\ndef _has_emoji(s):\n    return any(unicodedata.category(ch) == "So" or ord(ch) > 0xFFFF for ch in s)\n\n\ndef check_style(text, kind):\n    """kind: headline | description | path | sitelink | callout"""\n    issues = []\n    limit = {"headline": HEADLINE_MAX, "description": DESCRIPTION_MAX, "path": PATH_MAX, "sitelink": SITELINK_TEXT_MAX,\n             "sitelink_desc": SITELINK_DESC_MAX, "callout": CALLOUT_MAX}[kind]\n    if len(text) > limit:\n        issues.append(Issue("error", "too_long", text, f"Túl hosszú ({len(text)} > {limit} karakter)"))\n    if not text.strip():\n        issues.append(Issue("error", "empty", text, "Üres szöveg"))\n        return issues\n    if text != text.strip() or "  " in text:\n        issues.append(Issue("warn", "spacing", text, "Felesleges szóköz"))\n    if kind == "headline" and "!" in text:\n        issues.append(Issue("error", "exclamation_in_headline", text, "A címben nem lehet felkiáltójel (a Google elutasítja)"))\n    if kind in ("description", "sitelink", "sitelink_desc", "callout") and text.count("!") > (1 if kind == "description" else 0):\n        issues.append(Issue("error", "exclamation", text, "Legfeljebb egy felkiáltójel engedélyezett, és csak a leírásban"))\n    if _REPEATED.search(text):\n        issues.append(Issue("error", "repeated_punctuation", text, "Ismétlődő írásjel vagy három pont nem engedélyezett"))\n    if _has_emoji(text):\n        issues.append(Issue("error", "emoji", text, "Emoji és szimbólum nem engedélyezett"))\n    if _URL.search(text) or _EMAIL.search(text) or _PHONE.search(text):\n        issues.append(Issue("error", "contact_in_text", text, "URL, e-mail-cím vagy telefonszám nem szerepelhet a szövegben"))\n    for w in re.findall(r"[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]{4,}", text):\n        if w.isupper() and w not in ALLOWED_CAPS:\n            issues.append(Issue("error", "all_caps", text, f"Csupa nagybetűs szó: {w} (rövidítés kivételével nem engedélyezett)"))\n            break\n    ws = text.split()\n    if len(ws) >= 3 and all(w[:1].isupper() for w in ws if w[:1].isalpha()):\n        issues.append(Issue("warn", "title_case", text, "Minden szó nagybetűvel kezdődik: mondatszerű írás kell"))\n    if kind == "path" and not re.fullmatch(r"[\\wÁÉÍÓÖŐÚÜŰáéíóöőúüű-]+", text):\n        issues.append(Issue("error", "bad_path", text, "Az útvonal csak betűt, számot, kötőjelet és aláhúzást tartalmazhat"))\n    return issues\n\n\ndef check_forbidden(text, forbidden_words, competitor_brands=()):\n    issues = []\n    ws = words(text)\n    n = norm(text)\n    for f in forbidden_words or []:\n        fw = norm(f)\n        if " " in fw:\n            hit = fw in n\n        else:\n            hit = any(w.startswith(fw) for w in ws)\n        if hit:\n            issues.append(Issue("error", "forbidden_word", text, f"Tiltott szó a briefben: {f}"))\n    for b in competitor_brands or []:\n        if norm(b) and norm(b) in n:\n            issues.append(Issue("error", "competitor_brand", text, f"Versenytárs-márkanév: {b}"))\n    return issues\n\n\ndef check_text(text, kind, brief, *, where=None):\n    """Egy szöveg teljes ellenőrzése: stílus + tiltott szavak + tények."""\n    facts = brief.get("product", {}).get("facts", [])\n    voice = brief.get("voice", {})\n    comp = brief.get("keywords", {}).get("competitor_brands", [])\n    issues = check_style(text, kind)\n    issues += check_forbidden(text, voice.get("forbidden_words", []), comp)\n    issues += check_facts(text, facts, where=where or kind)\n    return issues\n\n\ndef check_rsa(headlines, descriptions, path1, path2, brief):\n    """Reszponzív keresési hirdetés: darabszám, hossz, ismétlődés, tartalmi szabályok."""\n    issues = []\n    if not 3 <= len(headlines) <= 15:\n        issues.append(Issue("error", "headline_count", "", f"3–15 cím kell, most {len(headlines)} van"))\n    if not 2 <= len(descriptions) <= 4:\n        issues.append(Issue("error", "description_count", "", f"2–4 leírás kell, most {len(descriptions)} van"))\n    seen = {}\n    for h in headlines:\n        k = norm(h)\n        if k in seen:\n            issues.append(Issue("error", "duplicate_headline", h, "Ismétlődő cím"))\n        seen[k] = True\n        issues += check_text(h, "headline", brief, where="cím")\n    seen_d = set()\n    for d in descriptions:\n        if norm(d) in seen_d:\n            issues.append(Issue("error", "duplicate_description", d, "Ismétlődő leírás"))\n        seen_d.add(norm(d))\n        issues += check_text(d, "description", brief, where="leírás")\n    for p in (path1, path2):\n        if p:\n            issues += check_style(p, "path")\n    if path2 and not path1:\n        issues.append(Issue("error", "path_order", path2, "A 2. útvonalhoz kell 1. útvonal is"))\n    return issues\n\n\n# ------------------------------------------------------------------ kulcsszavak\n_KW_OK = re.compile(r"^[\\w\\s\'’+&.-]+$", re.UNICODE)\n\n\ndef tokens_of(text):\n    return words(text)\n\n\ndef _is_subsequence(needle, hay):\n    n = len(needle)\n    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))\n\n\ndef stem(w):\n    return w[:5]\n\n\ndef check_keyword(text, brief, *, require_close_to_core=False):\n    issues = []\n    if len(text) > KEYWORD_MAX_CHARS:\n        issues.append(Issue("error", "kw_too_long", text, f"A kulcsszó túl hosszú ({len(text)} > {KEYWORD_MAX_CHARS})"))\n    if len(text.split()) > KEYWORD_MAX_WORDS:\n        issues.append(Issue("error", "kw_too_many_words", text, f"A kulcsszó legfeljebb {KEYWORD_MAX_WORDS} szó lehet"))\n    if not text.strip() or not _KW_OK.match(text) or _URL.search(text):\n        issues.append(Issue("error", "kw_bad_chars", text, "A kulcsszó csak betűt, számot, szóközt, kötőjelet tartalmazhat (URL, írásjel nem)"))\n    issues += check_forbidden(text, [], brief.get("keywords", {}).get("competitor_brands", []))\n    if require_close_to_core:\n        core = {stem(w) for c in brief.get("keywords", {}).get("core", []) for w in words(c) if len(w) >= 4}\n        if not core & {stem(w) for w in words(text) if len(w) >= 4}:\n            issues.append(Issue("error", "kw_off_topic", text, "Az új kulcsszó nem kapcsolódik a magkifejezésekhez"))\n    return issues\n\n\ndef check_negative(text, brief, positives=()):\n    """Negatív kulcsszó: ne zárja ki a magkifejezéseket és a pozitív kulcsszavakat (pl. „kutya” tiltott negatív)."""\n    issues = []\n    base = check_keyword(text, brief)\n    issues += base\n    nt = tokens_of(text)\n    if not nt:\n        return issues\n    for c in list(brief.get("keywords", {}).get("core", [])) + list(positives):\n        if _is_subsequence(nt, tokens_of(c)):\n            issues.append(Issue("error", "negative_blocks_positive", text, f"Ez a negatív kulcsszó kizárná a(z) „{c}” kifejezést"))\n            break\n    return issues\n\n\ndef errors(issues):\n    return [i for i in issues if i.level == "error"]\n', 'packcheck': '"""Az Ads Pack tartalmi ellenőrzése – csak szabványos Python, I/O nélkül (a sémákat paraméterként kapja).\n\nUgyanezt a kódot használja a motor (pack.py) és az önálló bekötési validátor (validator/ads_pack_validator.py, a\ntools/build_validator.py állítja elő), így a másik projekt pontosan azt a szabályt látja, amit a motor érvényesít.\n"""\nimport hashlib\nimport json\nimport urllib.parse\n\n\n\ndef host_of(url):\n    return (urllib.parse.urlsplit(url).hostname or "").lower()\n\n\ndef content_hash(brief, creatives):\n    blob = json.dumps({"brief": brief, "creatives": creatives}, sort_keys=True, ensure_ascii=False).encode("utf-8")\n    return hashlib.sha256(blob).hexdigest()[:12]\n\n\ndef validate_brief(brief, schema, *, slug=None, allowed_hosts=None, umami_website_id=None):\n    """Séma + tartalmi ellenőrzés. (hibák, figyelmeztetések) – str listák.\n\n    slug / allowed_hosts / umami_website_id: a motor konfigjából (projects.toml); az önálló validátor a briefből vezeti le."""\n    errs = jsonschema_lite.validate(schema, brief)\n    warns = []\n    if errs:\n        return errs, warns\n    ids = [p["id"] for p in brief["landing_pages"]]\n    if len(set(ids)) != len(ids):\n        errs.append("landing_pages: az azonosítók nem lehetnek ismétlődők")\n    fids = [f["id"] for f in brief["product"]["facts"]]\n    if len(set(fids)) != len(fids):\n        errs.append("product.facts: az azonosítók nem lehetnek ismétlődők")\n    if slug and brief["project"]["slug"] != slug:\n        errs.append(f"project.slug ({brief[\'project\'][\'slug\']}) nem egyezik a motor konfigjával ({slug})")\n    if allowed_hosts is not None:\n        allowed = {h.lower() for h in allowed_hosts}\n        if host_of(brief["project"]["site"]) not in allowed:\n            errs.append(f"project.site gazdagépe nincs az engedélyezettek között: {host_of(brief[\'project\'][\'site\'])}")\n        for p in brief["landing_pages"]:\n            if host_of(p["url"]) not in allowed:\n                errs.append(f"landing_pages[{p[\'id\']}].url gazdagépe nincs az engedélyezettek között: {host_of(p[\'url\'])}")\n    wid = brief.get("tracking", {}).get("umami_website_id")\n    if wid and umami_website_id and wid != umami_website_id:\n        warns.append("tracking.umami_website_id eltér a motor konfigjában lévőtől (a konfig az irányadó)")\n    return errs, warns\n\n\ndef validate_creatives(creatives, brief, schema):\n    """Séma + a hirdetésszövegek, kulcsszavak, hivatkozások tartalmi ellenőrzése. (hibák, figyelmeztetések) – str listák."""\n    errs = jsonschema_lite.validate(schema, creatives)\n    warns = []\n    if errs:\n        return errs, warns\n    landing_ids = {p["id"] for p in brief["landing_pages"]}\n    ids = [a["id"] for a in creatives["adsets"]]\n    if len(set(ids)) != len(ids):\n        errs.append("adsets: az azonosítók nem lehetnek ismétlődők")\n    all_positive = []\n    for a in creatives["adsets"]:\n        all_positive += [k["text"] for k in a["keywords"]]\n\n    def add(issues, tag):\n        for i in issues:\n            (errs if i.level == "error" else warns).append(f"{tag}: {i}")\n\n    for a in creatives["adsets"]:\n        tag = f"adsets[{a[\'id\']}]"\n        if a["landing"] not in landing_ids:\n            errs.append(f"{tag}.landing ({a[\'landing\']}) nincs a brief landing_pages között")\n        add(validators.check_rsa(a["headlines"], a["descriptions"], a.get("path1", ""), a.get("path2", ""), brief), tag)\n        positives = [k["text"] for k in a["keywords"]]\n        for k in a["keywords"]:\n            add(validators.check_keyword(k["text"], brief), f"{tag}.keywords")\n        for n in a.get("negatives", []):\n            add(validators.check_negative(n, brief, positives), f"{tag}.negatives")\n    for n in brief["keywords"].get("negatives", []):\n        add(validators.check_negative(n, brief, all_positive), "brief.keywords.negatives")\n    seen = set()\n    for s in creatives.get("sitelinks", []):\n        if s["landing"] not in landing_ids:\n            errs.append(f"sitelinks[{s[\'text\']}].landing ({s[\'landing\']}) nincs a brief landing_pages között")\n        if validators.norm(s["text"]) in seen:\n            errs.append(f"sitelinks: ismétlődő szöveg: {s[\'text\']}")\n        seen.add(validators.norm(s["text"]))\n        for text, kind in ((s["text"], "sitelink"), (s.get("description1", ""), "sitelink_desc"), (s.get("description2", ""), "sitelink_desc")):\n            if text:\n                add(validators.check_text(text, kind, brief, where="hivatkozás"), "sitelinks")\n    for c in creatives.get("callouts", []):\n        add(validators.check_text(c, "callout", brief, where="kiemelés"), "callouts")\n    img_ids = [i["id"] for i in creatives.get("images", [])]\n    if len(set(img_ids)) != len(img_ids):\n        errs.append("images: az azonosítók nem lehetnek ismétlődők")\n    return errs, warns\n'}


def _load_modules():
    mods = {}
    for name in MODULE_ORDER:
        m = types.ModuleType(name)
        m.__dict__.update(mods)
        exec(compile(SOURCES[name], "<" + name + ">", "exec"), m.__dict__)
        mods[name] = m
    return mods


_MODS = _load_modules()
jsonschema_lite, validators, packcheck = _MODS["jsonschema_lite"], _MODS["validators"], _MODS["packcheck"]

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
