"""Kreatív-gyár: új hirdetési képek a meglévő anyagból (ingyenes vágásváltozatok) és AI-val (hetente ≤ N, kemény korlát), ellenőrzéssel.

⚠ A Google a Search kampányok kép-eszközein (AD_IMAGE) TILTJA a szöveges réteget, a logót, az utólag készített kollázst és az elmosott
képet; a fióknak legalább 60 naposnak kell lennie, jó szabályzati előzménnyel, aktív szöveges hirdetéssel és az elmúlt 30 napban
költéssel. Ezért: a képre a motor SZÖVEGET NEM tesz (a szövegváltozat a hirdetésszövegekben, az RSA-ban készül), a képeknél a vágás
(méret, arány) és az új, szövegmentes kulcsvizuál a változó; a feltöltés előtt egy `validateOnly` próba megmondja, hogy a fiók már
jogosult-e (addig AI-képre sem költünk).

Források:
  pack   a projekt saját képei (védettek: a motor soha nem szünetelteti őket; ha az indításkor nem fértek fel, a heti kör pótolja);
  crop   a meglévő képek más vágással/méretben (csak ≤ 30 % vágással, elmosás és kitöltés nélkül);
  ai     új kulcsvizuál gpt-image-2-vel, a brief képstílus-promptjával; hetente legfeljebb `project.max_images_per_week`.

Szabályok (kódban): az AI-kép csak Claude képnézés után kerülhet fel (nincs szöveg/logó, nincs rajzolási hiba, nem elmosott, nincs
túl sok üres felület, a fő téma középen van); ellenőrző (ANTHROPIC_API_KEY) nélkül AI-kép nem tölthető fel. Az AI-kép költség: a
számláló az OpenAI-hívás ELŐTT nő; próbaüzemben (dry) AI-képet nem kérünk. Kampányonként legfeljebb MAX_LIVE_IMAGES bekapcsolt kép;
a helyet a leggyengébb CTR-ű, motor által készített kép szüneteltetése adja (törlés soha).
"""
import dataclasses
import datetime as dt
from typing import List, Literal

from PIL import Image
from pydantic import BaseModel, Field

from . import builder, guardrails as g, images, llm as llmmod, log, openai_images, pack as packmod, reportdata, validators
from .actions import Action
from .google.client import GoogleAdsError

MAX_LIVE_IMAGES = 15           # a Google kampányonként 20-at enged; a többi tartalék
MIN_LIVE_IMAGES = 3
MAX_CROPS_PER_WEEK = 3         # ingyenes vágásváltozatok heti száma
AI_PER_WEEK_DEFAULT = 3        # az AI-képek heti célja (a kemény korlát: project.max_images_per_week)
MAX_CROP_LOSS = 0.30           # ennél nagyobb vágásnál nem készül változat (kitöltés/elmosás tilos)
RETIRE_MIN_IMPRESSIONS = 1000
RETIRE_MIN_AGE_DAYS = 14
RETIRE_CTR_FACTOR = 0.5        # a medián CTR fele alatti kép gyengének számít
DERIVED = {"square": ["portrait"], "portrait": ["square"], "landscape": []}      # amit ≤ 20 % vágással kapunk ugyanabból az AI-képből
FOCI = [(0.5, 0.5), (0.2, 0.5), (0.8, 0.5), (0.5, 0.2), (0.5, 0.8)]
REGISTRY_KEEP = 300
ELIGIBILITY_HINT = ("A Search kampány kép-bővítményéhez a Google legalább 60 napos fiókot, jó szabályzati előzményt, aktív szöveges hirdetést és az "
                    "elmúlt 30 napban költést kér; a motor a képeket később, a heti körben tölti fel.")


# ------------------------------------------------------------------ AI-sémák
class ImageConcept(BaseModel):
    scene: str = Field(max_length=500, description="A jelenet angolul, a képstílus-leírás után fűzve (szöveg, logó, felirat nélkül).")
    kind: Literal["landscape", "square", "portrait"] = "square"


class Concepts(BaseModel):
    concepts: List[ImageConcept] = Field(default_factory=list, max_length=6)


class ImageQA(BaseModel):
    usable: bool
    score: int = Field(ge=1, le=5)
    visible_text: str = Field(default="", max_length=300, description="A képen látható szöveg vagy logó szó szerint (üres, ha nincs).")
    issues: List[str] = Field(default_factory=list, max_length=8)


