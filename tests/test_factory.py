"""Próbák: a kreatív-gyár – OpenAI-kliens és heti keret, képnézés, vágásváltozatok, jogosultság, feltöltés, helycsinálás, a heti körbe illesztés."""
import dataclasses
import datetime as dt
import io
import json
import unittest

import _path  # noqa: F401
import helpers
from PIL import Image
from ads_engine import builder, factory, guardrails as g, images, launch, net, openai_images
from ads_engine.openai_images import ImageAPIError, ImageBudgetExceeded, OpenAIImages
from ads_engine.store import Store
from mock_openai import MockOpenAI
from test_review import ReviewBase, LIVE_SINCE, NOW, TODAY

CID = helpers.CID
SCENES = {"concepts": [{"scene": "A fluffy puppy sniffing golden autumn leaves in a quiet park", "kind": "square"},
                       {"scene": "A dog walking on a misty forest path in the morning", "kind": "portrait"}]}


def synthetic_prepared(seed, kind="square"):
    import hashlib
    buf = io.BytesIO()
    Image.new("RGB", images.SPECS[kind]["size"], tuple(hashlib.sha256(str(seed).encode()).digest()[:3])).save(buf, "PNG")
    return images.prepare(buf.getvalue(), kind, mode="exact")


class OpenAIClientTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockOpenAI().start()
        self.client = OpenAIImages(self.srv.api_key, base_url=self.srv.base_url)

    def tearDown(self):
        self.srv.stop()

    def test_sizes_and_request_shape(self):
        for kind, size in (("square", (1536, 1536)), ("portrait", (1536, 1920)), ("landscape", (1920, 1008))):
            gen = self.client.generate(f"egy kép: {kind}", kind)
            self.assertEqual(Image.open(io.BytesIO(gen.data)).size, size)
            self.assertEqual(gen.size, f"{size[0]}x{size[1]}")
            self.assertTrue(gen.request_id.startswith("req_"))
        body = self.srv.requests[0]
        self.assertEqual((body["model"], body["quality"], body["n"], body["size"]), ("gpt-image-2", "high", 1, "1536x1536"))

    def test_every_size_is_within_the_model_limits_and_close_to_the_google_ratios(self):
        for kind, size in openai_images.SIZES.items():
            w, h = (int(x) for x in size.split("x"))
            self.assertLessEqual(w * h, openai_images.MAX_PIXELS)
            self.assertLessEqual(max(w, h), 3840)
            self.assertEqual(images.classify(w, h), kind)                          # a Google ±1 %-os arány-tűrésén belül

    def test_errors_are_explained_and_fatal_only_when_retrying_is_pointless(self):
        cases = [((401, {"error": {"code": "invalid_api_key", "message": "x"}}), "OPENAI_API_KEY", True),
                 ((429, {"error": {"code": "insufficient_quota", "message": "x"}}), "számlakeret", True),
                 ((400, {"error": {"code": "moderation_blocked", "message": "safety"}}), "biztonsági szűrő", False),
                 ((500, {"error": {"code": "server_error", "message": "elromlott"}}), "hibát adott (500)", False)]
        for resp, text, fatal in cases:
            self.srv.fail.append(resp)
            with self.assertRaises(ImageAPIError) as cm:
                self.client.generate("x", "square")
            self.assertIn(text, str(cm.exception))
            self.assertEqual(cm.exception.fatal, fatal)

    def test_no_automatic_retry_after_a_server_error(self):
        self.srv.fail += [(503, {"error": {"message": "x"}})] * 3
        with self.assertRaises(ImageAPIError):
            self.client.generate("x", "square")
        self.assertEqual(len(self.srv.requests), 1)                                # nincs újrapróba: a hívás pénzbe kerülhetett

    def test_unreachable_is_fatal(self):
        self.srv.stop()
        with self.assertRaises(ImageAPIError) as cm:
            self.client.generate("x", "square")
        self.assertTrue(cm.exception.fatal)

    def test_missing_key_and_key_is_redacted(self):
        with self.assertRaises(ImageAPIError):
            OpenAIImages("")
        import ads_engine.log as log
        self.assertIn(self.srv.api_key, log._secrets) if hasattr(log, "_secrets") else None


class BudgetAndQaTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.project = type("P", (), {"slug": "pacsi", "max_images_per_week": 3})()

    def tearDown(self):
        self.store.close()

    def test_budget_counts_before_the_call_and_refuses_at_the_cap(self):
        b = factory.AiBudget(self.store, self.project, dt.date(2026, 10, 12))
        self.assertEqual((b.cap, b.used(), b.left()), (3, 0, 3))
        for i in range(3):
            self.assertEqual(b.reserve(), i + 1)
        with self.assertRaises(ImageBudgetExceeded):
            b.reserve()
        self.assertEqual((b.used(), b.left()), (3, 0))

    def test_budget_is_per_iso_week(self):
        factory.AiBudget(self.store, self.project, dt.date(2026, 10, 12)).reserve()
        next_week = factory.AiBudget(self.store, self.project, dt.date(2026, 10, 19))
        self.assertEqual((next_week.period, next_week.used()), ("2026-W43", 0))
        sunday = factory.AiBudget(self.store, self.project, dt.date(2026, 10, 18))
        self.assertEqual((sunday.period, sunday.used()), ("2026-W42", 1))            # ugyanaz a hét vasárnapig

    def test_accept_rules(self):
        qa = lambda **k: factory.ImageQA(**{"usable": True, "score": 5, "visible_text": "", "issues": [], **k})
        self.assertEqual(factory.accept(qa()), [])
        self.assertEqual(factory.accept(qa(score=4)), [])
        self.assertIn("alacsony pontszám (3/5)", factory.accept(qa(score=3))[0])
        self.assertIn("nem használható", factory.accept(qa(usable=False, issues=["extra végtag"]))[0])
        self.assertIn("extra végtag", " ".join(factory.accept(qa(usable=False, issues=["extra végtag"]))))
        text = factory.accept(qa(visible_text="PACSI"))
        self.assertIn("szöveg vagy logó látszik", text[0])
        self.assertIn("Search kép-eszközön tiltja", text[0])


class RegistryTests(ReviewBase):
    def reg(self):
        return factory.Registry(self.e.store, self.e.settings, self.e.project)

    def test_roundtrip_keeps_the_exact_bytes_name_and_hash(self):
        p = synthetic_prepared(1)
        self.reg().add(p, "ai", {"scene": "x"}, "approved", qa={"score": 5}, today=TODAY)
        back = self.reg().prepared(p.name)
        self.assertEqual((back.data, back.sha256, back.name, back.kind, back.width, back.height), (p.data, p.sha256, p.name, p.kind, p.width, p.height))
        e = self.reg().get(p.name)
        self.assertEqual((e["source"], e["status"], e["week"], e["created"]), ("ai", "approved", "2026-W42", "2026-10-12"))
        self.reg().set_status([p.name], "uploaded")
        self.assertEqual(self.reg().get(p.name)["status"], "uploaded")
        self.assertIsNone(self.reg().prepared("nincs-ilyen"))

    def test_combo_memory_and_pruning(self):
        p = synthetic_prepared(2)
        self.reg().add(p, "crop", {"combo": "crop:a:square:0.5:0.5"}, "approved", today=TODAY)
        self.assertTrue(self.reg().has_combo("crop:a:square:0.5:0.5"))
        self.assertFalse(self.reg().has_combo("crop:a:square:0.2:0.5"))
        for i in range(10, 10 + factory.REGISTRY_KEEP + 5):
            self.reg().add(synthetic_prepared(i), "ai", {}, "rejected", today=TODAY)
        self.assertEqual(len(self.reg().entries()), factory.REGISTRY_KEEP)


