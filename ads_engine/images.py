"""Képek a Google Ads méreteire (kép-eszközök): vágás/kitöltés, tömörítés ≤ 5 MB, tartalom-hash alapú név.

Méretek (a Google szerint): fekvő 1,91:1 (ajánlott 1200×628, min. 600×314) · négyzetes 1:1 (1200×1200, min. 300×300) ·
álló 4:5 (960×1200, min. 480×600) · logó 1:1 (min. 128×128). Az arány-tűrés ±1 %, a fájl legfeljebb 5 MB (JPEG vagy PNG).
Az M4 mérföldkőben ide kerül az AI-képgenerálás (gpt-image-2, hetente ≤ 10) és a szöveges átfedés is.
"""
import dataclasses
import hashlib
import io

from PIL import Image, ImageFilter, ImageOps

SPECS = {
    "landscape": {"ratio": 1.91, "size": (1200, 628), "min": (600, 314), "label": "1,91:1"},
    "square": {"ratio": 1.0, "size": (1200, 1200), "min": (300, 300), "label": "1:1"},
    "portrait": {"ratio": 0.8, "size": (960, 1200), "min": (480, 600), "label": "4:5"},
    "logo": {"ratio": 1.0, "size": (1200, 1200), "min": (128, 128), "label": "logó 1:1"},
}
RATIO_KIND = {"1.91:1": "landscape", "1:1": "square", "4:5": "portrait"}
MAX_BYTES = 5 * 1024 * 1024 - 1024            # kis ráhagyás a Google 5 MB-os korlátjához
TOLERANCE = 0.01


class ImageError(Exception):
    pass


@dataclasses.dataclass
class Prepared:
    data: bytes
    mime: str
    width: int
    height: int
    kind: str
    sha256: str
    mode: str                 # exact | cover | pad
    name: str                 # tartalom-hash alapú: azonos kép → azonos név (nincs duplikált eszköz)


def classify(width, height):
    """A méretből az eszköz-fajta (landscape/square/portrait) ±1 % tűréssel, vagy None."""
    r = width / height
    for kind in ("landscape", "square", "portrait"):
        if abs(r - SPECS[kind]["ratio"]) / SPECS[kind]["ratio"] <= TOLERANCE:
            return kind
    return None


def open_image(data):
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise ImageError(f"a kép nem olvasható: {e}") from None
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def _cover(im, size, focus=(0.5, 0.5)):
    return ImageOps.fit(im, size, Image.LANCZOS, centering=focus)


def _edge_color(im):
    """A kép szélének (átlagos) színe, ha a szél egyöntetű (pl. krém háttér), különben None."""
    w, h = im.size
    strip = max(2, min(w, h) // 50)
    pixels = []
    for box in ((0, 0, w, strip), (0, h - strip, w, h), (0, 0, strip, h), (w - strip, 0, w, h)):
        raw = im.convert("RGB").crop(box).resize((24, 24)).tobytes()
        pixels += [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    channels = list(zip(*pixels))
    means = tuple(int(sum(c) / len(c)) for c in channels)
    spread = max((sum((x - m) ** 2 for x in c) / len(c)) ** 0.5 for c, m in zip(channels, means))
    return means if spread < 14 else None


def _pad(im, size):
    """Kitöltés: egyöntetű szélű képnél (krém háttér) a szél színe, különben a kép elmosott változata; semmi nem vágódik le."""
    fg = ImageOps.contain(im, size, Image.LANCZOS)
    edge = _edge_color(im)
    bg = Image.new("RGB", size, edge) if edge else _cover(im, size).filter(ImageFilter.GaussianBlur(radius=max(size) / 40))
    bg.paste(fg, ((size[0] - fg.width) // 2, (size[1] - fg.height) // 2))
    return bg


def prepare(data, kind, *, mode="auto", focus=(0.5, 0.5), max_bytes=MAX_BYTES):
    """Forrásképből Google-méretű, ≤ 5 MB-os JPEG. mode: auto | cover | pad. ImageError, ha a forrás túl kicsi."""
    if kind not in SPECS:
        raise ImageError(f"ismeretlen képfajta: {kind}")
    spec = SPECS[kind]
    im = open_image(data)
    mw, mh = spec["min"]
    ratio_src = im.width / im.height
    target = spec["size"]
    # túl kicsi forrás: a minimumot a vágás után is el kell érni
    if mode != "pad":
        crop_w, crop_h = (im.height * spec["ratio"], im.height) if ratio_src > spec["ratio"] else (im.width, im.width / spec["ratio"])
    else:
        crop_w, crop_h = im.width, im.height
    if crop_w < mw or crop_h < mh:
        raise ImageError(f"a kép túl kicsi ({im.width}×{im.height}); legalább {mw}×{mh} pixel kell a vágás után ({spec['label']})")
    diff = abs(ratio_src - spec["ratio"]) / spec["ratio"]
    if diff <= TOLERANCE:
        used = "exact"
        out = im.resize(target, Image.LANCZOS) if im.size != target else im
    else:
        lost = 1 - (min(ratio_src, spec["ratio"]) / max(ratio_src, spec["ratio"]))     # a levágott terület aránya
        used = mode if mode != "auto" else ("cover" if lost <= 0.30 else "pad")
        out = _cover(im, target, focus) if used == "cover" else _pad(im, target)
    quality, buf = 92, io.BytesIO()
    while True:
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        if buf.tell() <= max_bytes:
            break
        if quality > 60:
            quality -= 8
            continue
        # a minőség már nem csökkenthető: kicsinyítés, amíg a Google minimumát el nem érnénk
        nw, nh = int(out.width * 0.85), int(out.height * 0.85)
        if nw < mw or nh < mh:
            raise ImageError(f"a kép tömörítve sem fér a {max_bytes // 1024} KB-os korlátba")
        out = out.resize((nw, nh), Image.LANCZOS)
    raw = buf.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    return Prepared(raw, "image/jpeg", out.width, out.height, kind, sha, used, f"{kind}-{sha[:12]}")


def kind_for(ratio_label, width, height):
    """A creatives.json `ratio` mezőjéből (vagy a méretből) az eszköz-fajta; ha a méret nem illik, a legközelebbi."""
    if ratio_label in RATIO_KIND:
        return RATIO_KIND[ratio_label]
    k = classify(width, height)
    if k:
        return k
    r = width / height
    return min(("landscape", "square", "portrait"), key=lambda kk: abs(r - SPECS[kk]["ratio"]))