@dataclasses.dataclass
class Outcome:
    actions: list = dataclasses.field(default_factory=list)
    info: dict = dataclasses.field(default_factory=dict)
    notes: list = dataclasses.field(default_factory=list)


# ------------------------------------------------------------------ nyilvántartás
class Registry:
    """A kép-eszközök nyilvántartása (kv) és a fájlok a /data/creatives/<projekt>/ alatt.
    source: pack (a projekt saját képe, védett) | crop | ai. status: approved (feltöltésre vár) | uploaded | paused | rejected | needs_review."""

    def __init__(self, store, settings, project):
        self.store, self.slug = store, project.slug
        self.key = f"{project.slug}.creatives"
        self.dir = settings.data_dir / "creatives" / project.slug

    def entries(self):
        return list(self.store.get(self.key, []) or [])

    def _save(self, entries):
        for e in entries[:-REGISTRY_KEEP]:                                   # a nyilvántartásból kiesők fájljait is töröljük
            (self.dir / f"{e['name']}.jpg").unlink(missing_ok=True)
        self.store.put(self.key, entries[-REGISTRY_KEEP:])

    def get(self, name):
        return next((e for e in self.entries() if e["name"] == name), None)

    def has_combo(self, combo):
        return any(e.get("detail", {}).get("combo") == combo for e in self.entries())

    def add(self, prepared, source, detail, status, qa=None, reasons=None, today=None):
        today = today or dt.date.today()
        entries = [e for e in self.entries() if e["name"] != prepared.name]
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{prepared.name}.jpg").write_bytes(prepared.data)
        entries.append({"name": prepared.name, "kind": prepared.kind, "source": source, "detail": detail, "sha256": prepared.sha256, "width": prepared.width,
                        "height": prepared.height, "mime": prepared.mime, "mode": prepared.mode, "created": today.isoformat(), "week": g.iso_week(today),
                        "status": status, "qa": qa, "reasons": reasons or []})
        self._save(entries)

    def set_status(self, names, status):
        entries = self.entries()
        for e in entries:
            if e["name"] in names:
                e["status"] = status
        self._save(entries)

    def prepared(self, name):
        """A nyilvántartott kép bájtjai és adatai: ugyanaz a név/hash, mint amikor készült (nincs újrakódolás)."""
        e = self.get(name)
        f = self.dir / f"{name}.jpg"
        if not e or not f.exists():
            return None
        return images.Prepared(f.read_bytes(), e["mime"], e["width"], e["height"], e["kind"], e["sha256"], e["mode"], e["name"])


class AiBudget:
    """Heti kemény korlát: a számláló az OpenAI-hívás ELŐTT nő (a sikertelen próba is számít)."""

    def __init__(self, store, project, today):
        self.store, self.slug, self.period, self.cap = store, project.slug, g.iso_week(today), max(0, int(project.max_images_per_week))

    def used(self):
        return self.store.counter(self.slug, "ai_images", self.period)

    def left(self):
        return max(0, self.cap - self.used())

    def reserve(self):
        if self.left() <= 0:
            raise openai_images.ImageBudgetExceeded(f"a heti AI-képkeret ({self.cap}) elfogyott")
        return self.store.counter_add(self.slug, "ai_images", self.period, 1)


# ------------------------------------------------------------------ ellenőrzés (képnézés)
def qa_system(brief):
    forbidden = ", ".join(brief.get("voice", {}).get("forbidden_words", [])[:20]) or "nincs"
    return (f"Te a(z) {brief['project']['name']} hirdetési képeinek ellenőrzője vagy. Egy kész képet kapsz; döntsd el, hogy használható-e Google Ads Search "
            "kép-bővítménynek. A Google szabályai szerint HIBA: szöveg, felirat, logó vagy vízjel a képen; utólag szerkesztett kollázs vagy keret; elmosott, "
            "életlen, torz vagy rosszul vágott kép; túl sok üres felület, ahol a téma alig ismerhető fel; meztelenség vagy kényes tartalom. Továbbá:\n"
            "- visible_text: írd le SZÓ SZERINT a képen látható szöveget vagy logót (üres, ha nincs).\n"
            "- Rajzolási hibák: extra vagy hiányzó végtag, torz arc vagy test, összeolvadó alakok, értelmetlen tárgyak.\n"
            "- Tilos: márkajelzés, versenytárs, ember arca, megtévesztő tartalom.\n"
            f"- Tiltott témák a képen (a termék szabályai): {forbidden}.\n"
            "- A fő téma a kép középső 80 %-ában legyen, hogy a vágás ne vágja le.\n"
            "A score 1–5 (5: hibátlan); usable csak akkor igaz, ha nincs lényeges hiba. Az issues rövid, magyar mondatok.\n\n" + llmmod.DATA_RULE +
            "\nCsak a kért JSON-t add vissza.")


