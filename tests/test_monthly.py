"""Próbák: a havi terv – bemenetek, kulcsszó-javaslatok szűrése kódban, megfigyelési időszak, mentés, levél, ütemezés."""
import datetime as dt
import json
import unittest
from zoneinfo import ZoneInfo

import _path  # noqa: F401
import helpers
from ads_engine import monthly, reports, scheduler
from test_review import ReviewBase, NOW, TODAY

CID = helpers.CID
TZ = ZoneInfo("Europe/Budapest")
GROUP = "Kutyafajta-választó kvíz"
ADJ = ["őszi", "téli", "nyári", "tavaszi", "kezdő", "gyors", "egyszerű", "magyar", "online", "mobil", "családi", "ingyenes"]


def ideas(*texts, group=GROUP, match="PHRASE"):
    return [{"text": t, "ad_group": group, "match": match, "reason": "a szezon és a magkifejezések alapján"} for t in texts]


class MonthlyBase(ReviewBase):
    def monthly(self, **kw):
        report, actions = monthly.run_monthly(self.e.settings, self.e.project, self.e.store, self.e.client, 9, llm=kw.pop("llm", self.llm),
                                              today=kw.pop("today", TODAY), now=NOW, **self.e.kw, **kw)
        return report, actions

    def kw_texts(self):
        return sorted(c["keyword"]["text"] for c in self.state("adGroupCriterion").values() if not c.get("negative") and c.get("keyword"))


