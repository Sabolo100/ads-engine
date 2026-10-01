"""Próbák: a heti jelentés – formázás, szöveges és HTML levél, AI-értelmezés számjegyek nélkül, mentés, küldés."""
import copy
import json
import pathlib
import tempfile
import unittest

import _path  # noqa: F401
from ads_engine import config, reports
from ads_engine.llm import LLM
from ads_engine.store import Store
from mock_anthropic import MockAnthropic
from mock_smtp import MockSMTP
from test_review import ReviewBase

M = 1_000_000


def sample(**over):
    r = {"project": "pacsi", "name": "Pacsi", "currency": "HUF", "mode": "live", "generated": "2026-10-12T07:00:00+02:00",
         "period": {"start": "2026-10-05", "end": "2026-10-11"}, "approved_daily_micros": 2000 * M, "weekly_budget_micros": 14000 * M,
         "google": {"this": {"impressions": 1800, "clicks": 100, "cost_micros": 3600 * M, "ctr": 100 / 1800, "avg_cpc_micros": 36 * M},
                    "prev": {"impressions": 300, "clicks": 10, "cost_micros": 500 * M, "ctr": 10 / 300, "avg_cpc_micros": 50 * M}},
         "budget": {"weekly_micros": 14000 * M, "spent_micros": 3600 * M, "used": 3600 / 14000},
         "web": {"available": True, "total": {"visits": 10, "engaged": 6, "key_actions": 2}, "cost_per_visit_micros": 360 * M,
                 "engaged_rate": 0.6, "cost_per_engaged_micros": 600 * M},
         "top_keywords": [{"text": "kutyafajta teszt", "match": "PHRASE", "ad_group": "Kvíz", "status": "ENABLED", "clicks": 30, "impressions": 600,
                           "cost_micros": 1500 * M, "quality_score": 0}],
         "top_terms": [{"term": "kutyafajta teszt ingyen", "clicks": 7, "impressions": 70, "cost_micros": 400 * M}],
         "ads": [{"ad_id": "77", "ad_group": "Kvíz", "status": "ENABLED", "approval": "APPROVED", "clicks": 60, "impressions": 900, "cost_micros": 2000 * M,
                  "web": {"visits": 5, "engaged": 3, "key_actions": 1}}],
         "actions": [{"kind": "add_negative", "target": "kutyaház", "reason": "keresési kifejezés: „kutyaház építés” – más téma", "status": "applied", "because": [],
                      "detail": {"text": "kutyaház", "clicks": 4}},
                     {"kind": "rotate_rsa", "target": "hirdetés 77 (csoport 5)", "reason": "a Google 3 szöveget gyengének jelzett", "status": "validated", "because": [],
                      "detail": {"removed": ["Régi cím"], "added": ["Új cím"]}}],
         "rejected": [{"kind": "pause_keyword", "target": "kutya kvíz [PHRASE]", "reason": "x", "status": "rejected", "because": ["hoz bevont látogatást"], "detail": {}}],
         "notes": ["Megfigyelési időszak vége."], "todo": ["Erősítsd meg a keretet."], "notices": [{"ts": "2026-10-08T06:30:00+02:00", "text": "A keretet felemelted."}],
         "observation": {"active": False, "days_left": 0}}
    r.update(over)
    return r


class FormatTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(reports.money(3600 * M), "3 600 Ft")
        self.assertEqual(reports.money(None), "–")
        self.assertEqual(reports.money(1_234_500_000, "EUR"), "1 234,50 EUR")
        self.assertEqual(reports.num(1234567), "1 234 567")
        self.assertEqual(reports.pct(0.1234), "12,3 %")
        self.assertEqual(reports.pct(0.6, 0), "60 %")
        self.assertEqual(reports.pct(None), "–")

    def test_delta(self):
        self.assertEqual(reports.delta(120, 100), "+20 %")
        self.assertEqual(reports.delta(80, 100), "−20 %")
        self.assertEqual(reports.delta(100, 100), "±0 %")
        self.assertEqual(reports.delta(5, 0), "új")
        self.assertEqual(reports.delta(0, 0), "–")

    def test_subject_marks_dry_runs(self):
        self.assertEqual(reports.subject(sample()), "[Pacsi] Heti Google Ads jelentés · 10.05.–10.11. · 3 600 Ft, 100 kattintás")
        self.assertTrue(reports.subject(sample(mode="dry")).startswith("[PRÓBA] "))


