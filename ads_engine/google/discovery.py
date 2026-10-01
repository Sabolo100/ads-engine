"""A Google Ads API hivatalos REST-leíró dokumentuma (discovery) alapú ellenőrzés.

A leíró (https://googleads.googleapis.com/$discovery/rest?version=v25) minden kérés- és erőforrás-szerkezetet tartalmaz.
Ezzel élesítés előtt, hálózat nélkül ellenőrizhető, hogy a kérésünkben nincs ismeretlen mező, hibás típus vagy érvénytelen
enum-érték, és hogy nem írtunk csak kimeneti (readOnly) mezőt. A leíró ~3 MB: a `.cache/` mappában tartjuk, az `api-check`
frissíti.

Az ellenőrzés a protobuf-JSON szabályait követi: lowerCamelCase mezőnevek, az int64 szövegként vagy számként is jó,
a `byte` mező base64.
"""
import base64
import binascii
import datetime as dt
import json
import os
import pathlib
import re

from .. import http

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CACHE = pathlib.Path(os.environ["ADS_CACHE_DIR"]) if os.environ.get("ADS_CACHE_DIR") else ROOT / ".cache"     # Docker: /data/cache
DISCOVERY_URL = "https://googleads.googleapis.com/$discovery/rest?version={version}"

# Ismert lejáratok (a Google sunset-dates oldala szerint; a v25: 2026. július – 2027. augusztus)
KNOWN_SUNSETS = {"v25": "2027-08-31"}

_ALLOWED_READONLY = {"resourceName"}      # a resourceName létrehozáskor is megadható (ideiglenes azonosító)
# A leíró „Output only”-nak jelöli, de létrehozáskor a `data` mezővel írható (a hivatalos példák is így töltenek fel képet:
# asset.image_asset.data = bájtok). Ezt a kivételt a próbák az éles Google ellen is igazolják.
_WRITABLE_EXCEPTIONS = {("Resources__Asset", "imageAsset")}


