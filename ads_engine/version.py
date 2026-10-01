"""Verzió és build-azonosító.

A verzió egy helyen él: a repó gyökerének VERSION fájlja (FŐ.MELLÉK.JAVÍTÁS). A build-azonosító a kód
tartalom-hash-e, így egy futó konténerről is látszik, pontosan melyik kód fut (levél lábléce, /healthz, --version).
"""
import hashlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def version():
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def build_id():
    h = hashlib.sha256()
    files = sorted(list((ROOT / "ads_engine").rglob("*.py")) + list((ROOT / "schema").glob("*.json")) + [ROOT / "VERSION"])
    for p in files:
        if not p.is_file():
            continue
        h.update(p.relative_to(ROOT).as_posix().encode("utf-8"))
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))   # a sorvég (Windows/Linux) ne változtassa meg az azonosítót
    return h.hexdigest()[:7]


def label():
    return f"v{version()} · {build_id()}"