def qa_image(llm, slug, brief, data, note=""):
    user = "A képen NEM szabad szövegnek vagy logónak látszania." + (f"\nA kért jelenet: {note}" if note else "")
    return llm.ask(project=slug, purpose="image_qa", system=qa_system(brief), user=llmmod.wrap_data("kérés", user) if note else user, schema=ImageQA,
                   effort="low", max_tokens=2000, images=[("image/jpeg", data)])


def accept(qa):
    """A képnézés döntése (kódban): [] = elfogadható; különben az elutasítás okai."""
    reasons = []
    if not qa.usable:
        reasons.append("a képnézés szerint nem használható")
    if qa.score < 4:
        reasons.append(f"alacsony pontszám ({qa.score}/5)")
    if validators.norm(qa.visible_text):
        reasons.append(f"a képen szöveg vagy logó látszik („{qa.visible_text}”): a Google a Search kép-eszközön tiltja")
    if reasons:
        reasons += [i for i in qa.issues[:3] if i not in reasons]
    return reasons


# ------------------------------------------------------------------ jelenetek
def concept_prompt(brief, theme, avoid, n):
    style = brief.get("brand", {}).get("image_style_prompt", "")
    system = (f"Te a(z) {brief['project']['name']} hirdetési képeinek művészeti vezetője vagy. {n} új, egymástól különböző képjelenetet javasolsz egy Google Ads "
              "Search kép-bővítményhez. Minden jelenet: angol nyelvű, konkrét, vizuális leírás (kik/mik, hol, milyen hangulat). Szabályok: a képen NINCS szöveg, "
              "logó, felirat; ember arca nem látszik; a jelenet teljes képkitöltő illusztráció gazdag háttérrel (nem üres, egyszínű felület, nem kollázs, nem keret); "
              "illik a márka képstílusához és a termékhez; a fő téma középen van. A kind a legjobb képarány a jelenethez (square: 1:1, portrait: 4:5, "
              "landscape: 1,91:1).\n\n" + llmmod.DATA_RULE + "\nCsak a kért JSON-t add vissza.")
    user = "\n\n".join([
        llmmod.wrap_data("termék", {"összefoglaló": brief["product"]["summary"], "célcsoportok": brief["product"].get("audiences", [])}),
        llmmod.wrap_data("képstílus", style),
        llmmod.wrap_data("aktuális_téma", theme or "nincs megadva"),
        llmmod.wrap_data("már_használt_jelenetek", avoid[-12:]),
        f"Javasolj {n} új jelenetet."])
    return system, user


def plan_concepts(llm, slug, brief, theme, avoid, n):
    system, user = concept_prompt(brief, theme, avoid, n)
    return llm.ask(project=slug, purpose="image_concepts", system=system, user=user, schema=Concepts, effort="low", max_tokens=3000).concepts[:n]


def ai_prompt(brief, concept):
    style = brief.get("brand", {}).get("image_style_prompt", "")
    return (f"{style}\n\nScene: {concept.scene}\n\nNo text, no letters, no numbers, no logos, no watermark. Keep the main subject inside the central 80 percent "
            "of the frame. A full-bleed illustration with a rich background that fills the whole frame; no large empty areas, no borders, frames or collages.").strip()


# ------------------------------------------------------------------ források és vágásváltozatok
def load_sources(pack, project, fetch, fetch_kw):
    """A projekt kész képei (creatives.json) eredeti méretben. (források, problémák). Egy kép hibája nem akadályozza a többit."""
    sources, problems = [], []
    for ref in pack.creatives.get("images", []):
        try:
            data, _url = packmod.fetch_image(pack, project, ref["file"], fetch=fetch, **fetch_kw)
            im = images.open_image(data)
            sources.append({"id": ref["id"], "data": data, "im": im, "kind": images.kind_for(ref.get("ratio"), im.width, im.height)})
        except Exception as e:
            problems.append(f"forráskép kihagyva ({ref.get('id', '?')}): {e}")
    return sources, problems