class RenderTests(unittest.TestCase):
    def test_text_has_all_sections_and_numbers(self):
        t = reports.render_text(sample())
        for part in ("Pacsi – heti Google Ads jelentés (10.05.–10.11.)", "SZÁMOK", "3 600 Ft", "+620 %", "KERET: heti 14 000 Ft (napi 2 000 Ft) · elköltve 3 600 Ft (26 %)",
                     "A TE TEENDŐD", "Erősítsd meg a keretet.", "MIT CSINÁLTAM A HÉTEN", "Új negatív kulcsszó: kutyaház", "[kész]",
                     "Hirdetésszöveg-csere", "kihullott: Régi cím", "új: Új cím", "[próba: a Google elfogadta, de nem írtam]",
                     "AMIT JAVASOLTAM, DE A SZABÁLYOK NEM ENGEDTEK", "hoz bevont látogatást", "LEGTÖBB KATTINTÁST HOZÓ KULCSSZAVAK", "LEGGYAKORIBB KERESÉSEK",
                     "HIRDETÉSEK", "5 látogatás / 3 bevont", "JEGYZETEK", "A keretet felemelted.", "költség / bevont látogatás: 600 Ft", "Ads Engine v"):
            self.assertIn(part, t)
        self.assertNotIn("None", t)
        self.assertNotIn("PRÓBAÜZEM", t)

    def test_dry_mode_is_announced(self):
        self.assertIn("PRÓBAÜZEM (dry)", reports.render_text(sample(mode="dry")))
        self.assertIn("Próbaüzem (dry)", reports.render_html(sample(mode="dry")))

    def test_html_escapes_untrusted_text(self):
        r = sample()
        r["top_terms"][0]["term"] = "<script>alert(1)</script> & \"x\""
        r["top_keywords"][0]["text"] = "<img src=x onerror=alert(1)>"
        r["actions"][0]["target"] = "<b>negatív</b>"
        html = reports.render_html(r)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>negatív</b>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_html_is_a_complete_email_document(self):
        html = reports.render_html(sample())
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('lang="hu"', html)
        self.assertIn("max-width:640px", html)
        self.assertIn("Ads Engine v", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<link", html)                                   # nincs külső erőforrás

    def test_deterministic_summary_variants(self):
        quiet = sample(google={"this": {"impressions": 0, "clicks": 0, "cost_micros": 0, "ctr": 0, "avg_cpc_micros": 0},
                               "prev": {"impressions": 0, "clicks": 0, "cost_micros": 0, "ctr": 0, "avg_cpc_micros": 0}}, actions=[], web={"available": False, "total": None})
        self.assertIn("nem kaptak megjelenést", " ".join(reports.deterministic_summary(quiet)))
        obs = sample(actions=[], observation={"active": True, "days_left": 5})
        self.assertIn("Megfigyelési időszak van még 5 napig", " ".join(reports.deterministic_summary(obs)))
        self.assertIn("2 módosítást ellenőrzött (próba)", " ".join(reports.deterministic_summary(sample(mode="dry"))))
        self.assertIn("2 módosítást hajtott végre", " ".join(reports.deterministic_summary(sample())))

    def test_report_without_data_still_renders(self):
        r = {"project": "pacsi", "name": "Pacsi", "currency": "HUF", "mode": "live", "generated": "2026-10-12T07:00:00+02:00",
             "period": {"start": "2026-10-05", "end": "2026-10-11"}, "actions": [], "rejected": [], "notes": ["Még nincs motor-kampány."], "todo": [], "notices": []}
        self.assertIn("Még nincs motor-kampány.", reports.render_text(r))
        self.assertIn("Még nincs motor-kampány.", reports.render_html(r))
        self.assertIn("[Pacsi] Heti Google Ads jelentés", reports.subject(r))

    def test_many_rejections_are_capped(self):
        r = sample()
        r["rejected"] = [{"kind": "add_negative", "target": f"n{i}", "reason": "x", "status": "rejected", "because": ["ok"], "detail": {}} for i in range(12)]
        self.assertIn("és még 4", reports.render_text(r))
        self.assertIn("és még 4", reports.render_html(r))


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockAnthropic().start()
        self.store = Store(":memory:")
        self.llm = LLM(self.mock.api_key, base_url=self.mock.base_url, store=self.store, max_retries=0)

    def tearDown(self):
        self.mock.stop()
        self.store.close()

    def answer(self, **kw):
        a = {"headline": "Jó hét volt: a látogatók nagy része ténylegesen használta az appot.",
             "paragraphs": ["A kattintások száma nőtt az előző héthez képest.", "A kvíz csoport vitte a forgalmat."], "next_steps": ["Várjuk meg a következő hetet."]}
        a.update(kw)
        self.mock.queue.append(json.dumps(a, ensure_ascii=False))

    def test_narrative_replaces_the_deterministic_summary(self):
        self.answer()
        n = reports.make_narrative(self.llm, "pacsi", sample())
        self.assertIn("Jó hét volt", n.headline)
        text = reports.render_text(sample(), n)
        self.assertIn("Jó hét volt", text)
        self.assertIn("Javasolt következő lépések", text)
        self.assertNotIn("A hét költése", text)                          # a determinisztikus összefoglaló helyére lépett
        self.assertIn("Jó hét volt", reports.render_html(sample(), n))
        req = self.mock.requests[0]
        self.assertIn('<adat nev="heti_adatok">', req["messages"][0]["content"])
        self.assertIn("ne írj egyetlen számjegyet sem", req["system"])
        self.assertEqual(req["output_config"]["effort"], "low")

    def test_numbers_in_the_narrative_are_dropped(self):
        self.answer(paragraphs=["A kattintások száma 100 volt."])
        self.assertIsNone(reports.make_narrative(self.llm, "pacsi", sample()))
        self.answer(next_steps=["Emeld a keretet 20 000 Ft-ra."])
        self.assertIsNone(reports.make_narrative(self.llm, "pacsi", sample()))

    def test_ai_errors_and_missing_key_fall_back(self):
        self.mock.queue.append((500, {"type": "error", "error": {"type": "api_error", "message": "elromlott"}}))
        self.assertIsNone(reports.make_narrative(self.llm, "pacsi", sample()))
        self.assertIsNone(reports.make_narrative(None, "pacsi", sample()))
        self.assertIsNone(reports.make_narrative(self.llm, "pacsi", {"name": "Pacsi", "notes": []}))     # adat nélkül nincs mit értelmezni
        self.assertEqual(len(self.mock.requests), 1)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.smtp = MockSMTP().start()
        self.env = {"DATA_DIR": self.tmp.name, "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.port), "SMTP_USER": self.smtp.user,
                    "SMTP_PASSWORD": self.smtp.password, "SMTP_FROM": "Pacsi Ads <hello@pacsit.hu>", "REPORT_TO": "ember@pelda.hu", "SMTP_SECURITY": "plain",
                    "PROJECTS_FILE": str(pathlib.Path(self.tmp.name) / "nincs.toml")}
        self.settings = config.load(self.env)
        self.project = config.Project(slug="pacsi", name="Pacsi", site="https://pacsit.hu", brief_url="https://pacsit.hu/ads/brief.json")
        self.store = Store(":memory:")

    def tearDown(self):
        self.smtp.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        from ads_engine.reports import Narrative
        n = Narrative(headline="Nyugodt hét.", paragraphs=["Semmi különös."], next_steps=[])
        r = sample()
        path = reports.save(self.settings, r, reports.render_text(r, n), reports.render_html(r, n), n)
        self.assertEqual(path.name, "pacsi-2026-10-11.json")
        for ext in (".txt", ".html"):
            self.assertTrue(path.with_suffix(ext).exists())
        back, back_n = reports.load_saved(path)
        self.assertEqual(back, r)
        self.assertEqual(back_n, n)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw["engine"]), {"version", "build"})

    def test_deliver_sends_text_and_html_and_remembers(self):
        out = reports.deliver(self.settings, self.project, self.store, sample())
        self.assertTrue(out["mailed"])
        [m] = self.smtp.messages
        self.assertIn("Heti Google Ads jelentés", m["Subject"])
        self.assertIn("3 600 Ft", self.smtp.text_of(m))
        self.assertTrue(any(p.get_content_type() == "text/html" for p in m.walk()))
        self.assertEqual(self.store.get("pacsi.last_report")["mailed"], True)

    def test_mail_failure_keeps_the_report_and_says_so(self):
        def boom(*a, **k):
            raise reports.mailer.MailError("nincs SMTP")
        out = reports.deliver(self.settings, self.project, self.store, sample(), send=boom)
        self.assertFalse(out["mailed"])
        self.assertIn("nincs SMTP", out["mail_error"])
        self.assertTrue(pathlib.Path(out["path"]).exists())
        self.assertEqual(self.store.get("pacsi.last_report")["mailed"], False)


