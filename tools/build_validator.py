# -*- coding: utf-8 -*-
"""Az önálló, függőség nélküli Ads Pack validátor előállítása: validator/ads_pack_validator.py.

A fájlba beágyazódik a motor jsonschema_lite, validators és packcheck modulja, a két séma és a főprogram, így a másik projektnek
csak ezt az EGY fájlt kell letöltenie és `python ads_pack_validator.py <brief-url>` néven futtatnia (Python 3.9+).
Futtatás: python tools/build_validator.py        (a docs és a próbák erre a fájlra hivatkoznak)
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ads_engine import version  # noqa: E402

OUT = ROOT / "validator" / "ads_pack_validator.py"
MODULES = ("jsonschema_lite", "validators", "packcheck")

HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ads Engine – Ads Pack ellenőrző (önálló, külső csomag nélküli). Verzió: {version}

GENERÁLT FÁJL – ne szerkeszd. Forrás: ads-engine / tools/build_validator.py (a motor jsonschema_lite, validators és packcheck
moduljából, a sémákból és a tools/validator_main.py-ból). Ugyanazt a szabályt érvényesíti, mint a motor.

Használat:
  python ads_pack_validator.py https://pelda.hu/ads/brief.json            séma + tartalom (szövegek, tények, kulcsszavak)
  python ads_pack_validator.py https://pelda.hu/ads/brief.json --online   + nyitóoldalak (UTM megmarad-e), képek (méret, arány)
  python ads_pack_validator.py ads/brief.json                             helyi fájl
Kilépési kód: 0 = rendben, 1 = hiba van, 2 = hibás használat. Python 3.9+, csak szabványos könyvtár.
"""
import sys
import types

VERSION = {version!r}
'''

LOADER = '''

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

'''


def build(out=OUT):
    sources = {}
    for name in MODULES:
        src = (ROOT / "ads_engine" / f"{name}.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        src = src.replace("from . import jsonschema_lite, validators\n", "")
        if "from ." in src or "import ads_engine" in src:
            raise SystemExit(f"{name}: relatív import maradt a beágyazandó forrásban")
        sources[name] = src
    schemas = {"brief": json.loads((ROOT / "schema" / "ads-brief.schema.json").read_text(encoding="utf-8")),
               "creatives": json.loads((ROOT / "schema" / "creative-pack.schema.json").read_text(encoding="utf-8"))}
    main_src = (ROOT / "tools" / "validator_main.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    parts = [HEADER.format(version=version.label()),
             "MODULE_ORDER = " + repr(MODULES) + "\n",
             "SCHEMAS = json.loads(" + repr(json.dumps(schemas, ensure_ascii=False)) + ")\n",
             "SOURCES = " + repr(sources) + "\n",
             LOADER, main_src]
    text = "".join(parts)
    # a SCHEMAS-hoz kell a json: a fejléc utáni import-blokk elé
    text = text.replace("import sys\nimport types\n", "import json\nimport sys\nimport types\n", 1)
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


if __name__ == "__main__":
    p = build()
    print(f"megírva: {p} ({p.stat().st_size // 1024} KB)")
