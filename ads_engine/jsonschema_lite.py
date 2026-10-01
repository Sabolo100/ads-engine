"""Kis JSON-Schema ellenőrző (csak szabványos Python) – a brief és a kreatív-csomag sémáihoz.

A használt részhalmaz: type (több típus is), required, properties, additionalProperties (bool vagy séma), items, minItems,
maxItems, uniqueItems, enum, const, minLength, maxLength, pattern, minimum, maximum, anyOf, oneOf, $ref (#/$defs/…).
A hibák magyarul, JSON-útvonallal (pl. $.product.facts[2].text) jönnek, hogy a másik projekt gyorsan javíthassa.
Ez a fájl önállóan is használható (a bekötési validátor ezt ágyazza be).
"""
import re


def validate(schema, value, root=None):
    """Hibák listája (üres = megfelel)."""
    errs = []
    _check(schema, value, "$", errs, root or schema)
    return errs


def _types(t):
    return t if isinstance(t, list) else [t]


def _is(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "null":
        return value is None
    return True


TYPE_HU = {"object": "objektum", "array": "lista", "string": "szöveg", "boolean": "igaz/hamis érték", "integer": "egész szám",
           "number": "szám", "null": "üres érték"}


def _name(value):
    for t in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _is(value, t):
            return TYPE_HU[t]
    return type(value).__name__


def _resolve(ref, root):
    if not ref.startswith("#/"):
        raise ValueError(f"nem támogatott $ref: {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _check(schema, v, path, errs, root):
    if "$ref" in schema:
        _check(_resolve(schema["$ref"], root), v, path, errs, root)
        return
    if "const" in schema and v != schema["const"]:
        errs.append(f"{path}: az értéknek ennek kell lennie: {schema['const']!r}")
        return
    if "enum" in schema and v not in schema["enum"]:
        errs.append(f"{path}: érvénytelen érték {v!r} (lehet: {', '.join(map(str, schema['enum']))})")
        return
    if "type" in schema:
        ts = _types(schema["type"])
        if not any(_is(v, t) for t in ts):
            errs.append(f"{path}: {' vagy '.join(TYPE_HU.get(t, t) for t in ts)} kell, ez: {_name(v)}")
            return
    for key in ("anyOf", "oneOf"):
        if key in schema:
            ok = [s for s in schema[key] if not validate(s, v, root)]
            if (key == "anyOf" and not ok) or (key == "oneOf" and len(ok) != 1):
                errs.append(f"{path}: nem felel meg a megadott változatok egyikének sem")
    if isinstance(v, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in v:
                errs.append(f"{path}: hiányzik a kötelező mező: {req}")
        for k, val in v.items():
            if k in props:
                _check(props[k], val, f"{path}.{k}", errs, root)
            else:
                ap = schema.get("additionalProperties", True)
                if ap is False:
                    errs.append(f"{path}.{k}: ismeretlen mező")
                elif isinstance(ap, dict):
                    _check(ap, val, f"{path}.{k}", errs, root)
    elif isinstance(v, list):
        if "minItems" in schema and len(v) < schema["minItems"]:
            errs.append(f"{path}: legalább {schema['minItems']} elem kell, most {len(v)} van")
        if "maxItems" in schema and len(v) > schema["maxItems"]:
            errs.append(f"{path}: legfeljebb {schema['maxItems']} elem lehet, most {len(v)} van")
        if schema.get("uniqueItems") and len({repr(x) for x in v}) != len(v):
            errs.append(f"{path}: az elemek nem ismétlődhetnek")
        if "items" in schema:
            for i, item in enumerate(v):
                _check(schema["items"], item, f"{path}[{i}]", errs, root)
    elif isinstance(v, str):
        if "minLength" in schema and len(v) < schema["minLength"]:
            errs.append(f"{path}: legalább {schema['minLength']} karakter kell, most {len(v)}")
        if "maxLength" in schema and len(v) > schema["maxLength"]:
            errs.append(f"{path}: legfeljebb {schema['maxLength']} karakter lehet, most {len(v)}")
        if "pattern" in schema and not re.search(schema["pattern"], v):
            errs.append(f"{path}: nem megfelelő formátum ({schema['pattern']}): {v[:60]!r}")
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            errs.append(f"{path}: legalább {schema['minimum']} kell, ez: {v}")
        if "maximum" in schema and v > schema["maximum"]:
            errs.append(f"{path}: legfeljebb {schema['maximum']} lehet, ez: {v}")