class CropTests(ReviewBase):
    def sources(self):
        pk = factory.packmod.load(self.e.project, self.e.store, **self.e.kw)
        return factory.load_sources(pk, self.e.project, net.fetch, self.e.kw)

    def test_only_clean_crops_with_a_small_loss_are_made(self):
        sources, problems = self.sources()
        self.assertEqual(problems, [])
        self.assertEqual({s["kind"] for s in sources}, {"square", "landscape"})
        reg = factory.Registry(self.e.store, self.e.settings, self.e.project)
        base = factory.baseline_pack(sources)
        known = {p.name for p, _ in base}
        crops = factory.crop_candidates(sources, reg, known, 10)
        # csak a négyzetes forrásból lesz álló (4:5) vágás: 3 különböző fókusz; a fekvő képek bármilyen más arányra ≥ 47 % vágást jelentenének
        self.assertEqual(len(crops), 3)
        for p, detail in crops:
            self.assertEqual((p.kind, p.mode, (p.width, p.height)), ("portrait", "cover", (960, 1200)))
            self.assertTrue(detail["combo"].startswith("crop:"))
            self.assertNotIn(p.name, known)
        self.assertEqual(len({p.name for p, _ in crops}), 3)
        self.assertEqual(len(factory.crop_candidates(sources, reg, known, 2)), 2)                # a korlát érvényes

    def test_baseline_is_what_launch_uploaded(self):
        sources, _ = self.sources()
        names = {p.name for p, _ in factory.baseline_pack(sources)}
        uploaded = {a["name"] for a in self.state("asset").values() if a.get("name")}
        self.assertEqual(names, uploaded)

    def test_registered_combos_are_not_repeated(self):
        sources, _ = self.sources()
        reg = factory.Registry(self.e.store, self.e.settings, self.e.project)
        known = {p.name for p, _ in factory.baseline_pack(sources)}
        first = factory.crop_candidates(sources, reg, known, 10)
        for p, d in first:
            reg.add(p, "crop", d, "approved", today=TODAY)
        self.assertEqual(factory.crop_candidates(sources, reg, known, 10), [])


class FactoryBase(ReviewBase):
    def setUp(self):
        super().setUp()
        self.oai_srv = MockOpenAI().start()
        self.oai = OpenAIImages(self.oai_srv.api_key, base_url=self.oai_srv.base_url)

    def tearDown(self):
        self.oai_srv.stop()
        super().tearDown()

    def fweekly(self, **kw):
        return self.weekly(openai=kw.pop("openai", self.oai), **kw)

    def links(self, status=None):
        return [c for c in self.state("campaignAsset").values() if c["fieldType"] == "AD_IMAGE" and (status is None or c.get("status") == status)]

    def reg(self):
        return factory.Registry(self.e.store, self.e.settings, self.e.project)

    def creative(self, res):
        return res.report["creative"]

    def uploads(self, res):
        return [a for a in res.actions if a.kind == "add_images"]


