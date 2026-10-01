"""Hirdetésszöveg-írás: Claude javaslatai, amelyeket a KÓDBAN lévő validátorok szűrnek (validators.py).

Az AI csak javasol: minden címet és leírást megvizsgálunk (hossz, írásjelek, tiltott szavak, tények és kényes állítások). Ami
hibás, kiesik és a naplóba kerül; ami átmegy, az egyedi (nem ismétli a meglévőket, és nem ismétlik egymást).
"""
import dataclasses
from typing import List

from pydantic import BaseModel, Field

from . import llm as llmmod, validators


class CopyVariants(BaseModel):
    headlines: List[str] = Field(default_factory=list, max_length=20, description="Új címek, legfeljebb 30 karakter, felkiáltójel nélkül.")
    descriptions: List[str] = Field(default_factory=list, max_length=8, description="Új leírások, legfeljebb 90 karakter, legfeljebb egy felkiáltójellel.")
    rationale: str = Field(default="", max_length=600, description="Egy-két mondat: milyen szemszögekből írtál.")


@dataclasses.dataclass
class CopyResult:
    headlines: list
    descriptions: list
    rejected: list            # [(szöveg, [okok])]
    rationale: str = ""


def system_prompt(brief):
    voice = brief.get("voice", {})
    forbidden = ", ".join(voice.get("forbidden_words", [])) or "nincs"
    claims = "\n".join(f"- {c}" for c in voice.get("forbidden_claims", []))
    avoid = "\n".join(f"- {a}" for a in voice.get("avoid", []))
    return (f"Te egy magyar Google Ads szövegíró vagy, a(z) {brief['project']['name']} nevű termék hirdetéseit írod. "
            "A feladatod: új, jól hangzó címeket és leírásokat adni egy reszponzív keresési hirdetéshez.\n\n"
            "Szabályok (kódban ellenőrizzük őket, ami nem felel meg, kidobjuk):\n"
            f"- Hangnem: {voice.get('tone', 'barátságos, érthető')}\n"
            "- Cím: legfeljebb 30 karakter, NINCS felkiáltójel, nincs emoji, nincs csupa nagybetűs szó (rövidítés kivételével), mondatszerű írás.\n"
            "- Leírás: legfeljebb 90 karakter, legfeljebb egy felkiáltójel.\n"
            "- Csak a briefben szereplő tényekből dolgozz. Minden szám a tények közül való legyen; az ingyenes, a regisztráció nélkül, a "
            "garantált, az allergia, az ár és a felsőfok csak akkor szerepelhet, ha a tények igazolják.\n"
            f"- Tiltott szavak: {forbidden}\n"
            + (f"- Tiltott állítások:\n{claims}\n" if claims else "")
            + (f"- Kerülendő:\n{avoid}\n" if avoid else "")
            + "- Ne használd versenytárs nevét. Minden változat más szemszögű legyen (előny, kérdés, felszólítás, konkrét tény), és ne ismételd "
              "a meglévő szövegeket.\n\n" + llmmod.DATA_RULE + "\nCsak a kért JSON-t add vissza.")


def user_prompt(brief, adset, n_headlines, n_descriptions, performance=None):
    facts = [{"id": f["id"], "tény": f["text"], "számok": f.get("numbers", []), "igazolt_szavak": f.get("tokens", [])}
             for f in brief["product"]["facts"]]
    landing = next((p for p in brief["landing_pages"] if p["id"] == adset["landing"]), {})
    parts = [
        llmmod.wrap_data("brief", {"termék": brief["product"]["summary"], "ajánlat": brief["product"].get("offer", ""), "tények": facts,
                                   "célcsoportok": brief["product"].get("audiences", [])}),
        llmmod.wrap_data("hirdetéscsoport", {"téma": adset["theme"], "nyitóoldal": landing.get("purpose", ""),
                                             "kulcsszavak": [k["text"] for k in adset["keywords"]][:20]}),
        llmmod.wrap_data("meglévő_szövegek", {"címek": adset["headlines"], "leírások": adset["descriptions"]}),
    ]
    if performance:
        parts.append(llmmod.wrap_data("teljesítmény", performance))
    parts.append(f"Írj {n_headlines} új címet és {n_descriptions} új leírást ehhez a hirdetéscsoporthoz. "
                 "Ha a teljesítmény-adatban gyenge szövegek vannak, ne azok stílusában írj.")
    return "\n\n".join(parts)


def generate_copy(llm, project_slug, brief, adset, *, n_headlines=6, n_descriptions=2, performance=None, effort="medium"):
    """Új cím- és leírás-változatok az adott hirdetéscsoporthoz, a validátorokon átszűrve."""
    raw = llm.ask(project=project_slug, purpose="copy", system=system_prompt(brief),
                  user=user_prompt(brief, adset, n_headlines, n_descriptions, performance), schema=CopyVariants, effort=effort)
    seen = {validators.norm(t) for t in adset["headlines"] + adset["descriptions"]}
    ok_h, ok_d, rejected = [], [], []

    def take(texts, kind, bucket, limit):
        for t in texts:
            t = t.strip()
            key = validators.norm(t)
            if not t:
                continue
            if key in seen:
                rejected.append((t, ["ismétlődő vagy már létező szöveg"]))
                continue
            errs = validators.errors(validators.check_text(t, kind, brief))
            if errs:
                rejected.append((t, [e.message for e in errs]))
                continue
            seen.add(key)
            if len(bucket) < limit:
                bucket.append(t)

    take(raw.headlines, "headline", ok_h, n_headlines)
    take(raw.descriptions, "description", ok_d, n_descriptions)
    return CopyResult(ok_h, ok_d, rejected, raw.rationale)
