"""Szöveg- és kulcsszó-ellenőrzők – KÓDBAN, nem promptban.

Elv: a hirdetésszöveg minden számát és kényes állítását (ingyenes, regisztráció nélkül, garantált, allergia, ár, felsőfok…)
a brief tényei (`product.facts`) igazolják. Amit nem igazol tény, azt a motor nem teszi ki: nem számít, ki írta a szöveget
(ember, a projekt skillje vagy az AI). Ugyanez a fájl önállóan is használható (a bekötési validátor ezt ágyazza be).

Google szerkesztési szabályok, amelyeket itt kódolunk: cím ≤ 30, leírás ≤ 90, útvonal ≤ 15 karakter; cím nem tartalmazhat
felkiáltójelet, a leírásban legfeljebb egy lehet; nincs ismétlődő írásjel, három pont, emoji; nincs csupa nagybetűs szó
(rövidítés kivételével); nincs URL, e-mail, telefonszám; a címek nem ismétlődhetnek.
"""
import dataclasses
import re
import unicodedata

HEADLINE_MAX, DESCRIPTION_MAX, PATH_MAX = 30, 90, 15
SITELINK_TEXT_MAX, SITELINK_DESC_MAX, CALLOUT_MAX = 25, 35, 25
KEYWORD_MAX_CHARS, KEYWORD_MAX_WORDS = 80, 10
ALLOWED_CAPS = {"PWA", "FCI", "SOS", "AI", "USA", "EU", "SMS", "GPS"}


@dataclasses.dataclass
class Issue:
    level: str          # error | warn
    code: str
    text: str
    message: str

    def __str__(self):
        return f"[{'HIBA' if self.level == 'error' else 'figyelmeztetés'}] {self.message} → „{self.text}”"