def baseline_pack(sources):
    """A projekt saját képei a Google méreteire, ahogy az indításkor készültek: [(Prepared, forrás-azonosító)]."""
    out, seen = [], set()
    for s in sources:
        try:
            p = images.prepare(s["data"], s["kind"])
        except images.ImageError:
            continue
        if p.sha256 not in seen:
            seen.add(p.sha256)
            out.append((p, s["id"]))
    return out


def crop_candidates(sources, registry, known_names, limit):
    """Ingyenes változatok: forráskép × más méret/arány × fókusz, csak ≤ 30 % vágással (elmosás és kitöltés nélkül); a már meglévőket kihagyja."""
    out = []
    for s in sources:
        for kind in ("square", "portrait", "landscape"):
            spec = images.SPECS[kind]
            ratio = s["im"].width / s["im"].height
            if abs(ratio - spec["ratio"]) / spec["ratio"] <= images.TOLERANCE:
                continue                                                      # már pontos arány: a fókusz nem változtat, az alapváltozat megvan
            if 1 - min(ratio, spec["ratio"]) / max(ratio, spec["ratio"]) > MAX_CROP_LOSS:
                continue
            for fx, fy in FOCI:
                combo = f"crop:{s['id']}:{kind}:{fx}:{fy}"
                if registry.has_combo(combo):
                    continue
                try:
                    p = images.prepare(s["data"], kind, mode="cover", focus=(fx, fy))
                except images.ImageError:
                    continue
                if p.name in known_names or registry.get(p.name) or any(p.name == o[0].name for o in out):
                    continue                                                      # ugyanaz a kép már megvan (más fókusz, azonos vágás)
                out.append((p, {"combo": combo, "base": s["id"], "focus": [fx, fy]}))
                if len(out) >= limit:
                    return out
    return out


def derive(prepared, kind):
    """Ugyanabból az AI-képből másik arány (≤ 20 % vágás); None, ha nem lehetséges."""
    try:
        return images.prepare(prepared.data, kind, mode="cover")
    except images.ImageError:
        return None


# ------------------------------------------------------------------ jogosultság
def sample_image():
    """Semleges próbakép a jogosultság-ellenőrzéshez (a validateOnly semmit nem hoz létre)."""
    import io
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1200), (196, 178, 160)).save(buf, "JPEG", quality=80)
    return images.Prepared(buf.getvalue(), "image/jpeg", 1200, 1200, "square", "probe", "exact", "probe-image")


def image_eligible(client, cid, campaign_rn):
    """(igen/nem, indok): a fiók feltöltheti-e a kép-eszközt a kampányhoz? validateOnly próba – nem ír, nem költ. Átmeneti hibánál GoogleAdsError."""
    try:
        client.mutate(cid, builder.build_image_ops(cid, campaign_rn, [sample_image()]), validate_only=True)
        return True, ""
    except GoogleAdsError as e:
        if e.transient:
            raise
        return False, str(e)[:300]


# ------------------------------------------------------------------ a heti lépés
def _finish(out, budget):
    out.info["ai_used_week"] = budget.used()                                # a lépés végén: az ebben a körben elhasznált keret is benne van
    return out