class WeeklyCropTests(FactoryBase):
    def test_free_crops_are_uploaded_once_and_registered(self):
        before = len(self.links())
        res = self.fweekly()
        self.assertEqual(before, 3)
        c = self.creative(res)
        self.assertEqual((c["crops"], c["uploaded"], c["live_images"]), (3, 3, 3))
        [a] = self.uploads(res)
        self.assertEqual((a.status, a.detail["sources"] and set(a.detail["sources"].values())), ("applied", {"crop"}))
        self.assertEqual(len(self.links("ENABLED")), 6)
        self.assertEqual({e["status"] for e in self.reg().entries() if e["source"] == "crop"}, {"uploaded"})
        self.assertEqual({e["source"] for e in self.reg().entries() if e["status"] == "uploaded"}, {"crop", "pack"})        # a pack-képek is bekerültek (védettként)
        log = self.e.store.actions(kind="add_images")[0]
        self.assertEqual((log["status"], log["mode"]), ("applied", "live"))
        again = self.fweekly()                                                         # a következő héten nincs mit hozzátenni
        self.assertEqual(self.uploads(again), [])
        self.assertEqual(len(self.links()), 6)

    def test_the_report_has_an_image_section(self):
        from ads_engine import reports
        res = self.fweekly()
        text = reports.render_text(res.report)
        self.assertIn("KÉPEK", text)
        self.assertIn("3 új vágásváltozat", text)
        self.assertIn("Új képek feltöltve", text)
        self.assertIn("Képek", reports.render_html(res.report))

    def test_no_image_step_during_the_observation_period(self):
        self.e.store.put("pacsi.go_live", {"weekly_budget": 14000, "daily_micros": 2000 * 1_000_000, "date": "2026-10-10"})
        res = self.fweekly()
        self.assertNotIn("creative", res.report)
        self.assertEqual(len(self.links()), 3)
        self.assertEqual(self.oai_srv.requests, [])

    def test_dry_mode_validates_but_uploads_nothing_and_asks_for_no_ai_image(self):
        self.e.with_mode("dry")
        self.concepts_answer = SCENES
        res = self.fweekly()
        [a] = self.uploads(res)
        self.assertEqual(a.status, "validated")
        self.assertEqual(len(self.links()), 3)
        self.assertEqual(self.oai_srv.requests, [])
        self.assertIn("próbaüzemben (dry) AI-képet nem kérek", " ".join(res.report["notes"]))
        self.assertEqual({e["status"] for e in self.reg().entries() if e["source"] == "crop"}, {"approved"})     # feltöltésre vár

    def test_hands_off_campaign_gets_no_image_step(self):
        self.e.store.put("pacsi.hands_off", {self.camp_rn: "2026-11-01"})
        self.assertNotIn("creative", self.fweekly().report)

    def test_regulated_projects_are_not_changed_automatically(self):
        self.e.root.joinpath("brief.json").write_text(
            json.dumps({**json.loads(self.e.root.joinpath("brief.json").read_text(encoding="utf-8")), "compliance": {"category": "regulated"}}, ensure_ascii=False), encoding="utf-8")
        res = self.fweekly()
        self.assertEqual(res.actions, [])
        self.assertIn("szabályozott területen", " ".join(res.report["notes"]))
        self.assertEqual(len(self.links()), 3)