def norm(s):
    """Kisbetű, ékezet nélkül (ő→o, ű→u), egységes szóköz: az összehasonlításokhoz."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def words(s):
    return re.findall(r"[a-z0-9]+(?:['’-][a-z0-9]+)*", norm(s))


# ------------------------------------------------------------------ állítások és tények
# (tő, címke): a szöveg egy szava a tővel kezdődik → kényes állítás; tény nélkül tilos
CLAIM_STEMS = [
    ("ingyen", "ingyenesség"), ("regisztracio nelkul", "regisztráció nélkül"), ("regisztraciomentes", "regisztráció nélkül"),
    ("garant", "garancia"), ("bizonyit", "bizonyítottság"), ("hatasos", "hatásosság"), ("hatekony", "hatékonyság"),
    ("megbizhato", "megbízhatóság"), ("tudomanyos", "tudományosság"), ("szakerto", "szakértői állítás"),
    ("olcso", "ár"), ("akcio", "ár"), ("kedvezmeny", "ár"), ("leertekel", "ár"), ("forint", "ár"),
    ("allergi", "allergia"), ("hipoallergen", "allergia"), ("gyogy", "egészség"), ("betegseg", "egészség"),
    ("egeszseg", "egészség"), ("terapi", "egészség"), ("elso", "egyediség"), ("egyetlen", "egyediség"),
    ("szamu egy", "egyediség"), ("nr.1", "egyediség"), ("100%", "teljesség"),
]
SUPERLATIVE_OK = {"legalabb", "legfeljebb", "legutobb", "legkozelebb", "legalul", "legfelul", "legelejen", "legvegen"}


def claims_in(text):
    """A szövegben talált kényes állítások: [(tő, címke)]."""
    n = norm(text)
    found = []
    for stem, label in CLAIM_STEMS:
        if " " in stem or "%" in stem or "." in stem:
            if stem in n:
                found.append((stem, label))
        elif any(w.startswith(stem) for w in words(text)):
            found.append((stem, label))
    for w in words(text):
        if len(w) >= 6 and re.fullmatch(r"leg[a-z]*bb(?:an|i|ik)?", w) and w not in SUPERLATIVE_OK:
            found.append((w, "felsőfok"))
    return found


def fact_tokens(facts):
    out = set()
    for f in facts or []:
        for t in f.get("tokens", []) or []:
            out.add(norm(t))
    return out


def fact_numbers(facts):
    out = set()
    for f in facts or []:
        for n in f.get("numbers", []) or []:
            out.add(str(n).replace(",", "."))
    return out


def check_facts(text, facts, *, where="szöveg"):
    """Minden szám és kényes állítás igazolt-e a brief tényeivel."""
    issues = []
    nums = fact_numbers(facts)
    for m in re.findall(r"\d+(?:[.,]\d+)?", text):
        if m.replace(",", ".") not in nums:
            issues.append(Issue("error", "unproven_number", text, f"A(z) {where} „{m}” számát nem igazolja tény a briefben (product.facts[].numbers)"))
    tokens = fact_tokens(facts)
    for stem, label in claims_in(text):
        if label == "felsőfok":
            supported = stem in tokens
        else:
            supported = any(t.startswith(stem) or stem.startswith(t) for t in tokens if len(t) >= 3)
        if not supported:
            issues.append(Issue("error", "unproven_claim", text,
                                f"A(z) {where} „{label}” állítást tartalmaz, amit nem igazol tény a briefben (product.facts[].tokens)"))
    return issues


# ------------------------------------------------------------------ stílus- és szabályellenőrzés
_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(?:hu|com|org|net|eu)\b)", re.I)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_PHONE = re.compile(r"(?<!\d)(?:\+?36|06)[\s\-/]?\d{1,2}[\s\-/]?\d{3}[\s\-/]?\d{3,4}(?!\d)")
_REPEATED = re.compile(r"[!?.,;:]{2,}|\.{3}|…")


def _has_emoji(s):
    return any(unicodedata.category(ch) == "So" or ord(ch) > 0xFFFF for ch in s)


def check_style(text, kind):
    """kind: headline | description | path | sitelink | callout"""
    issues = []
    limit = {"headline": HEADLINE_MAX, "description": DESCRIPTION_MAX, "path": PATH_MAX, "sitelink": SITELINK_TEXT_MAX,
             "sitelink_desc": SITELINK_DESC_MAX, "callout": CALLOUT_MAX}[kind]
    if len(text) > limit:
        issues.append(Issue("error", "too_long", text, f"Túl hosszú ({len(text)} > {limit} karakter)"))
    if not text.strip():
        issues.append(Issue("error", "empty", text, "Üres szöveg"))
        return issues
    if text != text.strip() or "  " in text:
        issues.append(Issue("warn", "spacing", text, "Felesleges szóköz"))
    if kind == "headline" and "!" in text:
        issues.append(Issue("error", "exclamation_in_headline", text, "A címben nem lehet felkiáltójel (a Google elutasítja)"))
    if kind in ("description", "sitelink", "sitelink_desc", "callout") and text.count("!") > (1 if kind == "description" else 0):
        issues.append(Issue("error", "exclamation", text, "Legfeljebb egy felkiáltójel engedélyezett, és csak a leírásban"))
    if _REPEATED.search(text):
        issues.append(Issue("error", "repeated_punctuation", text, "Ismétlődő írásjel vagy három pont nem engedélyezett"))
    if _has_emoji(text):
        issues.append(Issue("error", "emoji", text, "Emoji és szimbólum nem engedélyezett"))
    if _URL.search(text) or _EMAIL.search(text) or _PHONE.search(text):
        issues.append(Issue("error", "contact_in_text", text, "URL, e-mail-cím vagy telefonszám nem szerepelhet a szövegben"))
    for w in re.findall(r"[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]{4,}", text):
        if w.isupper() and w not in ALLOWED_CAPS:
            issues.append(Issue("error", "all_caps", text, f"Csupa nagybetűs szó: {w} (rövidítés kivételével nem engedélyezett)"))
            break
    ws = text.split()
    if len(ws) >= 3 and all(w[:1].isupper() for w in ws if w[:1].isalpha()):
        issues.append(Issue("warn", "title_case", text, "Minden szó nagybetűvel kezdődik: mondatszerű írás kell"))
    if kind == "path" and not re.fullmatch(r"[\wÁÉÍÓÖŐÚÜŰáéíóöőúüű-]+", text):
        issues.append(Issue("error", "bad_path", text, "Az útvonal csak betűt, számot, kötőjelet és aláhúzást tartalmazhat"))
    return issues


def check_forbidden(text, forbidden_words, competitor_brands=()):
    issues = []
    ws = words(text)
    n = norm(text)
    for f in forbidden_words or []:
        fw = norm(f)
        if " " in fw:
            hit = fw in n
        else:
            hit = any(w.startswith(fw) for w in ws)
        if hit:
            issues.append(Issue("error", "forbidden_word", text, f"Tiltott szó a briefben: {f}"))
    for b in competitor_brands or []:
        if norm(b) and norm(b) in n:
            issues.append(Issue("error", "competitor_brand", text, f"Versenytárs-márkanév: {b}"))
    return issues


def check_text(text, kind, brief, *, where=None):
    """Egy szöveg teljes ellenőrzése: stílus + tiltott szavak + tények."""
    facts = brief.get("product", {}).get("facts", [])
    voice = brief.get("voice", {})
    comp = brief.get("keywords", {}).get("competitor_brands", [])
    issues = check_style(text, kind)
    issues += check_forbidden(text, voice.get("forbidden_words", []), comp)
    issues += check_facts(text, facts, where=where or kind)
    return issues


def check_rsa(headlines, descriptions, path1, path2, brief):
    """Reszponzív keresési hirdetés: darabszám, hossz, ismétlődés, tartalmi szabályok."""
    issues = []
    if not 3 <= len(headlines) <= 15:
        issues.append(Issue("error", "headline_count", "", f"3–15 cím kell, most {len(headlines)} van"))
    if not 2 <= len(descriptions) <= 4:
        issues.append(Issue("error", "description_count", "", f"2–4 leírás kell, most {len(descriptions)} van"))
    seen = {}
    for h in headlines:
        k = norm(h)
        if k in seen:
            issues.append(Issue("error", "duplicate_headline", h, "Ismétlődő cím"))
        seen[k] = True
        issues += check_text(h, "headline", brief, where="cím")
    seen_d = set()
    for d in descriptions:
        if norm(d) in seen_d:
            issues.append(Issue("error", "duplicate_description", d, "Ismétlődő leírás"))
        seen_d.add(norm(d))
        issues += check_text(d, "description", brief, where="leírás")
    for p in (path1, path2):
        if p:
            issues += check_style(p, "path")
    if path2 and not path1:
        issues.append(Issue("error", "path_order", path2, "A 2. útvonalhoz kell 1. útvonal is"))
    return issues


# ------------------------------------------------------------------ kulcsszavak
_KW_OK = re.compile(r"^[\w\s'’+&.-]+$", re.UNICODE)


def tokens_of(text):
    return words(text)


def _is_subsequence(needle, hay):
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def stem(w):
    return w[:5]


def check_keyword(text, brief, *, require_close_to_core=False):
    issues = []
    if len(text) > KEYWORD_MAX_CHARS:
        issues.append(Issue("error", "kw_too_long", text, f"A kulcsszó túl hosszú ({len(text)} > {KEYWORD_MAX_CHARS})"))
    if len(text.split()) > KEYWORD_MAX_WORDS:
        issues.append(Issue("error", "kw_too_many_words", text, f"A kulcsszó legfeljebb {KEYWORD_MAX_WORDS} szó lehet"))
    if not text.strip() or not _KW_OK.match(text) or _URL.search(text):
        issues.append(Issue("error", "kw_bad_chars", text, "A kulcsszó csak betűt, számot, szóközt, kötőjelet tartalmazhat (URL, írásjel nem)"))
    issues += check_forbidden(text, [], brief.get("keywords", {}).get("competitor_brands", []))
    if require_close_to_core:
        core = {stem(w) for c in brief.get("keywords", {}).get("core", []) for w in words(c) if len(w) >= 4}
        if not core & {stem(w) for w in words(text) if len(w) >= 4}:
            issues.append(Issue("error", "kw_off_topic", text, "Az új kulcsszó nem kapcsolódik a magkifejezésekhez"))
    return issues


def check_negative(text, brief, positives=()):
    """Negatív kulcsszó: ne zárja ki a magkifejezéseket és a pozitív kulcsszavakat (pl. „kutya” tiltott negatív)."""
    issues = []
    base = check_keyword(text, brief)
    issues += base
    nt = tokens_of(text)
    if not nt:
        return issues
    for c in list(brief.get("keywords", {}).get("core", [])) + list(positives):
        if _is_subsequence(nt, tokens_of(c)):
            issues.append(Issue("error", "negative_blocks_positive", text, f"Ez a negatív kulcsszó kizárná a(z) „{c}” kifejezést"))
            break
    return issues


def errors(issues):
    return [i for i in issues if i.level == "error"]
