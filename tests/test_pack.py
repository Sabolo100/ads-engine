"""Próbák: biztonságos letöltés, sémaellenőrző, szöveg- és kulcsszó-validátorok, Ads Pack betöltése, képek."""
import copy
import io
import json
import pathlib
import tempfile
import unittest

import _path  # noqa: F401
from PIL import Image

from ads_engine import images, jsonschema_lite, net, pack, validators as v
from ads_engine.config import Project
from ads_engine.store import Store
from pack_server import PackServer

ROOT = pathlib.Path(_path.ROOT)
EXAMPLE = ROOT / "examples" / "pacsi"
BRIEF = json.loads((EXAMPLE / "brief.json").read_text(encoding="utf-8"))
CREATIVES = json.loads((EXAMPLE / "creatives.json").read_text(encoding="utf-8"))


def png_bytes(w, h, color=(200, 120, 60), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ net
class NetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = PackServer(EXAMPLE).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def fetch(self, path, hosts=("127.0.0.1",), **kw):
        kw.setdefault("allow_private", True)
        kw.setdefault("allow_http", True)
        return net.fetch(self.srv.base + path, hosts, **kw)

    def test_fetches_allowed_host(self):
        r = self.fetch("/ads/brief.json")
        self.assertEqual(r.status, 200)
        self.assertIn("schema_version", r.text())

    def test_http_is_refused_by_default(self):
        with self.assertRaises(net.FetchError) as cm:
            net.fetch(self.srv.base + "/ads/brief.json", ("127.0.0.1",), allow_private=True)
        self.assertIn("https", str(cm.exception))

    def test_host_must_be_allowed(self):
        with self.assertRaises(net.FetchError) as cm:
            self.fetch("/ads/brief.json", hosts=("pacsit.hu",))
        self.assertIn("nincs az engedélyezettek", str(cm.exception))

    def test_private_address_refused_without_flag(self):
        with self.assertRaises(net.FetchError) as cm:
            net.fetch(self.srv.base + "/ads/brief.json", ("127.0.0.1",), allow_http=True)
        self.assertTrue("nem nyilvános" in str(cm.exception) or "port" in str(cm.exception))

    def test_userinfo_and_odd_port_refused(self):
        with self.assertRaises(net.FetchError):
            net.check_url("https://user:pw@pacsit.hu/x", ("pacsit.hu",))
        with self.assertRaises(net.FetchError):
            net.check_url("https://pacsit.hu:8443/x", ("pacsit.hu",))

    def test_is_public_ip(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.1.1", "::1", "0.0.0.0", "172.16.0.9"):
            self.assertFalse(net.is_public_ip(ip), ip)
        self.assertTrue(net.is_public_ip("8.8.8.8"))

    def test_redirect_to_disallowed_host_blocked(self):
        with self.assertRaises(net.FetchError):
            self.fetch("/redirect-evil")

    def test_redirect_within_allowed_host_followed_and_loop_limited(self):
        self.assertEqual(self.fetch("/redirect-ok").status, 200)
        with self.assertRaises(net.FetchError) as cm:
            self.fetch("/redirect-loop")
        self.assertIn("átirányítás", str(cm.exception))

    def test_size_limit(self):
        with self.assertRaises(net.FetchError) as cm:
            self.fetch("/big", max_bytes=1000)
        self.assertIn("túl nagy", str(cm.exception))

    def test_truncate_reads_only_the_start_of_a_big_page(self):
        r = self.fetch("/big", max_bytes=1000, truncate=True)             # a nagy (egyfájlos PWA) oldal elérhető, csak az elejét olvassuk
        self.assertEqual((r.status, len(r.body)), (200, 1000))
        self.assertEqual(self.fetch("/big", max_bytes=10_000_000, truncate=True).status, 200)

    def test_404_and_etag_304(self):
        with self.assertRaises(net.FetchError):
            self.fetch("/ads/nincs.json")
        first = self.fetch("/ads/brief.json")
        again = self.fetch("/ads/brief.json", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status, 304)

    def test_connection_refused_is_fetch_error(self):
        with self.assertRaises(net.FetchError):
            net.fetch("http://127.0.0.1:1/x", ("127.0.0.1",), allow_private=True, allow_http=True, timeout=2)


# ------------------------------------------------------------------ jsonschema_lite
class SchemaTests(unittest.TestCase):
    def test_examples_conform(self):
        self.assertEqual(jsonschema_lite.validate(pack.load_schema("ads-brief.schema.json"), BRIEF), [])
        self.assertEqual(jsonschema_lite.validate(pack.load_schema("creative-pack.schema.json"), CREATIVES), [])

    def test_messages_are_hungarian_with_paths(self):
        b = copy.deepcopy(BRIEF)
        del b["voice"]
        b["project"]["language"] = "magyar"
        b["landing_pages"][0]["url"] = "http://x"
        b["nincs"] = 1
        errs = " | ".join(jsonschema_lite.validate(pack.load_schema("ads-brief.schema.json"), b))
        self.assertIn("$: hiányzik a kötelező mező: voice", errs)
        self.assertIn("$.project.language", errs)
        self.assertIn("$.landing_pages[0].url", errs)
        self.assertIn("$.nincs: ismeretlen mező", errs)

    def test_basic_keywords(self):
        s = {"type": "object", "required": ["a"], "additionalProperties": False,
             "properties": {"a": {"type": "integer", "minimum": 1, "maximum": 5}, "b": {"type": "array", "minItems": 1, "maxItems": 2,
                                                                                         "uniqueItems": True, "items": {"enum": ["x", "y", "z"]}},
                            "c": {"type": ["string", "null"], "minLength": 2, "maxLength": 3, "pattern": "^[a-z]+$"}}}
        self.assertEqual(jsonschema_lite.validate(s, {"a": 3, "b": ["x"], "c": None}), [])
        e = " | ".join(jsonschema_lite.validate(s, {"a": 9, "b": ["x", "x", "q"], "c": "ABCD"}))
        for frag in ("legfeljebb 5", "legfeljebb 2 elem", "nem ismétlődhetnek", "érvénytelen érték", "legfeljebb 3 karakter", "nem megfelelő formátum"):
            self.assertIn(frag, e)
        self.assertTrue(jsonschema_lite.validate(s, {"a": True}))          # a bool nem egész szám
        self.assertTrue(jsonschema_lite.validate(s, [1]))

    def test_refs_const_anyof(self):
        s = {"$defs": {"n": {"type": "integer"}}, "type": "object", "properties": {"x": {"$ref": "#/$defs/n"}, "k": {"const": 1},
                                                                                     "o": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
        self.assertEqual(jsonschema_lite.validate(s, {"x": 1, "k": 1, "o": "s"}), [])
        self.assertEqual(len(jsonschema_lite.validate(s, {"x": "a", "k": 2, "o": 1.5})), 3)


# ------------------------------------------------------------------ validators
class ValidatorTests(unittest.TestCase):
    def codes(self, issues):
        return {i.code for i in issues}

    def test_norm_strips_accents(self):
        self.assertEqual(v.norm("  Kőrösi  ŰRLAP "), "korosi urlap")

    def test_headline_rules(self):
        self.assertIn("exclamation_in_headline", self.codes(v.check_style("Derítsd ki 1 perc alatt!", "headline")))
        self.assertIn("too_long", self.codes(v.check_style("x" * 31, "headline")))
        self.assertIn("all_caps", self.codes(v.check_style("Ingyenes FELHŐ kvíz", "headline")))
        self.assertNotIn("all_caps", self.codes(v.check_style("Telepíthető PWA app", "headline")))
        self.assertIn("emoji", self.codes(v.check_style("Kutyák 🐾 kvíz", "headline")))
        self.assertIn("contact_in_text", self.codes(v.check_style("Nézd meg: pacsit.hu", "headline")))
        self.assertIn("contact_in_text", self.codes(v.check_style("Hívj: +36 30 123 4567", "headline")))
        self.assertIn("repeated_punctuation", self.codes(v.check_style("Mi?? Mi", "headline")))
        self.assertIn("repeated_punctuation", self.codes(v.check_style("Várj…", "headline")))
        self.assertEqual(v.check_style("Melyik kutya illik hozzád?", "headline"), [])

    def test_description_allows_one_exclamation(self):
        self.assertEqual(v.check_style("Próbáld ki ingyen, most azonnal!", "description"), [])
        self.assertIn("exclamation", self.codes(v.check_style("Hú! Jé! Hát ez!", "description")))

    def test_title_case_is_a_warning(self):
        issues = v.check_style("Pacsi Kutyafajta Választó", "headline")
        self.assertEqual([(i.level, i.code) for i in issues], [("warn", "title_case")])

    def test_path_rules(self):
        self.assertEqual(v.check_style("kutyak", "path"), [])
        self.assertIn("bad_path", self.codes(v.check_style("kutya fajta", "path")))
        self.assertIn("too_long", self.codes(v.check_style("x" * 16, "path")))

    def test_numbers_must_be_facts(self):
        facts = BRIEF["product"]["facts"]
        self.assertEqual(v.check_facts("124 kutyafajta, 10 kérdés, 1 perc, top 5", facts), [])
        issues = v.check_facts("150 kutyafajta", facts)
        self.assertEqual(self.codes(issues), {"unproven_number"})

    def test_claims_need_facts(self):
        facts = BRIEF["product"]["facts"]
        self.assertEqual(v.check_facts("Ingyenes, regisztráció nélkül", facts), [])
        no_free = [f for f in facts if f["id"] not in ("ingyenes", "regisztracio")]
        c = self.codes(v.check_facts("Ingyenes, regisztráció nélkül", no_free))
        self.assertEqual(c, {"unproven_claim"})
        for text in ("Garantált találat", "A legjobb kutyafajta-választó", "Allergiásoknak is jó", "Olcsó megoldás", "Az első és egyetlen"):
            self.assertIn("unproven_claim", self.codes(v.check_facts(text, facts)), text)

    def test_superlative_detection_ignores_legalabb(self):
        self.assertEqual(v.claims_in("legalább egy perc, legfeljebb tíz kérdés"), [])
        self.assertTrue(v.claims_in("a legkönnyebb választás"))

    def test_forbidden_words_accent_insensitive_and_prefix(self):
        issues = v.check_forbidden("Tökéletes társ és garantáltan jó", ["tökéletes", "garantált"])
        self.assertEqual(len(issues), 2)
        self.assertEqual(v.check_forbidden("Jó társ", ["tökéletes"]), [])
        self.assertEqual(self.codes(v.check_forbidden("Hasznosabb a Kutyaverzum", [], ["kutyaverzum"])), {"competitor_brand"})

    def test_dating_like_word_is_forbidden_in_pacsi_brief(self):
        self.assertIn("forbidden_word", self.codes(v.check_text("Párkereső kvíz kutyákhoz", "headline", BRIEF)))

    def test_rsa_counts_duplicates_and_paths(self):
        a = CREATIVES["adsets"][0]
        self.assertEqual(v.errors(v.check_rsa(a["headlines"], a["descriptions"], a.get("path1", ""), a.get("path2", ""), BRIEF)), [])
        issues = v.check_rsa(["Egy cím", "egy CÍM", "Harmadik"], ["Leírás egy", "Leírás egy"], "", "kviz", BRIEF)
        c = self.codes(issues)
        for code in ("duplicate_headline", "duplicate_description", "path_order"):
            self.assertIn(code, c)
        self.assertIn("headline_count", self.codes(v.check_rsa(["a", "b"], ["x", "y"], "", "", BRIEF)))

    def test_keyword_rules(self):
        self.assertEqual(v.check_keyword("kutyafajta választó", BRIEF), [])
        self.assertIn("kw_too_many_words", self.codes(v.check_keyword("a b c d e f g h i j k", BRIEF)))
        self.assertIn("kw_bad_chars", self.codes(v.check_keyword("kutya!!", BRIEF)))
        self.assertIn("kw_bad_chars", self.codes(v.check_keyword("kutya pacsit.hu", BRIEF)))
        self.assertIn("kw_too_long", self.codes(v.check_keyword("x" * 81, BRIEF)))

    def test_new_keyword_must_be_close_to_core(self):
        self.assertEqual(v.check_keyword("kutyafajta tesztek", BRIEF, require_close_to_core=True), [])
        self.assertIn("kw_off_topic", self.codes(v.check_keyword("autó biztosítás", BRIEF, require_close_to_core=True)))

    def test_negative_cannot_block_core_or_positives(self):
        self.assertEqual(v.check_negative("kutyakozmetika", BRIEF), [])
        for bad in ("kutyafajta", "választó", "melyik kutyafajta illik", "kutyafajta választó"):
            self.assertIn("negative_blocks_positive", self.codes(v.check_negative(bad, BRIEF)), bad)
        b = copy.deepcopy(BRIEF)
        self.assertIn("negative_blocks_positive", self.codes(v.check_negative("gyerekbarát", b, ["gyerekbarát kutyafajták"])))


# ------------------------------------------------------------------ pack
class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for src in EXAMPLE.rglob("*"):
            if src.is_file():
                dst = self.root / src.relative_to(EXAMPLE)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
        self.srv = PackServer(self.root).start()
        self.project = Project(slug="pacsi", name="Pacsi", site="https://pacsit.hu", brief_url=f"{self.srv.base}/ads/brief.json",
                               customer_id="2222222222", umami_website_id=BRIEF["tracking"]["umami_website_id"],
                               allowed_hosts=("pacsit.hu", "127.0.0.1"), image_hosts=("127.0.0.1",))
        self.store = Store(":memory:")
        self.kw = {"allow_private": True, "allow_http": True}

    def tearDown(self):
        self.srv.stop()
        self.store.close()
        self.tmp.cleanup()

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_loads_and_validates_the_example_pack(self):
        pk = pack.load(self.project, self.store, **self.kw)
        self.assertEqual(pk.brief["project"]["slug"], "pacsi")
        self.assertEqual(len(pk.creatives["adsets"]), 4)
        self.assertEqual(pk.warnings, [])
        self.assertTrue(pk.changed)
        self.assertRegex(pk.content_hash, r"^[0-9a-f]{12}$")

    def test_second_load_uses_etag_and_reports_unchanged(self):
        pack.load(self.project, self.store, **self.kw)
        n_before = len(self.srv.requests)
        pk = pack.load(self.project, self.store, **self.kw)
        self.assertFalse(pk.changed)
        self.assertTrue(all(r[1] for r in self.srv.requests[n_before:]), "a második kérés If-None-Match fejlécet küldött")
        self.assertEqual(len(pk.creatives["adsets"]), 4)

    def test_change_is_detected(self):
        pack.load(self.project, self.store, **self.kw)
        c = copy.deepcopy(CREATIVES)
        c["version"] = "2026-10-02.1"
        self.write("creatives.json", c)
        pk = pack.load(self.project, self.store, **self.kw)
        self.assertTrue(pk.changed)
        self.assertEqual(pk.creatives["version"], "2026-10-02.1")

    def test_invalid_brief_gives_hungarian_problem_list(self):
        b = copy.deepcopy(BRIEF)
        del b["product"]["facts"]
        self.write("brief.json", b)
        with self.assertRaises(pack.PackError) as cm:
            pack.load(self.project, self.store, **self.kw)
        self.assertTrue(any("facts" in p for p in cm.exception.problems))

    def test_brief_slug_mismatch_and_foreign_landing_host(self):
        b = copy.deepcopy(BRIEF)
        b["project"]["slug"] = "masik"
        b["landing_pages"][0]["url"] = "https://evil.example/"
        self.write("brief.json", b)
        with self.assertRaises(pack.PackError) as cm:
            pack.load(self.project, self.store, **self.kw)
        text = " | ".join(cm.exception.problems)
        self.assertIn("slug", text)
        self.assertIn("evil.example", text)

    def test_creatives_with_unproven_claim_rejected(self):
        c = copy.deepcopy(CREATIVES)
        c["adsets"][0]["headlines"][3] = "A legjobb kutyafajta kvíz"
        self.write("creatives.json", c)
        with self.assertRaises(pack.PackError) as cm:
            pack.load(self.project, self.store, **self.kw)
        self.assertTrue(any("felsőfok" in p for p in cm.exception.problems))

    def test_creatives_unknown_landing_and_duplicate_ids(self):
        c = copy.deepcopy(CREATIVES)
        c["adsets"][1]["landing"] = "nincs"
        c["adsets"][2]["id"] = c["adsets"][0]["id"]
        self.write("creatives.json", c)
        with self.assertRaises(pack.PackError) as cm:
            pack.load(self.project, self.store, **self.kw)
        text = " | ".join(cm.exception.problems)
        self.assertIn("landing_pages", text)
        self.assertIn("ismétlődő", text)

    def test_negative_that_blocks_a_keyword_rejected(self):
        b = copy.deepcopy(BRIEF)
        b["keywords"]["negatives"].append("kutyafajta")
        self.write("brief.json", b)
        with self.assertRaises(pack.PackError) as cm:
            pack.load(self.project, self.store, **self.kw)
        self.assertTrue(any("kizárná" in p for p in cm.exception.problems))

    def test_not_json_and_missing_brief(self):
        (self.root / "brief.json").write_text("{nem json", encoding="utf-8")
        with self.assertRaises(pack.PackError):
            pack.load(self.project, None, **self.kw)
        (self.root / "brief.json").unlink()
        with self.assertRaises(pack.PackError) as cm:
            pack.load(self.project, None, **self.kw)
        self.assertIn("nem tölthető le", cm.exception.problems[0])

    def test_brief_without_creatives_url_loads_empty_creatives(self):
        b = copy.deepcopy(BRIEF)
        del b["creatives_url"]
        self.write("brief.json", b)
        pk = pack.load(self.project, None, **self.kw)
        self.assertEqual(pk.creatives["adsets"], [])

    def test_fetch_image_from_allowed_host_only(self):
        pk = pack.load(self.project, self.store, **self.kw)
        data, url = pack.fetch_image(pk, self.project, "img/osz-tura-1200x628.jpg", **self.kw)
        self.assertGreater(len(data), 1000)
        self.assertTrue(url.endswith("/ads/img/osz-tura-1200x628.jpg"))
        with self.assertRaises(net.FetchError):
            pack.fetch_image(pk, self.project, "https://evil.example/x.jpg", **self.kw)

    def test_umami_id_mismatch_is_only_a_warning(self):
        self.project.umami_website_id = "mas-azonosito"
        pk = pack.load(self.project, None, **self.kw)
        self.assertTrue(any("umami" in w for w in pk.warnings))


# ------------------------------------------------------------------ képek
class ImageTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(images.classify(1200, 628), "landscape")
        self.assertEqual(images.classify(1200, 1200), "square")
        self.assertEqual(images.classify(960, 1200), "portrait")
        self.assertIsNone(images.classify(1000, 500))

    def test_exact_ratio_is_just_resized(self):
        p = images.prepare(png_bytes(2400, 1256), "landscape")
        self.assertEqual((p.width, p.height, p.mode, p.mime), (1200, 628, "exact", "image/jpeg"))

    def test_cover_for_small_difference_pad_for_big(self):
        cover = images.prepare(png_bytes(2048, 1024), "landscape")
        self.assertEqual((cover.mode, cover.width, cover.height), ("cover", 1200, 628))
        pad = images.prepare(png_bytes(3000, 1000), "landscape")
        self.assertEqual(pad.mode, "pad")
        self.assertEqual((pad.width, pad.height), (1200, 628))

    def test_pad_uses_edge_color_for_uniform_borders(self):
        im = Image.new("RGB", (3000, 1000), (251, 246, 238))
        im.paste((10, 10, 10), (1400, 400, 1600, 600))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        p = images.prepare(buf.getvalue(), "landscape", mode="pad")
        out = Image.open(io.BytesIO(p.data)).convert("RGB")
        r, g, b = out.getpixel((5, 5))
        self.assertTrue(abs(r - 251) < 6 and abs(g - 246) < 6 and abs(b - 238) < 6)

    def test_square_and_portrait(self):
        self.assertEqual(images.prepare(png_bytes(1500, 1000), "square").width, 1200)
        p = images.prepare(png_bytes(1536, 2048), "portrait")
        self.assertEqual((p.width, p.height), (960, 1200))

    def test_too_small_source_is_rejected(self):
        with self.assertRaises(images.ImageError) as cm:
            images.prepare(png_bytes(500, 300), "landscape")
        self.assertIn("túl kicsi", str(cm.exception))

    def test_unreadable_image(self):
        with self.assertRaises(images.ImageError):
            images.prepare(b"nem kep", "square")

    def test_size_cap_and_hash_name(self):
        import os
        noisy = Image.frombytes("RGB", (1200, 628), os.urandom(1200 * 628 * 3))
        buf = io.BytesIO()
        noisy.save(buf, "PNG")
        p = images.prepare(buf.getvalue(), "landscape", max_bytes=300_000)
        self.assertLessEqual(len(p.data), 300_000)
        self.assertLess(p.width, 1200)                        # a minőség nem volt elég: kicsinyítés is kellett
        self.assertGreaterEqual(p.width, 600)                 # de a Google minimuma alá nem megy
        with self.assertRaises(images.ImageError):
            images.prepare(buf.getvalue(), "landscape", max_bytes=20_000)
        a = images.prepare(png_bytes(1200, 628), "landscape")
        b = images.prepare(png_bytes(1200, 628), "landscape")
        self.assertEqual(a.name, b.name)
        self.assertRegex(a.name, r"^landscape-[0-9a-f]{12}$")

    def test_transparent_png_gets_white_background(self):
        buf = io.BytesIO()
        Image.new("RGBA", (1200, 1200), (0, 0, 0, 0)).save(buf, "PNG")
        p = images.prepare(buf.getvalue(), "square")
        self.assertEqual(Image.open(io.BytesIO(p.data)).convert("RGB").getpixel((10, 10)), (255, 255, 255))

    def test_kind_for(self):
        self.assertEqual(images.kind_for("1:1", 10, 20), "square")
        self.assertEqual(images.kind_for(None, 1200, 628), "landscape")
        self.assertEqual(images.kind_for(None, 1500, 1000), "landscape")

    def test_example_images_are_ready_for_google(self):
        for name, size in (("osz-tura-1200x628.jpg", (1200, 628)), ("highfive-1200x1200.jpg", (1200, 1200)), ("lineup-1200x628.jpg", (1200, 628))):
            f = EXAMPLE / "img" / name
            with Image.open(f) as im:
                self.assertEqual(im.size, size)
            self.assertLess(f.stat().st_size, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
