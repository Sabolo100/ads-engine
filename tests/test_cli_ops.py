"""Próbák: az üzemi parancsok (sync, weekly, report, confirm-budget, stop/resume, tick, healthcheck, status) a parancssoron át."""
import pathlib
import unittest

import _path  # noqa: F401
import helpers
from ads_engine import web
from ads_engine.store import Store
from test_launch import Env
from test_review import ReviewBase

CID = helpers.CID
M = 1_000_000


class CliBase(ReviewBase):
    def setUp(self):
        super().setUp()
        self.e.store.clock = __import__("time").time                       # a parancsok a valódi órát használják
        self.e.env.update({"ANTHROPIC_API_KEY": self.anth.api_key, "ANTHROPIC_BASE_URL": self.anth.base_url, "UMAMI_URL": self.um.base_url,
                           "UMAMI_USER": self.um.username, "UMAMI_PASSWORD": self.um.password})
        self.e.with_mode("live")

    def cli(self, *argv):
        return self.e.run_cli(list(argv))

    def campaign_status(self):
        return self.state("campaign")[self.camp_rn]["status"]


class StatusTests(CliBase):
    def test_status_shows_budget_go_live_and_runs(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        for part in ("Üzemmód: live", "Jóváhagyott keret: 2 000 HUF/nap", "(14 000 /hét)", "Éles indulás: 2026-09-01", "Utolsó napi szinkron: még nem volt",
                     "Utolsó heti jelentés: még nem volt", "STOP fájl: nincs", "Zár: szabad"):
            self.assertIn(part, out)

    def test_stop_and_resume_toggle_the_stop_file(self):
        code, out = self.cli("stop")
        self.assertEqual(code, 0)
        self.assertTrue((self.e.settings.data_dir / "STOP").exists())
        self.assertIn("STOP fájl: VAN", self.cli("status")[1])
        self.assertIn("törölve", self.cli("resume")[1])
        self.assertFalse((self.e.settings.data_dir / "STOP").exists())
        self.assertIn("Nincs STOP fájl", self.cli("resume")[1])


class SyncCommandTests(CliBase):
    def test_sync_runs_under_the_lease_and_records_the_run(self):
        code, out = self.cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("Szinkron kész: 1 kampány", out)
        s = Store(self.e.settings.db_path)
        try:
            self.assertEqual(s.get("pacsi.last_sync")["campaigns"], 1)
            self.assertEqual(s.runs("pacsi")[0]["kind"], "sync")
            self.assertIsNone(s.lease_holder("engine"))
        finally:
            s.close()
        self.assertIn("Utolsó napi szinkron:", self.cli("status")[1])

    def test_sync_refuses_when_another_instance_holds_the_lease(self):
        import time
        self.e.store.clock = time.time
        self.assertTrue(self.e.store.acquire("engine", "masik-peldany", 900))
        code, out = self.cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("másik példány", out)

    def test_overspend_is_braked_and_status_and_confirm_flow_work(self):
        self.e.mock.state[CID]["campaignBudget"][next(iter(self.state("campaignBudget")))]["amountMicros"] = str(20_000 * M)      # elírt keret (extra nulla)
        code, out = self.cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("budget_confirm", out)
        self.assertEqual(self.campaign_status(), "PAUSED")
        self.assertIn("megerősítésre vár", self.cli("status")[1])
        # megerősítés nélkül nem történik semmi
        code, out = self.cli("confirm-budget")
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertEqual(self.campaign_status(), "PAUSED")
        # a keret elfogadása visszakapcsolás nélkül
        code, out = self.cli("confirm-budget", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Megerősítve: jóváhagyott napi keret 20 000 HUF", out)
        self.assertIn("--enable", out)
        self.assertEqual(self.campaign_status(), "PAUSED")
        s = Store(self.e.settings.db_path)
        try:
            self.assertEqual(s.get("pacsi.approved_daily_micros"), 20_000 * M)
            self.assertIsNone(s.get("pacsi.needs_budget_confirmation"))
        finally:
            s.close()

    def test_confirm_with_enable_turns_the_braked_campaign_back_on(self):
        self.e.mock.state[CID]["campaignBudget"][next(iter(self.state("campaignBudget")))]["amountMicros"] = str(20_000 * M)
        self.cli("sync")
        self.assertEqual(self.campaign_status(), "PAUSED")
        code, out = self.cli("confirm-budget", "--yes", "--enable")
        self.assertEqual(code, 0, out)
        self.assertIn("Visszakapcsolva: 1 kampány", out)
        self.assertEqual(self.campaign_status(), "ENABLED")
        log = [a for a in self.e.store.actions(kind="confirm_enable")]
        self.assertEqual([a["status"] for a in log], ["applied"])
        self.assertNotIn("megerősítésre vár", self.cli("status")[1])

    def test_a_human_pause_is_not_turned_back_on_by_the_confirmation(self):
        self.state("campaign")[self.camp_rn]["status"] = "PAUSED"                          # ember szüneteltette (nem a fék)
        code, out = self.cli("confirm-budget", "--yes", "--enable")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.campaign_status(), "PAUSED")
        self.assertNotIn("Visszakapcsolva", out)

    def test_enable_needs_live_mode(self):
        self.e.env["ENGINE_MODE"] = "dry"
        code, out = self.cli("confirm-budget", "--yes", "--enable")
        self.assertEqual(code, 1)
        self.assertIn("ENGINE_MODE=live", out)


class WeeklyCommandTests(CliBase):
    def test_live_weekly_needs_confirmation(self):
        code, out = self.cli("weekly")
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertEqual([r for r in self.e.store.runs("pacsi") if r["kind"] == "weekly-manual"], [])        # megerősítés nélkül el sem indul

    def test_weekly_prints_saves_and_optionally_mails_the_report(self):
        code, out = self.cli("weekly", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("heti Google Ads jelentés", out)
        self.assertIn("MIT CSINÁLTAM A HÉTEN", out)
        self.assertEqual(self.smtp.messages, [])                                           # levél csak --mail-lel megy
        saved = list((self.e.settings.data_dir / "reports").glob("pacsi-*.json"))
        self.assertEqual(len(saved), 1)
        code, out = self.cli("weekly", "--yes", "--mail", "--no-ai")
        self.assertEqual(code, 0, out)
        self.assertIn("A levél elment.", out)
        self.assertEqual(len(self.smtp.messages), 1)

    def test_dry_weekly_needs_no_confirmation_and_writes_nothing(self):
        self.e.env["ENGINE_MODE"] = "dry"
        code, out = self.cli("weekly", "--no-ai")
        self.assertEqual(code, 0, out)
        self.assertIn("PRÓBAÜZEM (dry)", out)

    def test_report_command_shows_the_last_report(self):
        code, out = self.cli("report")
        self.assertEqual(code, 1)
        self.assertIn("Még nem készült", out)
        self.cli("weekly", "--yes", "--mail", "--no-ai")
        code, out = self.cli("report")
        self.assertEqual(code, 0)
        self.assertIn("heti Google Ads jelentés", out)
        self.assertIn("(levélben elment)", self.cli("status")[1])


class MonthlyCommandTests(CliBase):
    def test_live_monthly_needs_confirmation(self):
        code, out = self.cli("monthly")
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)

    def test_monthly_prints_the_plan_mails_it_and_status_remembers(self):
        self.monthly_answer["keyword_ideas"] = [{"text": "őszi kutyafajta választó", "ad_group": "Kutyafajta-választó kvíz", "match": "PHRASE", "reason": "szezon"}]
        code, out = self.cli("monthly", "--yes", "--mail")
        self.assertEqual(code, 0, out)
        for part in ("havi terv", "A HÓNAP TÉMÁJA", "Őszi séták a kutyával", "őszi kutyafajta választó [PHRASE] → Kutyafajta-választó kvíz [kész]", "A levél elment."):
            self.assertIn(part, out)
        self.assertEqual(len([m for m in self.smtp.messages if "Havi terv" in m["Subject"]]), 1)
        status = self.cli("status")[1]
        self.assertIn("Utolsó havi terv:", status)
        self.assertIn("(levélben elment)", status)
        self.assertIn("AI-képek ebben a hétben: 0 / 10", status)

    def test_dry_monthly_needs_no_confirmation(self):
        self.e.env["ENGINE_MODE"] = "dry"
        code, out = self.cli("monthly")
        self.assertEqual(code, 0, out)
        self.assertIn("PRÓBAÜZEM (dry)", out)

    def test_monthly_without_the_ai_key_says_why(self):
        self.e.env["ANTHROPIC_API_KEY"] = ""
        code, out = self.cli("monthly", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("ANTHROPIC_API_KEY", out)


class TickAndHealthTests(unittest.TestCase):
    def test_tick_without_a_customer_has_nothing_to_do(self):
        e = Env(mode="live", customer="")
        try:
            code, out = e.run_cli(["tick"])
            self.assertEqual(code, 0)
            self.assertIn("Nincs esedékes feladat.", out)
        finally:
            e.close()

    def test_healthcheck_follows_the_service(self):
        e = Env(mode="dry")
        try:
            server = web.start(web.HealthState(), e.store, e.settings, port=0, host="127.0.0.1")
            try:
                e.env["PORT"] = str(server.server_address[1])
                code, out = e.run_cli(["healthcheck"])
                self.assertEqual((code, out.strip()), (0, "ok (200)"))
            finally:
                server.shutdown()
                server.server_close()
            code, out = e.run_cli(["healthcheck"])
            self.assertEqual(code, 1)
        finally:
            e.close()

    def test_version_and_help_list_the_new_commands(self):
        e = Env(mode="dry")
        try:
            code, out = e.run_cli(["--version"])
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith("ads-engine "))
            from ads_engine import cli
            doc = cli.__doc__
            for cmd in ("serve", "tick", "sync", "weekly", "monthly", "report", "confirm-budget", "stop", "healthcheck"):
                self.assertIn(cmd, doc)
                self.assertIn(cmd, cli.COMMANDS)
        finally:
            e.close()


if __name__ == "__main__":
    unittest.main()