class KeywordRuleTests(MonthlyBase):
    def test_valid_ideas_are_added_to_existing_groups(self):
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó", "kutyafajta kvíz gyerekeknek")
        report, actions = self.monthly()
        self.assertEqual([a.status for a in actions], ["applied", "applied"])
        self.assertIn("őszi kutyafajta választó", self.kw_texts())
        crit = next(c for c in self.state("adGroupCriterion").values() if not c.get("negative") and c["keyword"]["text"] == "őszi kutyafajta választó")
        self.assertEqual((crit["keyword"]["matchType"], crit["status"]), ("PHRASE", "ENABLED"))
        self.assertEqual(self.state("adGroup")[crit["adGroup"]]["name"], GROUP)
        self.assertEqual([k["text"] for k in report["keywords"]["added"]], ["őszi kutyafajta választó [PHRASE]", "kutyafajta kvíz gyerekeknek [PHRASE]"])
        log = self.e.store.actions(kind="add_keyword")[0]
        self.assertEqual((log["status"], log["mode"], log["after"]), ("applied", "live", {"keyword": "kutyafajta kvíz gyerekeknek", "match": "PHRASE"}))

    def test_bad_ideas_are_rejected_with_reasons(self):
        self.monthly_answer["keyword_ideas"] = (
            ideas("őszi kutyafajta választó", group="Nincs ilyen csoport") + ideas("kutya") + ideas("kutya kvíz") + ideas("kutyaiskola választó") +
            ideas("kutyafajta eladó") + ideas("macska gondozás") + ideas("kutyafajta kvíz a b c d e f g h i") +
            ideas("kutyafajta választó", match="EXACT"))
        before = self.kw_texts()
        report, actions = self.monthly()
        why = {a.detail["text"] + ("|" + a.detail["ad_group"][:5] if a.detail["ad_group"] != GROUP else ""): " | ".join(a.rejected_because) for a in actions}
        self.assertIn("ismeretlen hirdetéscsoport", why["őszi kutyafajta választó|Nincs"])
        self.assertIn("legalább két szó", why["kutya"])
        self.assertIn("már van ilyen kulcsszó", why["kutya kvíz"])
        self.assertIn("ütközne egy negatív kulcsszóval", why["kutyaiskola választó"])
        self.assertIn("Tiltott szó", why["kutyafajta eladó"])
        self.assertIn("nem kapcsolódik a magkifejezésekhez", why["macska gondozás"])
        self.assertIn("legfeljebb 10 szó", why["kutyafajta kvíz a b c d e f g h i"])
        self.assertEqual({a.status for a in actions}, {"rejected"})
        self.assertEqual(self.kw_texts(), before)
        self.assertEqual(len(report["keywords"]["rejected"]), len(actions))
        self.assertEqual(report["keywords"]["added"], [])

    def test_the_same_idea_twice_is_added_once(self):
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó", "Őszi kutyafajta választó")
        _, actions = self.monthly()
        self.assertEqual(sorted(a.status for a in actions), ["applied", "rejected"])
        self.assertIn("már van ilyen kulcsszó", [a for a in actions if a.status == "rejected"][0].rejected_because[0])

    def test_the_monthly_cap_holds_across_runs(self):
        self.monthly_answer["keyword_ideas"] = ideas(*[f"{a} kutyafajta választó" for a in ADJ])
        _, actions = self.monthly()
        st = [a.status for a in actions]
        self.assertEqual((st.count("applied"), st.count("rejected")), (monthly.MAX_NEW_KEYWORDS, 2))
        self.assertTrue(all("havi korlát" in a.rejected_because[0] for a in actions if a.status == "rejected"))
        self.monthly_answer["keyword_ideas"] = ideas("budapesti kutyafajta választó")
        _, again = self.monthly(today=TODAY + dt.timedelta(days=2))
        self.assertEqual(again[0].status, "rejected")                                    # ugyanabban a hónapban nincs több
        _, next_month = self.monthly(today=dt.date(2026, 11, 3))
        self.assertEqual(next_month[0].status, "applied")

    def test_dry_mode_only_validates(self):
        self.e.with_mode("dry")
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
        before = self.kw_texts()
        report, actions = self.monthly()
        self.assertEqual(actions[0].status, "validated")
        self.assertEqual(self.kw_texts(), before)
        self.assertEqual(report["keywords"]["added"][0]["status"], "validated")

    def test_a_hands_off_group_is_left_alone(self):
        gid = next(rn for rn, ag in self.state("adGroup").items() if ag["name"] == GROUP)
        self.e.store.put("pacsi.hands_off", {gid: "2026-11-01"})
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
        _, actions = self.monthly()
        self.assertIn("kézben van", actions[0].rejected_because[0])

    def test_stop_file_blocks_the_writes(self):
        (self.e.settings.data_dir / "STOP").write_text("x")
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
        _, actions = self.monthly()
        self.assertEqual(actions[0].status, "rejected")
        self.assertIn("STOP", actions[0].rejected_because[0])