def plan_week(*, settings, project, store, client, brief, pack, llm, openai, campaigns, today, hands_off, fetch, fetch_kw, theme="", renew=None):
    """A heti kreatív-lépés: jogosultság → vágásváltozatok és AI-képek → feltöltés/helycsinálás javaslata. Outcome(actions, info, notes).
    A Google-t csak olvassa (és validateOnly-t kér); a feltöltést a hívó hajtja végre (Executor)."""
    cid = project.customer_id
    out = Outcome(info={"crops": 0, "ai_generated": 0, "approved": 0, "rejected": 0, "needs_review": 0, "uploaded": 0})
    reg = Registry(store, settings, project)
    budget = AiBudget(store, project, today)
    out.info.update({"ai_used_week": budget.used(), "ai_cap": budget.cap})
    camp = next((c for c in campaigns if c["resource_name"] not in hands_off), None)
    if not camp:
        out.notes.append("Képek: nincs szabad (nem „kézben lévő”) bekapcsolt kampány.")
        return _finish(out, budget)
    rows = reportdata.image_rows(client, cid, [c["id"] for c in campaigns], today - dt.timedelta(days=28), today)
    live = [r for r in rows if r["status"] == "ENABLED"]
    out.info["live_images"] = len(live)
    sources, problems = load_sources(pack, project, fetch, fetch_kw)
    out.notes += problems
    base = baseline_pack(sources)
    known = {r["asset_name"] for r in rows} | {p.name for p, _ in base}          # a meglévő/pack nevek: ezeket a vágásváltozatok nem ismétlik
    on_google = {r["asset_name"] for r in rows}
    # a projekt saját képei: ami az indításkor nem fért fel, azt pótoljuk; amit valaki eltávolított (nyilvántartott, de nincs a kampányban), ahhoz nem nyúlunk
    pending_pack = []
    for p, sid in base:
        e = reg.get(p.name)
        if p.name in on_google:
            if e is None:
                reg.add(p, "pack", {"base": sid}, "uploaded", today=today)
            elif e["status"] == "approved":
                reg.set_status([p.name], "uploaded")
        elif e is None or e["status"] == "approved":
            if e is None:
                reg.add(p, "pack", {"base": sid}, "approved", today=today)
            pending_pack.append(p)
        else:
            out.notes.append(f"Képek: a(z) {sid} képet valaki eltávolította a kampányból, ezért nem töltöm fel újra.")
    approved = [reg.prepared(e["name"]) for e in reg.entries() if e["status"] == "approved" and e["source"] != "pack"]
    approved = [p for p in approved if p is not None]
    # jogosultság: amíg a Google nem engedi, semmit nem készítünk (AI-képre sem költünk)
    wants = pending_pack or approved or budget.left() > 0 or any(True for _ in crop_candidates(sources, reg, known, 1))
    if wants:
        try:
            eligible, why = image_eligible(client, cid, camp["resource_name"])
        except GoogleAdsError as e:
            out.notes.append(f"Képek: a jogosultság-ellenőrzés most nem sikerült ({str(e)[:160]}); a képlépés a következő héten újra próbálkozik.")
            return _finish(out, budget)
        if not eligible:
            out.notes.append("Képek: a Google még nem engedi a kép-bővítményt ezen a fiókon. " + ELIGIBILITY_HINT)
            out.info["ineligible_reason"] = why
            return _finish(out, budget)
    else:
        return _finish(out, budget)
    # 1) ingyenes vágásváltozatok
    for p, detail in crop_candidates(sources, reg, known, MAX_CROPS_PER_WEEK):
        reg.add(p, "crop", detail, "approved", today=today)
        approved.append(p)
        out.info["crops"] += 1
    # 2) AI-kulcsvizuálok (csak éles üzemben, képnézéssel)
    ai_target = min(AI_PER_WEEK_DEFAULT, budget.left())
    if ai_target > 0:
        if openai is None:
            out.notes.append("Képek: az új AI-képek kimaradnak (nincs OPENAI_API_KEY).")
        elif llm is None:
            out.notes.append("Képek: az AI-képek kimaradnak (ellenőrző nélkül, ANTHROPIC_API_KEY hiányában nem töltök fel AI-képet).")
        elif not settings.live:
            out.notes.append("Képek: próbaüzemben (dry) AI-képet nem kérek, mert költséggel jár.")
        elif not brief.get("brand", {}).get("image_style_prompt"):
            out.notes.append("Képek: az AI-képekhez a brief brand.image_style_prompt mezője kell.")
        else:
            approved += _ai_step(out, reg, budget, llm, openai, brief, project.slug, theme, ai_target, today, renew)
    # 3) feltöltés és helycsinálás
    queue = pending_pack + [p for p in approved if p.name not in on_google and p.name not in {q.name for q in pending_pack}]
    if not queue:
        return _finish(out, budget)
    room = MAX_LIVE_IMAGES - len(live)
    retire = _retire_candidates(rows, reg, today, need=len(queue) - room, live_count=len(live)) if len(queue) > room else []
    upload = queue[:max(0, room + len(retire))]
    for rn, name, why in retire[:max(0, len(upload) - max(0, room))]:
        out.actions.append(Action("pause_image", name, why, detail={"resource_name": rn, "asset_name": name}))
    if upload:
        srcs = {p.name: (reg.get(p.name) or {}).get("source", "pack") for p in upload}
        out.actions.append(Action("add_images", f"{len(upload)} kép ({', '.join(sorted(set(srcs.values())))})", "új kreatívok a kampányhoz",
                                  detail={"campaign_rn": camp["resource_name"], "names": [p.name for p in upload], "sources": srcs}))
        out.info["uploaded"] = len(upload)
    if len(queue) > len(upload):
        out.notes.append(f"Képek: a kampányban {len(live)} kép van, {len(queue) - len(upload)} új kép vár helyre (a leggyengébbeket a motor 14 nap után cseréli).")
    return _finish(out, budget)