class WeeklyAiTests(FactoryBase):
    def setUp(self):
        super().setUp()
        self.concepts_answer = SCENES

    def test_ai_images_are_generated_checked_derived_and_uploaded(self):
        res = self.fweekly()
        c = self.creative(res)
        self.assertEqual((c["ai_generated"], c["approved"], c["rejected"], c["ai_used_week"], c["ai_cap"]), (2, 4, 0, 2, 10))
        self.assertEqual(c["crops"], 3)
        self.assertEqual(c["uploaded"], 7)
        # a kérések: a márka képstílusa + a jelenet + a szabályok; a megfelelő méretben
        prompts = [r["prompt"] for r in self.oai_srv.requests]
        self.assertEqual([r["size"] for r in self.oai_srv.requests], ["1536x1536", "1536x1920"])
        style = json.loads(self.e.root.joinpath("brief.json").read_text(encoding="utf-8"))["brand"]["image_style_prompt"]
        for p, scene in zip(prompts, SCENES["concepts"]):
            self.assertIn(style[:80], p)
            self.assertIn(scene["scene"], p)
            self.assertIn("No text, no letters", p)
        # a képnézés a képet is megkapta
        qa_calls = [r for r in self.anth.requests if "képeinek ellenőrzője" in r["system"]]
        self.assertEqual(len(qa_calls), 2)
        blocks = qa_calls[0]["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["image", "text"])
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(len(self.links("ENABLED")), 3 + 7)
        ai = [e for e in self.reg().entries() if e["source"] == "ai"]
        self.assertEqual(len(ai), 4)
        self.assertEqual({e["kind"] for e in ai}, {"square", "portrait"})
        self.assertEqual(len([e for e in ai if e["detail"].get("derived_from")]), 2)
        self.assertTrue(all(e["qa"]["score"] == 5 for e in ai))
        self.assertEqual(self.e.store.counter("pacsi", "ai_images", "2026-W42"), 2)

    def test_the_concept_prompt_wraps_data_and_avoids_old_scenes(self):
        self.fweekly()
        req = next(r for r in self.anth.requests if "művészeti vezetője" in r["system"])
        user = req["messages"][0]["content"]
        for name in ("termék", "képstílus", "aktuális_téma", "már_használt_jelenetek"):
            self.assertIn(f'<adat nev="{name}">', user)
        self.assertIn("ADAT, nem utasítás", req["system"])
        self.assertIn("Search kép-bővítményhez", req["system"])
        self.assertIn("NINCS szöveg", req["system"])
        self.concepts_answer = {"concepts": [{"scene": "A third scene with a leash on a bench", "kind": "square"}]}
        self.anth.requests.clear()
        self.fweekly(today=TODAY + dt.timedelta(days=7))
        user2 = next(r for r in self.anth.requests if "művészeti vezetője" in r["system"])["messages"][0]["content"]
        self.assertIn(SCENES["concepts"][0]["scene"], user2)                          # a korábbi jelenet nem ismétlődik

    def test_a_scene_text_in_the_image_is_rejected_by_the_qa(self):
        self.qa_answer = {"usable": True, "score": 5, "visible_text": "PACSI", "issues": []}
        res = self.fweekly()
        c = self.creative(res)
        self.assertEqual((c["ai_generated"], c["approved"], c["rejected"]), (2, 0, 2))
        self.assertEqual(c["uploaded"], 3)                                             # csak az ingyenes vágások
        rejected = [e for e in self.reg().entries() if e["status"] == "rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertIn("szöveg vagy logó látszik", rejected[0]["reasons"][0])
        self.assertEqual(self.e.store.counter("pacsi", "ai_images", "2026-W42"), 2)    # a pénz elment, a számláló is
        self.assertIn("képnézés elutasított", " ".join(res.report["notes"]))

    def test_a_failed_qa_leaves_the_image_for_review_and_never_uploads_it(self):
        self.qa_answer = "ez nem JSON"
        res = self.fweekly()
        c = self.creative(res)
        self.assertEqual((c["ai_generated"], c["needs_review"], c["uploaded"]), (2, 2, 3))
        self.assertEqual(len([e for e in self.reg().entries() if e["status"] == "needs_review"]), 2)
        self.assertEqual(len(self.links("ENABLED")), 6)

    def test_without_the_checker_no_ai_image_is_uploaded(self):
        res = self.fweekly(llm=None)
        self.assertEqual(self.oai_srv.requests, [])
        self.assertIn("ellenőrző nélkül", " ".join(res.report["notes"]))
        self.assertEqual(self.creative(res)["crops"], 3)

    def test_without_the_openai_key_only_free_crops_are_made(self):
        res = self.fweekly(openai=None)
        self.assertIn("nincs OPENAI_API_KEY", " ".join(res.report["notes"]))
        self.assertEqual((self.creative(res)["crops"], self.creative(res)["ai_generated"]), (3, 0))

    def test_the_brief_needs_an_image_style_prompt(self):
        b = json.loads(self.e.root.joinpath("brief.json").read_text(encoding="utf-8"))
        del b["brand"]["image_style_prompt"]
        self.e.root.joinpath("brief.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        res = self.fweekly()
        self.assertEqual(self.oai_srv.requests, [])
        self.assertIn("image_style_prompt", " ".join(res.report["notes"]))


class AiBudgetInPracticeTests(FactoryBase):
    def setUp(self):
        super().setUp()
        self.concepts_answer = {"concepts": [{"scene": f"Scene number {i} with an autumn park", "kind": "square"} for i in range(3)]}

    def test_the_counter_is_incremented_before_the_call_so_a_failed_request_still_counts(self):
        self.e.project = dataclasses.replace(self.e.project, max_images_per_week=1)
        self.oai_srv.fail.append((500, {"error": {"message": "elromlott"}}))
        res = self.fweekly()
        self.assertEqual(len(self.oai_srv.requests), 1)                                # a célt a hátralévő keret korlátozza: 1 kérés
        self.assertEqual(self.e.store.counter("pacsi", "ai_images", "2026-W42"), 1)    # a sikertelen kérés is elhasználta a keretet
        self.assertEqual(self.creative(res)["ai_used_week"], 1)
        self.assertIn("az AI-képkérés nem sikerült", " ".join(res.report["notes"]))
        self.fweekly()                                                                 # ugyanabban a hétben újra: a keret elfogyott, nincs új kérés
        self.assertEqual(len(self.oai_srv.requests), 1)

    def test_the_cap_holds_across_runs_in_the_same_week(self):
        self.e.project = dataclasses.replace(self.e.project, max_images_per_week=4)
        self.fweekly()                                                                 # 3 hívás
        self.assertEqual(len(self.oai_srv.requests), 3)
        self.concepts_answer = {"concepts": [{"scene": f"Another scene {i}", "kind": "square"} for i in range(3)]}
        self.fweekly()                                                                 # csak 1 maradt
        self.assertEqual(len(self.oai_srv.requests), 4)
        self.fweekly()                                                                 # 0
        self.assertEqual(len(self.oai_srv.requests), 4)
        self.assertEqual(self.e.store.counter("pacsi", "ai_images", "2026-W42"), 4)
        self.fweekly(today=TODAY + dt.timedelta(days=7))                               # új hét, új keret
        self.assertGreater(len(self.oai_srv.requests), 4)

    def test_a_fatal_error_stops_the_remaining_requests(self):
        self.oai_srv.fail.append((429, {"error": {"code": "insufficient_quota", "message": "x"}}))
        res = self.fweekly()
        self.assertEqual(len(self.oai_srv.requests), 1)
        self.assertIn("számlakeret", " ".join(res.report["notes"]))
        self.assertEqual(self.e.store.counter("pacsi", "ai_images", "2026-W42"), 1)

    def test_a_moderation_block_only_skips_that_image(self):
        self.oai_srv.fail.append((400, {"error": {"code": "moderation_blocked", "message": "safety"}}))
        res = self.fweekly()
        self.assertEqual(len(self.oai_srv.requests), 3)
        self.assertEqual(self.creative(res)["ai_generated"], 2)
        self.assertIn("biztonsági szűrője", " ".join(res.report["notes"]))


class EligibilityTests(FactoryBase):
    def test_a_young_account_gets_no_images_and_spends_nothing(self):
        self.e.mock.reject_images = True
        self.concepts_answer = SCENES
        res = self.fweekly()
        notes = " ".join(res.report["notes"])
        self.assertIn("még nem engedi a kép-bővítményt", notes)
        self.assertIn("60 napos fiókot", notes)
        self.assertIn("ineligible_reason", self.creative(res))
        self.assertEqual(self.oai_srv.requests, [])                                    # AI-képre sem költünk
        self.assertEqual(self.uploads(res), [])
        self.assertEqual([e for e in self.reg().entries() if e["source"] != "pack"], [])      # csak az indításkori (védett) képek vannak benne
        self.e.mock.reject_images = False                                              # később, a fiók „megérésekor” a lépés magától működik
        later = self.creative(self.fweekly(today=TODAY + dt.timedelta(days=7)))
        self.assertEqual((later["crops"], later["ai_generated"], later["uploaded"]), (3, 2, 7))

    def test_the_probe_creates_nothing(self):
        before = (len(self.state("asset")), len(self.state("campaignAsset")))
        eligible, why = factory.image_eligible(self.e.client, CID, self.camp_rn)
        self.assertEqual((eligible, why), (True, ""))
        self.assertEqual((len(self.state("asset")), len(self.state("campaignAsset"))), before)
        last = [r for r in self.e.mock.requests if r[1].endswith("googleAds:mutate")][-1]
        self.assertTrue(last[3]["validateOnly"])

    def test_a_transient_google_error_postpones_the_step(self):
        self.e.mock.fail_next(503, None, "átmeneti", n=100, path="googleAds:mutate")
        res = self.fweekly()
        self.assertIn("jogosultság-ellenőrzés most nem sikerült", " ".join(res.report["notes"]))
        self.assertEqual(self.oai_srv.requests, [])


class PackImagesTests(FactoryBase):
    def test_pack_images_that_did_not_fit_at_launch_are_added_by_the_weekly_step(self):
        for rn, c in list(self.state("campaignAsset").items()):                          # az indításkori feltöltés „nem sikerült”
            if c["fieldType"] == "AD_IMAGE":
                del self.state("campaignAsset")[rn]
        for rn, a in list(self.state("asset").items()):
            if a.get("type") == "IMAGE":
                del self.state("asset")[rn]
        self.e.store.delete("pacsi.creatives")
        res = self.fweekly()
        [a] = self.uploads(res)
        self.assertEqual(a.status, "applied")
        self.assertEqual(sorted(set(a.detail["sources"].values())), ["crop", "pack"])
        self.assertEqual(len([s for s in a.detail["sources"].values() if s == "pack"]), 3)
        self.assertEqual(len(self.links("ENABLED")), 6)
        self.assertEqual(len([e for e in self.reg().entries() if e["source"] == "pack" and e["status"] == "uploaded"]), 3)

    def test_an_image_somebody_removed_is_not_added_back(self):
        self.fweekly()                                                                 # a pack-képek nyilvántartásba kerülnek
        gone = next(rn for rn, c in self.state("campaignAsset").items() if c["fieldType"] == "AD_IMAGE" and "highfive" not in rn)
        removed_asset = self.state("campaignAsset")[gone]["asset"]
        name = self.state("asset")[removed_asset]["name"]
        del self.state("campaignAsset")[gone]
        res = self.fweekly(today=TODAY + dt.timedelta(days=7))
        self.assertIn("valaki eltávolította a kampányból", " ".join(res.report["notes"]))
        self.assertEqual(self.uploads(res), [])
        self.assertNotIn(name, [self.state("asset")[c["asset"]]["name"] for c in self.links()])



class LateImagesTests(FactoryBase):
    """Új fiók: az indításkor a Google még nem engedte a képeket; a kampány képek nélkül jött létre."""
    REJECT_IMAGES_AT_LAUNCH = True

    def test_the_campaign_started_without_images_and_the_weekly_step_adds_them_once_allowed(self):
        self.assertEqual(self.links(), [])
        self.assertEqual(self.reg().entries(), [])
        self.assertEqual(len(self.state("adGroupAd")), 4)                               # a kampány többi része rendben van
        res = self.fweekly()
        [a] = self.uploads(res)
        self.assertEqual((a.status, len(a.detail["names"])), ("applied", 6))
        self.assertEqual(sorted(set(a.detail["sources"].values())), ["crop", "pack"])
        self.assertEqual(len(self.links("ENABLED")), 6)

    def test_while_the_account_is_not_eligible_the_step_only_waits(self):
        self.e.mock.reject_images = True
        res = self.fweekly()
        self.assertIn("60 napos fiókot", " ".join(res.report["notes"]))
        self.assertEqual(self.links(), [])


class CapacityTests(FactoryBase):
    def seed_images(self, n_weak=2, n_ok=3, n_pack=7):
        """15 kép a kampányban: 3 indításkori + 12 mesterséges (7 védett pack, 3 jó és 2 gyenge motor-kép), mind elég megjelenéssel."""
        made = []
        for i in range(n_weak + n_ok + n_pack):
            made.append(synthetic_prepared(100 + i))
        ops = builder.build_image_ops(CID, self.camp_rn, made)
        self.e.client.mutate(CID, ops)
        old = (TODAY - dt.timedelta(days=30))
        ctr = []
        for i, p in enumerate(made):
            if i < n_weak:
                self.reg().add(p, "ai", {"scene": f"weak {i}"}, "uploaded", today=old)
                ctr.append(0.001 * (i + 1))
            elif i < n_weak + n_ok:
                self.reg().add(p, "ai", {"scene": f"ok {i}"}, "uploaded", today=old)
                ctr.append(0.02)
            else:
                self.reg().add(p, "pack", {"launch": True}, "uploaded", today=old)
                ctr.append(0.025)
        for p, c in zip(made, ctr):
            link = next(rn for rn, a in self.state("campaignAsset").items() if self.state("asset")[a["asset"]].get("name") == p.name)
            self.e.mock.set_metrics(CID, "campaignAsset", link.rsplit("/", 1)[-1], "2026-10-06", impressions=2000, clicks=int(2000 * c))
        return made

    def test_the_weakest_engine_images_make_room_and_pack_images_are_never_paused(self):
        made = self.seed_images()
        self.assertEqual(len(self.links("ENABLED")), 15)
        res = self.fweekly()
        pauses = [a for a in res.actions if a.kind == "pause_image"]
        self.assertEqual(sorted(a.target for a in pauses), sorted(p.name for p in made[:2]))
        self.assertTrue(all(a.status == "applied" and "gyenge kattintási arány" in a.reason for a in pauses))
        for p in made[:2]:
            link = next(c for c in self.state("campaignAsset").values() if self.state("asset")[c["asset"]].get("name") == p.name)
            self.assertEqual(link["status"], "PAUSED")                                  # szüneteltetve, nem törölve
            self.assertEqual(self.reg().get(p.name)["status"], "paused")
        [up] = self.uploads(res)
        self.assertEqual(len(up.detail["names"]), 2)                                    # 3 új kép közül 2 fér a felszabadult helyre
        self.assertIn("1 új kép vár helyre", " ".join(res.report["notes"]))
        self.assertEqual(len(self.links("ENABLED")), 15)
        protected = {p.name for p in made[5:]}
        paused_names = {self.state("asset")[c["asset"]].get("name") for c in self.links("PAUSED")}
        self.assertFalse(protected & paused_names)

    def test_young_images_and_images_with_little_data_are_kept(self):
        made = self.seed_images()
        for p in made[:2]:
            self.reg().add(p, "ai", {"scene": "young"}, "uploaded", today=TODAY - dt.timedelta(days=3))      # túl fiatal
        res = self.fweekly()
        self.assertEqual([a for a in res.actions if a.kind == "pause_image"], [])
        self.assertEqual(self.uploads(res), [])
        self.assertIn("vár helyre", " ".join(res.report["notes"]))

    def test_the_floor_of_live_images_is_respected(self):
        rows = [{"asset_name": f"a{i}", "status": "ENABLED", "impressions": 2000, "clicks": 1 if i < 2 else 50, "resource_name": f"rn{i}", "campaign_id": "1"}
                for i in range(4)]
        reg = type("R", (), {"get": staticmethod(lambda n: {"source": "ai", "status": "uploaded", "created": "2026-01-01"})})()
        out = factory._retire_candidates(rows, reg, TODAY, need=5, live_count=4)
        self.assertEqual(len(out), 1)                                                   # 4 kép közül legalább 3 marad


if __name__ == "__main__":
    unittest.main()