class Discovery:
    def __init__(self, doc):
        self.doc = doc
        self.schemas = doc["schemas"]
        self.version = doc.get("version", "")
        self.revision = doc.get("revision", "")
        self.prefix = "GoogleAdsGoogleads" + self.version.upper()          # pl. GoogleAdsGoogleadsV25

    # ------------------------------------------------------------------ betöltés
    @classmethod
    def path_for(cls, version):
        return CACHE / f"googleads_{version}.json"

    @classmethod
    def load(cls, version="v25", refresh=False, request=http.request):
        p = cls.path_for(version)
        if p.exists() and not refresh:
            return cls(json.loads(p.read_text(encoding="utf-8")))
        r = request("GET", DISCOVERY_URL.format(version=version), timeout=120, retries=2)
        doc = r.json()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc), encoding="utf-8")
        return cls(doc)

    @classmethod
    def load_cached(cls, version="v25"):
        """Csak a helyi gyorsítótárból (hálózat nélkül); ha nincs, None."""
        p = cls.path_for(version)
        return cls(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None

    # ------------------------------------------------------------------ séma-keresés
    def schema_name(self, short):
        """'Campaign' → 'GoogleAdsGoogleadsV25Resources__Campaign' (Resources_, Services_, Common_ előtaggal is keres)."""
        for pre in ("Resources__", "Services__", "Common__", "Enums__", "Errors__"):
            n = self.prefix + pre + short
            if n in self.schemas:
                return n
        if short in self.schemas:
            return short
        raise KeyError(short)

    def schema(self, short):
        return self.schemas[self.schema_name(short)]

    # ------------------------------------------------------------------ ellenőrzés
    def validate(self, short, value, forbid_readonly=True):
        """A value megfelel-e a short nevű sémának? Hibák listája (üres = rendben)."""
        errs = []
        self._check({"$ref": self.schema_name(short)}, value, "$", errs, forbid_readonly)
        return errs

    def validate_mutate_request(self, body):
        errs = self.validate("MutateGoogleAdsRequest", body)
        for i, op in enumerate(body.get("mutateOperations", [])):
            if not isinstance(op, dict) or len(op) != 1:
                errs.append(f"$.mutateOperations[{i}]: pontosan egy művelet-kulcs kell (pl. campaignOperation)")
        return errs

    def _check(self, sch, v, path, errs, forbid_ro):
        name = ""
        if "$ref" in sch:
            name = sch["$ref"].replace(self.prefix, "", 1)
            sch = self.schemas[sch["$ref"]]
        t = sch.get("type")
        fmt = sch.get("format")
        if t == "object" or "properties" in sch:
            if not isinstance(v, dict):
                errs.append(f"{path}: objektumot vártunk, ez: {type(v).__name__}")
                return
            props = sch.get("properties", {})
            for k, val in v.items():
                if k in props:
                    sub = props[k]
                    if forbid_ro and sub.get("readOnly") and k not in _ALLOWED_READONLY and (name, k) not in _WRITABLE_EXCEPTIONS:
                        errs.append(f"{path}.{k}: csak kimeneti (readOnly) mező, íráskor nem adható meg")
                        continue
                    self._check(sub, val, f"{path}.{k}", errs, forbid_ro)
                elif "additionalProperties" in sch:
                    self._check(sch["additionalProperties"], val, f"{path}.{k}", errs, forbid_ro)
                else:
                    errs.append(f"{path}.{k}: ismeretlen mező")
            return
        if t == "array":
            if not isinstance(v, list):
                errs.append(f"{path}: listát vártunk, ez: {type(v).__name__}")
                return
            for i, item in enumerate(v):
                self._check(sch.get("items", {}), item, f"{path}[{i}]", errs, forbid_ro)
            return
        if "enum" in sch:
            if v not in sch["enum"]:
                errs.append(f"{path}: érvénytelen érték {v!r} (lehet: {', '.join(sch['enum'][:8])}{'…' if len(sch['enum']) > 8 else ''})")
            return
        if t == "string":
            if fmt in ("int64", "uint64"):
                if not (isinstance(v, int) and not isinstance(v, bool)) and not (isinstance(v, str) and re.fullmatch(r"-?\d+", v)):
                    errs.append(f"{path}: egész számot (vagy számjegyes szöveget) vártunk, ez: {v!r}")
            elif fmt == "byte":
                try:
                    base64.b64decode(v, validate=True)
                except (binascii.Error, ValueError, TypeError):
                    errs.append(f"{path}: érvényes base64 szöveget vártunk")
            elif not isinstance(v, str):
                errs.append(f"{path}: szöveget vártunk, ez: {type(v).__name__}")
        elif t == "integer":
            if not isinstance(v, int) or isinstance(v, bool):
                errs.append(f"{path}: egész számot vártunk, ez: {v!r}")
        elif t == "number":
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                errs.append(f"{path}: számot vártunk, ez: {v!r}")
        elif t == "boolean":
            if not isinstance(v, bool):
                errs.append(f"{path}: logikai értéket vártunk, ez: {v!r}")


# ------------------------------------------------------------------ api-check
def days_until(date_iso, today=None):
    today = today or dt.date.today()
    return (dt.date.fromisoformat(date_iso) - today).days


def api_check(version="v25", refresh=True, request=http.request, today=None):
    """Elérhető-e a leíró, változott-e a revízió, közeleg-e a lejárat, van-e újabb főverzió. Dict, nem dob kivételt."""
    out = {"version": version, "ok": True, "notes": []}
    local = Discovery.load_cached(version)
    try:
        remote = Discovery.load(version, refresh=refresh, request=request)
    except Exception as e:      # hálózati hiba: a helyi másolat marad
        out["ok"] = False
        out["notes"].append(f"A leíró-dokumentum nem tölthető le: {e}")
        remote = local
    if remote:
        out["revision"] = remote.revision
        if local and local.revision != remote.revision:
            out["notes"].append(f"A leíró revíziója változott: {local.revision} → {remote.revision} (nézd át a kiadási jegyzetet)")
    sunset = KNOWN_SUNSETS.get(version)
    if sunset:
        left = days_until(sunset, today)
        out["sunset"], out["days_left"] = sunset, left
        if left < 120:
            out["notes"].append(f"A {version} {left} nap múlva lejár ({sunset}) – válts újabb verzióra")
    n = int(version[1:])
    for nxt in (n + 1, n + 2):
        try:
            request("GET", DISCOVERY_URL.format(version=f"v{nxt}"), timeout=30, retries=0)
            out.setdefault("newer", []).append(f"v{nxt}")
        except http.HttpError:
            pass
    if out.get("newer"):
        out["notes"].append(f"Újabb API-verzió elérhető: {', '.join(out['newer'])} (a motor a {version}-ön marad, amíg át nem állítod)")
    return out