def _ai_step(out, reg, budget, llm, openai, brief, slug, theme, target, today, renew):
    approved = []
    avoid = [e["detail"].get("scene", "") for e in reg.entries() if e["source"] == "ai"]
    try:
        concepts = plan_concepts(llm, slug, brief, theme, avoid, target)
    except llmmod.LLMError as e:
        out.notes.append(f"Képek: a jelenettervezés nem sikerült ({e}).")
        return approved
    for concept in concepts:
        if renew:
            renew()
        try:
            budget.reserve()
        except openai_images.ImageBudgetExceeded as e:
            out.notes.append(f"Képek: {e}.")
            break
        try:
            gen = openai.generate(ai_prompt(brief, concept), concept.kind)
        except openai_images.ImageAPIError as e:
            out.notes.append(f"Képek: az AI-képkérés nem sikerült: {e}")
            if e.fatal:
                break                                                        # kulcs-/keret-hiba: a többi kérést sem érdemes
            continue
        out.info["ai_generated"] += 1
        try:
            main = images.prepare(gen.data, concept.kind, mode="exact")
        except images.ImageError as e:
            out.notes.append(f"Képek: a kapott kép nem használható ({e}).")
            continue
        detail = {"scene": concept.scene, "kind": concept.kind, "model": openai.model, "size": gen.size}
        try:
            qa = qa_image(llm, slug, brief, main.data, note=concept.scene[:200])
        except llmmod.LLMError as e:
            reg.add(main, "ai", detail, "needs_review", reasons=[f"a képnézés nem sikerült: {e}"], today=today)
            out.info["needs_review"] += 1
            continue
        reasons, qa_d = accept(qa), qa.model_dump()
        if reasons:
            reg.add(main, "ai", detail, "rejected", qa=qa_d, reasons=reasons, today=today)
            out.info["rejected"] += 1
            out.notes.append(f"Képek: egy AI-képet a képnézés elutasított: {'; '.join(reasons[:2])}")
            continue
        for p in [main] + [d for d in (derive(main, k) for k in DERIVED[concept.kind]) if d]:
            reg.add(p, "ai", {**detail, **({"derived_from": main.name} if p is not main else {})}, "approved", qa=qa_d, today=today)
            approved.append(p)
            out.info["approved"] += 1
    return approved


def _retire_candidates(rows, reg, today, need, live_count):
    """A leggyengébb CTR-ű, motor által készített, legalább 14 napos, elég megjelenést látott képek: [(kötés-erőforrásnév, név, indok)].
    A projekt saját képeit és a kevés adatú képeket soha; legalább MIN_LIVE_IMAGES kép marad."""
    by = {r["asset_name"]: r for r in rows if r["status"] == "ENABLED"}
    measured = [r for r in by.values() if r["impressions"] >= RETIRE_MIN_IMPRESSIONS]
    ctrs = sorted(r["clicks"] / r["impressions"] for r in measured)
    if not ctrs:
        return []
    median = ctrs[len(ctrs) // 2]
    weak = []
    for r in measured:
        e = reg.get(r["asset_name"])
        if not e or e["source"] == "pack" or e["status"] not in ("uploaded", "approved"):
            continue                                                         # védett: a projekt saját képe (vagy ismeretlen eredetű)
        if (today - dt.date.fromisoformat(e["created"])).days < RETIRE_MIN_AGE_DAYS:
            continue
        ctr = r["clicks"] / r["impressions"]
        if ctr < median * RETIRE_CTR_FACTOR:
            weak.append((ctr, r))
    weak.sort(key=lambda x: x[0])
    out = []
    for ctr, r in weak[:max(0, min(need, live_count - MIN_LIVE_IMAGES))]:
        out.append((r["resource_name"], r["asset_name"], f"gyenge kattintási arány ({ctr * 100:.2f} % a {median * 100:.2f} % mediánhoz képest), helyet csinálok az újaknak"))
    return out