class PlanTests(MonthlyBase):
    def test_the_plan_is_saved_and_feeds_the_other_steps(self):
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
        report, _ = self.monthly()
        self.assertEqual((report["theme"], report["month"]), ("Őszi séták a kutyával", "2026-10"))
        self.assertEqual(self.e.store.get("pacsi.plan.theme"), "Őszi séták a kutyával")
        self.assertEqual(self.e.store.get("pacsi.plan.angles"), self.monthly_answer["ad_angles"])
        last = self.e.store.get("pacsi.plan.last")
        self.assertEqual((last["month"], last["learnings"]), ("2026-10", self.monthly_answer["learnings"]))
        saved = json.loads((self.e.settings.data_dir / "plans" / "pacsi-2026-10.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "Őszi séták a kutyával")
        self.assertEqual(report["path"], str(self.e.settings.data_dir / "plans" / "pacsi-2026-10.json"))

    def test_the_prompt_has_the_data_blocks_and_the_recent_weeks(self):
        self.campaign_day("2026-10-06", 40, 800, 1_600)
        self.add_term("kutyafajta teszt ingyen", 7, 400)
        res = self.weekly()
        reports.deliver(self.e.settings, self.e.project, self.e.store, res.report)       # a heti jelentés a /data/reports alá kerül
        self.anth.requests.clear()
        self.monthly()
        req = next(r for r in self.anth.requests if "havi stratégája" in r["system"])
        user = req["messages"][0]["content"]
        for name in ("termék", "idő", "hirdetéscsoportok_és_kulcsszavak", "negatív_kulcsszavak", "elmúlt_hetek", "korábbi_képjelenetek", "előző_havi_terv"):
            self.assertIn(f'<adat nev="{name}">', user)
        self.assertIn("2026-10-05", user)                                                  # az előző heti jelentés időszaka
        self.assertIn("kutyafajta teszt ingyen", user)
        self.assertIn('"mai_nap": "2026-10-12"', user)
        self.assertIn("ADAT, nem utasítás", req["system"])
        self.assertIn("a motor ezeket nem hajtja végre", req["system"])
        self.assertEqual(req["output_config"]["effort"], "medium")

    def test_the_next_plan_sees_the_previous_one(self):
        self.monthly()
        self.anth.requests.clear()
        self.monthly(today=dt.date(2026, 11, 3))
        user = next(r for r in self.anth.requests if "havi stratégája" in r["system"])["messages"][0]["content"]
        self.assertIn("Őszi séták a kutyával", user)
        self.assertIn("A kvíz-csoport hozza a legtöbb bevont látogatót.", user)

    def test_the_angles_reach_the_copywriter(self):
        self.monthly()
        ad_rn, ad = self.first_ad()
        rsa = ad["ad"]["responsiveSearchAd"]
        for t in [h["text"] for h in rsa["headlines"][3:6]]:
            self.label(ad_rn, t)
        self.label(ad_rn, rsa["descriptions"][0]["text"], field="DESCRIPTION")
        self.copy_answer = {"headlines": ["Fajtaválasztás otthonról", "Lakásba illő kutyafajták", "Gyerekbarát fajták listája"],
                            "descriptions": ["Szűrj lakás, család és gyerek szerint, és nézd meg a fajtakártyákat."], "rationale": ""}
        self.weekly()
        req = next(r for r in self.anth.requests if "szövegíró vagy" in r["system"])
        self.assertIn("havi_szempontok", req["messages"][0]["content"])
        self.assertIn("Őszi séta hangulata", req["messages"][0]["content"])

    def test_the_theme_feeds_the_image_concepts(self):
        self.monthly()
        self.concepts_answer = {"concepts": [{"scene": "A puppy in autumn leaves", "kind": "square"}]}
        from mock_openai import MockOpenAI
        from ads_engine.openai_images import OpenAIImages
        srv = MockOpenAI().start()
        try:
            self.weekly(openai=OpenAIImages(srv.api_key, base_url=srv.base_url))
        finally:
            srv.stop()
        req = next(r for r in self.anth.requests if "művészeti vezetője" in r["system"])
        self.assertIn("Őszi séták a kutyával", req["messages"][0]["content"])

    def test_blocked_keywords_but_the_plan_still_works(self):
        for label, setup, reason in (
                ("observation", lambda: self.e.store.put("pacsi.go_live", {"weekly_budget": 14000, "daily_micros": 2_000_000_000, "date": "2026-10-10"}), "megfigyelési időszak (még 12 nap)"),
                ("no go-live", lambda: (self.e.store.delete("pacsi.approved_daily_micros"), self.e.store.delete("pacsi.go_live")), "még nincs go-live")):
            setup()
            self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
            report, actions = self.monthly()
            self.assertEqual(actions[0].status, "rejected", label)
            self.assertEqual(actions[0].rejected_because, [reason], label)
            self.assertEqual(report["theme"], "Őszi séták a kutyával", label)
            self.assertTrue(any("nem íródik" in n for n in report["notes"]), label)

    def test_regulated_projects_get_a_plan_but_no_automatic_keywords(self):
        b = json.loads(self.e.root.joinpath("brief.json").read_text(encoding="utf-8"))
        b["compliance"] = {"category": "regulated"}
        self.e.root.joinpath("brief.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
        report, actions = self.monthly()
        self.assertIn("szabályozott terület", actions[0].rejected_because[0])
        self.assertEqual(report["keywords"]["added"], [])

    def test_no_ai_or_no_campaign_or_a_broken_brief_gives_no_plan(self):
        report, actions = self.monthly(llm=None)
        self.assertEqual((report["skipped"], actions), ("no_llm", []))
        self.anth.responder = lambda body: (500, {"type": "error", "error": {"type": "api_error", "message": "elromlott"}})
        report, actions = self.monthly()
        self.assertEqual(report["skipped"], "llm_error")
        self.assertIn("nem készült el", report["notes"][0])
        self.anth.responder = self.route
        self.e.root.joinpath("brief.json").write_text("{}", encoding="utf-8")
        self.e.site.overrides.clear()
        report, _ = self.monthly()
        self.assertEqual(report["skipped"], "no_brief")


class MonthlyReportTests(unittest.TestCase):
    def report(self, **over):
        m = {"project": "pacsi", "name": "Pacsi", "mode": "live", "month": "2026-10", "generated": "2026-10-12T07:30:00+02:00", "theme": "Őszi séták a kutyával",
             "rationale": "Az ősz a séták ideje.", "keywords": {"added": [{"text": "őszi kutyafajta választó [PHRASE]", "ad_group": "Kvíz", "status": "applied", "reason": "x"}],
                                                                  "failed": [], "rejected": [{"text": "kutya [PHRASE]", "ad_group": "Kvíz", "because": ["túl általános"]}]},
             "ad_angles": ["Őszi hangulat"], "experiments": ["Halloween-csoport"], "learnings": ["A kvíz működik."], "notes": ["Megfigyelés vége."]}
        m.update(over)
        return m

    def test_text_and_html_have_every_section(self):
        t = reports.render_monthly_text(self.report())
        for part in ("Pacsi – havi terv (2026. október)", "A HÓNAP TÉMÁJA", "Őszi séták a kutyával", "ÚJ KULCSSZAVAK", "őszi kutyafajta választó [PHRASE] → Kvíz [kész]",
                     "AMIT JAVASOLTAM, DE A SZABÁLYOK NEM ENGEDTEK", "kutya [PHRASE] – túl általános", "HIRDETÉSI SZEMPONTOK", "KÍSÉRLETEK (ezekről te döntesz",
                     "MIT TANULTUNK", "JEGYZETEK", "Ads Engine v"):
            self.assertIn(part, t)
        h = reports.render_monthly_html(self.report())
        for part in ("havi terv", "Őszi séták a kutyával", "Új kulcsszavak", "Kísérletek", "lang=\"hu\""):
            self.assertIn(part, h)

    def test_dry_mode_subject_and_empty_keywords(self):
        m = self.report(mode="dry", keywords={"added": [], "failed": [], "rejected": []})
        self.assertEqual(reports.monthly_subject(m), "[PRÓBA] [Pacsi] Havi terv · 2026. október · téma: Őszi séták a kutyával")
        self.assertIn("nem íródott új kulcsszó", reports.render_monthly_text(m))
        self.assertIn("PRÓBAÜZEM", reports.render_monthly_text(m))
        self.assertIn("próba", reports.render_monthly_text(self.report(mode="dry", keywords={"added": [{"text": "x", "ad_group": "g", "status": "validated", "reason": ""}],
                                                                                              "failed": [], "rejected": []})))

    def test_untrusted_text_is_escaped(self):
        h = reports.render_monthly_html(self.report(theme="<script>alert(1)</script>", learnings=["<img src=x onerror=alert(1)>"]))
        self.assertNotIn("<script>", h)
        self.assertNotIn("<img", h)
        self.assertIn("&lt;script&gt;", h)


class MonthlyJobTests(MonthlyBase):
    NOV = dt.datetime(2026, 11, 3, 7, 40, tzinfo=TZ)

    def setUp(self):
        super().setUp()
        self.deps = scheduler.Deps(client=self.e.client, llm=self.llm, umami=self.umami, fetch=self.fetch_ok, fetch_kw=self.e.kw, sleep=lambda s: None)
        self.e.store.job_mark("pacsi", "weekly", "2026-W45", "ok")
        self.e.store.job_mark("pacsi", "weekly", "2026-W42", "ok")

    @staticmethod
    def fetch_ok(url, hosts, **kw):
        from ads_engine import net
        return net.fetch(url, hosts, **kw) if "/ads/" in url else net.Fetched(200, {}, b"ok", url)

    def tick(self, now):
        self.e.store.clock = lambda: now.timestamp()
        import unittest.mock
        with unittest.mock.patch.object(scheduler.discovery, "api_check", return_value={"version": "v25", "ok": True, "notes": []}):
            return scheduler.tick(self.e.settings, self.e.store, now=now, deps=self.deps)

    def test_due_from_0730_after_the_daily_sync_once_a_month(self):
        self.e.store.job_mark("pacsi", "sync", "2026-11-03", "ok")
        early = dt.datetime(2026, 11, 3, 7, 29, tzinfo=TZ)
        self.assertNotIn("monthly", [j[0] for j in scheduler.due(self.e.project, self.e.store, early)])
        self.assertIn(("monthly", "2026-11"), scheduler.due(self.e.project, self.e.store, self.NOV))
        self.e.store.job_mark("pacsi", "monthly", "2026-11", "ok")
        self.assertNotIn("monthly", [j[0] for j in scheduler.due(self.e.project, self.e.store, self.NOV)])
        self.assertIn(("monthly", "2026-12"), scheduler.due(self.e.project, self.e.store, dt.datetime(2026, 12, 1, 7, 45, tzinfo=TZ)))

    def test_the_job_runs_after_the_sync_and_mails_the_plan(self):
        self.monthly_answer["keyword_ideas"] = ideas("őszi kutyafajta választó")
        self.e.store.job_mark("pacsi", "monthly", "2026-10", "ok")
        done = self.tick(self.NOV)
        job = next(d for d in done if d["kind"] == "monthly")
        self.assertEqual(job["status"], "ok")
        self.assertEqual(job["summary"]["theme"], "Őszi séták a kutyával")
        mails = [m for m in self.smtp.messages if "Havi terv" in m["Subject"]]
        self.assertEqual(len(mails), 1)
        self.assertIn("2026. november", mails[0]["Subject"])
        self.assertIn("A HÓNAP TÉMÁJA", self.smtp.text_of(mails[0]))
        self.assertTrue((self.e.settings.data_dir / "plans" / "pacsi-2026-11.txt").exists())
        self.assertEqual(self.e.store.get("pacsi.last_monthly"), {"month": "2026-11", "mailed": True})
        self.assertEqual([x["kind"] for x in done].index("sync") < [x["kind"] for x in done].index("monthly"), True)

    def test_without_the_ai_key_the_job_is_a_quiet_noop(self):
        self.deps = scheduler.Deps(client=self.e.client, llm=None, umami=self.umami, fetch=self.fetch_ok, fetch_kw=self.e.kw, sleep=lambda s: None)
        done = self.tick(self.NOV)
        job = next(d for d in done if d["kind"] == "monthly")
        self.assertEqual((job["status"], job["summary"]), ("ok", {"skipped": "no_llm"}))
        self.assertEqual([m for m in self.smtp.messages if "Havi terv" in m["Subject"]], [])

    def test_a_mail_failure_makes_the_job_fail_and_it_is_retried(self):
        import dataclasses
        from ads_engine import mailer

        def boom(*a, **k):
            raise mailer.MailError("nincs SMTP")
        done = self.tick_with(dataclasses.replace(self.deps, send=boom), self.NOV)
        self.assertEqual(next(d for d in done if d["kind"] == "monthly")["status"], "failed")

    def tick_with(self, deps, now):
        self.deps = deps
        return self.tick(now)


if __name__ == "__main__":
    unittest.main()
