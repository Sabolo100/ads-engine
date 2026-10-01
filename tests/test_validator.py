"""Próbák: az önálló (függőség nélküli) Ads Pack validátor – a generált fájl külön folyamatban fut, a motor szabályaival egyezően."""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import _path  # noqa: F401
from ads_engine import pack
from pack_server import PackServer

ROOT = pathlib.Path(_path.ROOT)
EXAMPLE = ROOT / "examples" / "pacsi"
sys.path.insert(0, str(ROOT / "tools"))
import build_validator  # noqa: E402


class ValidatorCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.script = build_validator.build(pathlib.Path(cls.tmp.name) / "ads_pack_validator.py")
        cls.brief = json.loads((EXAMPLE / "brief.json").read_text(encoding="utf-8"))
        cls.creatives = json.loads((EXAMPLE / "creatives.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_validator(self, *args):
        p = subprocess.run([sys.executable, str(self.script), *args], capture_output=True, text=True, encoding="utf-8",
                           cwd=self.tmp.name, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def make_site(self, brief=None, creatives=None):
        d = pathlib.Path(tempfile.mkdtemp(dir=self.tmp.name))
        for src in EXAMPLE.rglob("*"):
            if src.is_file():
                dst = d / src.relative_to(EXAMPLE)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
        if brief is not None:
            (d / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        if creatives is not None:
            (d / "creatives.json").write_text(json.dumps(creatives, ensure_ascii=False), encoding="utf-8")
        return d

    def test_committed_validator_is_up_to_date(self):
        """A repóba commitolt validator/ads_pack_validator.py megegyezik a forrásokból épülővel (a verzió-sort leszámítva)."""
        import re
        committed = (ROOT / "validator" / "ads_pack_validator.py").read_text(encoding="utf-8")
        fresh = self.script.read_text(encoding="utf-8")
        norm = lambda t: re.sub(r"(Verzió: |VERSION = )[^\n]*", r"\1X", t).replace("\r\n", "\n")
        self.assertEqual(norm(committed), norm(fresh), "Futtasd: python tools/build_validator.py, és commitold a validator/ mappát")

    def test_standalone_file_has_no_package_dependency(self):
        text = self.script.read_text(encoding="utf-8")
        self.assertNotIn("import ads_engine", text)
        self.assertNotIn("from ads_engine", text)
        self.assertIn("GENERÁLT FÁJL", text)

    def test_version_and_help(self):
        code, out = self.run_validator("--version")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"ads_pack_validator v\d+\.\d+\.\d+ · [0-9a-f]{7}")
        code, out = self.run_validator()
        self.assertEqual(code, 2)

    def test_example_pack_passes(self):
        code, out = self.run_validator(str(EXAMPLE / "brief.json"))
        self.assertEqual(code, 0, out)
        self.assertIn("A csomag megfelel", out)
        self.assertIn("4 hirdetéscsoport", out)

    def test_schema_error_in_brief(self):
        b = copy.deepcopy(self.brief)
        del b["product"]["facts"]
        d = self.make_site(brief=b)
        code, out = self.run_validator(str(d / "brief.json"))
        self.assertEqual(code, 1)
        self.assertIn("facts", out)
        self.assertIn("hiba van", out)

    def test_content_errors_in_creatives(self):
        c = copy.deepcopy(self.creatives)
        c["adsets"][0]["headlines"][0] = "A legjobb kutyás kvíz!"
        c["adsets"][1]["descriptions"][0] = "Garantált találat, 150 fajtából."
        d = self.make_site(creatives=c)
        code, out = self.run_validator(str(d / "brief.json"))
        self.assertEqual(code, 1)
        for frag in ("felkiáltójel", "felsőfok", "garancia", "150"):
            self.assertIn(frag, out)

    def test_json_output(self):
        code, out = self.run_validator(str(EXAMPLE / "brief.json"), "--json")
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["errors"], 0)

    def test_missing_file_is_a_clear_message(self):
        code, out = self.run_validator(str(pathlib.Path(self.tmp.name) / "nincs.json"))
        self.assertNotEqual(code, 0)
        self.assertIn("Nem található", out)

    def test_parity_with_the_engine_on_mutated_packs(self):
        """Ugyanarra a hibás csomagra a motor és az önálló fájl ugyanannyi hibát jelez."""
        mutations = []
        for fn in (lambda c: c["adsets"][0]["headlines"].__setitem__(0, "Ingyenes!!"),
                   lambda c: c["adsets"][2]["descriptions"].__setitem__(1, "x" * 91),
                   lambda c: c["sitelinks"][0].__setitem__("landing", "nincs"),
                   lambda c: c["callouts"].append("Tökéletes választás"),
                   lambda c: c["adsets"][3]["keywords"].append({"text": "autó biztosítás!", "match": "PHRASE"}),
                   lambda c: c["adsets"][1].__setitem__("negatives", ["kutyafajták"])):
            c = copy.deepcopy(self.creatives)
            fn(c)
            mutations.append(c)
        for c in mutations:
            engine_errs, _ = pack.validate_creatives(c, self.brief)
            d = self.make_site(creatives=c)
            code, out = self.run_validator(str(d / "brief.json"), "--json")
            data = json.loads(out)
            standalone_errs = [r for r in data["results"] if r["level"] == "error"]
            self.assertEqual(len(standalone_errs), len(engine_errs), (engine_errs, standalone_errs))
            self.assertEqual(code, 1 if engine_errs else 0)


class ValidatorOnlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.script = build_validator.build(pathlib.Path(cls.tmp.name) / "ads_pack_validator.py")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.site = pathlib.Path(tempfile.mkdtemp(dir=self.tmp.name))
        for src in EXAMPLE.rglob("*"):
            if src.is_file():
                dst = self.site / src.relative_to(EXAMPLE)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
        self.srv = PackServer(self.site).start()
        b = json.loads((EXAMPLE / "brief.json").read_text(encoding="utf-8"))
        b["project"]["site"] = self.srv.base
        for p in b["landing_pages"]:
            p["url"] = f"{self.srv.base}/kviz" if p["id"] != "home" else f"{self.srv.base}/"
        self.brief = b
        self.write_brief()

    def tearDown(self):
        self.srv.stop()

    def write_brief(self):
        (self.site / "brief.json").write_text(json.dumps(self.brief, ensure_ascii=False), encoding="utf-8")

    def run_validator(self, *args):
        p = subprocess.run([sys.executable, str(self.script), *args], capture_output=True, text=True, encoding="utf-8", timeout=90)
        return p.returncode, p.stdout + p.stderr

    def test_online_checks_landing_utm_and_images(self):
        code, out = self.run_validator(f"{self.srv.base}/ads/brief.json", "--online", "--allow-http")
        self.assertEqual(code, 0, out)
        self.assertIn("az utm_* paraméterek megmaradnak", out)
        self.assertIn("JPEG 1200×628", out)
        self.assertIn("JPEG 1200×1200", out)

    def test_redirect_that_drops_utm_is_an_error(self):
        self.brief["landing_pages"][1]["url"] = f"{self.srv.base}/drops"
        self.write_brief()
        code, out = self.run_validator(f"{self.srv.base}/ads/brief.json", "--online", "--allow-http")
        self.assertEqual(code, 1)
        self.assertIn("elveszíti a követő paramétereket", out)

    def test_broken_image_and_wrong_ratio(self):
        (self.site / "img" / "lineup-1200x628.jpg").unlink()
        c = json.loads((self.site / "creatives.json").read_text(encoding="utf-8"))
        c["images"][1]["ratio"] = "1.91:1"            # a highfive kép négyzetes
        (self.site / "creatives.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        code, out = self.run_validator(f"{self.srv.base}/ads/brief.json", "--online", "--allow-http")
        self.assertEqual(code, 1)
        self.assertIn("nem tölthető le", out)
        self.assertIn("a megadott arány 1.91:1", out)

    def test_http_needs_the_flag(self):
        code, out = self.run_validator(f"{self.srv.base}/ads/brief.json")
        self.assertNotEqual(code, 0)
        self.assertIn("https", out)


if __name__ == "__main__":
    unittest.main()