class RealReportTests(ReviewBase):
    """A valódi heti körből jövő jelentés is hibátlanul megjelenik."""

    def test_report_from_a_real_weekly_run_renders(self):
        self.campaign_day("2026-10-06", 40, 800, 1_600)
        self.kw_metrics("kutyafajta teszt", 30, 1500)
        self.visits("a", "kutyafajta teszt", 6, engaged=0, ad_id=self.first_ad()[1]["ad"]["id"])
        self.add_term("kutyaház építés", 4, 900)
        self.terms_answer = {"judgements": [{"term": "kutyaház építés", "verdict": "irrelevant", "reason": "más téma", "negative": "kutyaház"}], "summary": ""}
        res = self.weekly()
        text, html = reports.render_text(res.report), reports.render_html(res.report)
        for part in ("Új negatív kulcsszó: kutyaház", "Kulcsszó szüneteltetve: kutyafajta teszt [PHRASE]", "1 600 Ft", "HIRDETÉSEK"):
            self.assertIn(part, text)
        self.assertIn("kutyaház", html)
        self.assertNotIn("None", text)
        out = reports.deliver(self.e.settings, self.e.project, self.e.store, res.report)
        self.assertTrue(out["mailed"])
        back, _ = reports.load_saved(out["path"])
        self.assertEqual(back["period"], res.report["period"])


if __name__ == "__main__":
    unittest.main()
