"""Alap-próbák: állapottároló, konfiguráció, védelmek, végrehajtó, ellenőrzés (check) és a parancssor."""
import base64
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

import _path  # noqa: F401
import helpers
from ads_engine import checks, cli, config, guardrails as g, log
from ads_engine.executor import Executor, WriteRefused
from ads_engine.google.client import GoogleAdsError
from ads_engine.store import LeaseBusy, Store

CID, MCC = helpers.CID, helpers.MCC
M = 1_000_000


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "e.sqlite"
        self.now = [1000.0]
        self.s = Store(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_kv_roundtrip_and_overwrite(self):
        self.assertEqual(self.s.get("a", "alap"), "alap")
        self.s.put("a", {"x": [1, 2]})
        self.s.put("a", {"x": [3]})
        self.assertEqual(self.s.get("a"), {"x": [3]})
        self.s.delete("a")
        self.assertIsNone(self.s.get("a"))

    def test_persists_across_reopen(self):
        self.s.put("k", 5)
        self.s.close()
        self.s = Store(self.path)
        self.assertEqual(self.s.get("k"), 5)

    def test_lease_exclusive_expiry_and_renew(self):
        self.assertTrue(self.s.acquire("engine", "A", ttl=100))
        self.assertFalse(self.s.acquire("engine", "B", ttl=100))
        self.assertTrue(self.s.acquire("engine", "A", ttl=100))          # a tulajdonos megújíthatja
        self.assertEqual(self.s.lease_holder("engine"), "A")
        self.now[0] += 101
        self.assertIsNone(self.s.lease_holder("engine"))
        self.assertTrue(self.s.acquire("engine", "B", ttl=100))          # a lejárt zárat átvehetjük
        self.s.release("engine", "A")                                    # nem az övé: nem szabadít fel
        self.assertEqual(self.s.lease_holder("engine"), "B")

    def test_lease_context_manager(self):
        with self.s.lease("engine", "A"):
            with self.assertRaises(LeaseBusy):
                with self.s.lease("engine", "B"):
                    pass
        self.assertIsNone(self.s.lease_holder("engine"))

    def test_two_connections_contend(self):
        other = Store(self.path, clock=lambda: self.now[0])
        try:
            self.assertTrue(self.s.acquire("engine", "A"))
            self.assertFalse(other.acquire("engine", "B"))
        finally:
            other.close()

    def test_runs_and_last_run(self):
        r1 = self.s.start_run("pacsi", "sync", "dry")
        self.s.finish_run(r1, "ok", {"n": 1})
        r2 = self.s.start_run("pacsi", "sync", "dry")
        self.s.finish_run(r2, "failed", {"error": "x"})
        self.assertEqual(self.s.last_run("pacsi", "sync")["id"], r2)
        self.assertEqual(self.s.last_run("pacsi", "sync", status="ok")["summary"], {"n": 1})
        self.assertIsNone(self.s.last_run("pacsi", "weekly"))
        self.assertEqual(len(self.s.runs("pacsi")), 2)

    def test_actions_log_filters(self):
        r = self.s.start_run("pacsi", "weekly", "live")
        self.s.log_action(r, "pacsi", "add_negative", "kw1", None, {"text": "x"}, "applied", "ok", "live", "req1")
        self.s.log_action(r, "pacsi", "pause_keyword", "kw2", {"s": "ENABLED"}, {"s": "PAUSED"}, "validated", "", "dry")
        self.assertEqual(len(self.s.actions(run_id=r)), 2)
        a = self.s.actions(kind="add_negative")[0]
        self.assertEqual((a["after"], a["status"], a["request_id"]), ({"text": "x"}, "applied", "req1"))

    def test_jobs_idempotent_marker(self):
        self.assertIsNone(self.s.job("pacsi", "weekly", "2026-W40"))
        self.s.job_mark("pacsi", "weekly", "2026-W40", "done", run_id=3)
        self.s.job_mark("pacsi", "weekly", "2026-W40", "done", run_id=4)
        self.assertEqual(self.s.job("pacsi", "weekly", "2026-W40")["run_id"], 4)

    def test_counters(self):
        self.assertEqual(self.s.counter("pacsi", "ai_images", "2026-W40"), 0)
        self.assertEqual(self.s.counter_add("pacsi", "ai_images", "2026-W40"), 1)
        self.assertEqual(self.s.counter_add("pacsi", "ai_images", "2026-W40", 3), 4)
        self.assertEqual(self.s.counter("pacsi", "ai_images", "2026-W41"), 0)

    def test_snapshots_keep_latest_n(self):
        for i in range(30):
            self.s.snapshot_put("pacsi", "live", {"i": i}, keep=5)
        self.assertEqual(self.s.snapshot_get("pacsi", "live")["data"], {"i": 29})
        n = self.s._db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual(n, 5)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        s = config.load({})
        self.assertEqual(s.mode, "dry")
        self.assertFalse(s.live)
        self.assertEqual(s.api_version, "v25")
        self.assertEqual(s.llm_model, "claude-sonnet-5-5")

    def test_invalid_mode_and_port(self):
        with self.assertRaises(config.ConfigError):
            config.load({"ENGINE_MODE": "talán"})
        with self.assertRaises(config.ConfigError):
            config.load({"SMTP_PORT": "x"})

    def test_repo_projects_file_loads(self):
        s = config.load({})
        p = s.project("pacsi")
        self.assertEqual(p.site, "https://pacsit.hu")
        self.assertEqual(p.max_images_per_week, 10)
        self.assertIn("pacsit.hu", p.allowed_hosts)

    def test_project_selection_errors(self):
        s = config.load({})
        with self.assertRaises(config.ConfigError):
            s.project("nincs")

    def test_projects_validation(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "p.toml"
            f.write_text('[[project]]\nslug="a"\nname="A"\nsite="https://a.hu"\nbrief_url="https://a.hu/ads/brief.json"\nbogus=1\n', encoding="utf-8")
            with self.assertRaises(config.ConfigError) as cm:
                config.load({"PROJECTS_FILE": str(f)})
            self.assertIn("bogus", str(cm.exception))
            f.write_text('[[project]]\nslug="a"\nname="A"\nsite="https://a.hu"\n', encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load({"PROJECTS_FILE": str(f)})
            f.write_text('[[project]]\nslug="a"\nname="A"\nsite="s"\nbrief_url="b"\ncustomer_id="123-456-7890"\n'
                         '[[project]]\nslug="b"\nname="B"\nsite="s"\nbrief_url="b"\n', encoding="utf-8")
            s = config.load({"PROJECTS_FILE": str(f)})
            self.assertEqual(s.projects["a"].customer_id, "1234567890")
            with self.assertRaises(config.ConfigError):
                s.project()                                   # két projekt: meg kell adni
            f.write_text('[[project]]\nslug="a"\nname="A"\nsite="s"\nbrief_url="b"\n[[project]]\nslug="a"\nname="A2"\nsite="s"\nbrief_url="b"\n', encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load({"PROJECTS_FILE": str(f)})

    def test_secrets_registered_for_redaction(self):
        config.load({"OPENAI_API_KEY": "sk-proj-SUPERSECRET123", "UMAMI_PASSWORD": "jelszo-nagyon-titkos"})
        self.assertNotIn("SUPERSECRET123", log.redact("kulcs: sk-proj-SUPERSECRET123"))
        self.assertNotIn("nagyon-titkos", log.redact("pw=jelszo-nagyon-titkos"))


class GuardrailTests(unittest.TestCase):
    def codes(self, **kw):
        return {f.code: f for f in g.check_budget(**kw)}

    def test_no_findings_when_everything_normal(self):
        self.assertEqual(self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_yesterday=1500 * M, cost_30d=20000 * M), {})

    def test_not_approved_pauses_running_campaigns(self):
        f = self.codes(approved_daily=None, live_daily=500 * M)
        self.assertEqual(f["not_approved"].level, "pause")
        self.assertEqual(self.codes(approved_daily=None, live_daily=0), {})

    def test_typo_guard_two_x_requires_confirmation(self):
        f = self.codes(approved_daily=1000 * M, live_daily=10_000 * M)
        self.assertEqual(f["budget_confirm"].level, "pause")
        self.assertIn("erősíted", f["budget_confirm"].message)
        self.assertEqual(g.next_approved(1000 * M, 10_000 * M, list(f.values())), 1000 * M)
        self.assertIn("budget_confirm", self.codes(approved_daily=1000 * M, live_daily=2000 * M))      # pontosan 2×: már megerősítés kell

    def test_small_raise_and_lower_are_adopted(self):
        f = g.check_budget(approved_daily=1000 * M, live_daily=1500 * M)
        self.assertEqual([x.code for x in f], ["budget_raised"])
        self.assertEqual(g.next_approved(1000 * M, 1500 * M, f), 1500 * M)
        f = g.check_budget(approved_daily=1000 * M, live_daily=600 * M)
        self.assertEqual([x.code for x in f], ["budget_lowered"])
        self.assertEqual(g.next_approved(1000 * M, 600 * M, f), 600 * M)

    def test_daily_overspend_threshold_is_2_1x(self):
        self.assertNotIn("daily_overspend", self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_yesterday=2100 * M))
        self.assertIn("daily_overspend", self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_yesterday=2101 * M))

    def test_google_2x_day_does_not_misfire(self):
        """A Google napi 2×-t is költhet: ez még nem baj, csak 2,1× fölött fékezünk."""
        self.assertEqual(self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_yesterday=1990 * M), {})

    def test_monthly_limit_with_and_without_history(self):
        self.assertNotIn("monthly_overspend", self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_30d=30_400 * M))
        self.assertIn("monthly_overspend", self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_30d=30_401 * M))
        # keret-előzménnyel: 25 000 összes keret + 10 % = 27 500
        self.assertNotIn("monthly_overspend", self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_30d=27_400 * M, budget_30d_total=25_000 * M))
        self.assertIn("monthly_overspend", self.codes(approved_daily=1000 * M, live_daily=1000 * M, cost_30d=27_600 * M, budget_30d_total=25_000 * M))

    def test_format_and_weekly(self):
        self.assertEqual(g.fmt(1_234_567 * M), "1 234 567 HUF")
        self.assertEqual(g.weekly_from_daily(1000 * M), 7000 * M)

    def test_ownership_by_label(self):
        row = {"campaign": {"labels": [f"customers/{CID}/labels/77"]}}
        self.assertTrue(g.owned_campaign(row, f"customers/{CID}/labels/77"))
        self.assertFalse(g.owned_campaign(row, f"customers/{CID}/labels/78"))
        self.assertFalse(g.owned_campaign({"campaign": {}}, "x"))

    def test_snapshot_diff_detects_human_edits(self):
        prev = {"c1": {"status": "ENABLED", "name": "A", "amountMicros": "100"}, "c2": {"status": "PAUSED"}}
        cur = {"c1": {"status": "PAUSED", "name": "A", "amountMicros": "900"}}
        ch = g.diff_snapshots(prev, cur)
        self.assertEqual({(c["resource"], c["field"]) for c in ch}, {("c1", "status"), ("c1", "amountMicros"), ("c2", "*")})
        self.assertEqual(g.diff_snapshots(prev, prev), [])

    def test_human_change_events_filter_api_clients(self):
        rows = [
            {"changeEvent": {"changeResourceName": "c1", "clientType": "GOOGLE_ADS_WEB_CLIENT", "userEmail": "ember@x.hu", "changeDateTime": "t"}},
            {"changeEvent": {"changeResourceName": "c1", "clientType": "GOOGLE_ADS_API", "userEmail": "sa@x"}},
            {"changeEvent": {"changeResourceName": "c9", "clientType": "GOOGLE_ADS_WEB_CLIENT", "userEmail": "ember@x.hu"}},     # nem a miénk
            {"changeEvent": {"changeResourceName": "c1", "clientType": "GOOGLE_ADS_AUTOMATED_RULE"}},
            {"changeEvent": {"changeResourceName": "c1", "clientType": "GOOGLE_ADS_RECOMMENDATIONS"}},
        ]
        got = g.human_change_events(rows, {"c1"}, sa_email="sa@x")
        self.assertEqual([x["client"] for x in got], ["GOOGLE_ADS_WEB_CLIENT", "GOOGLE_ADS_AUTOMATED_RULE", "GOOGLE_ADS_RECOMMENDATIONS"])

    def test_never_reenable_what_humans_paused(self):
        self.assertTrue(g.never_reenable("ENABLED", "PAUSED", True))
        self.assertFalse(g.never_reenable("ENABLED", "PAUSED", False))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.mock = helpers.start_mock()
        self.client = helpers.make_client(self.mock)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "e.sqlite")
        self.run = self.store.start_run("pacsi", "test", "x")

    def tearDown(self):
        self.mock.stop()
        self.store.close()
        self.tmp.cleanup()

    def make(self, mode="dry", customer=CID):
        s = config.load({"ENGINE_MODE": mode, "DATA_DIR": self.tmp.name})
        p = s.project("pacsi")
        p.customer_id = customer
        return Executor(self.client, self.store, s, p, self.run), s

    def campaigns(self):
        return self.client.search(CID, "SELECT campaign.id, campaign.status FROM campaign WHERE campaign.status != 'REMOVED'")

    def test_dry_mode_only_validates(self):
        ex, _ = self.make("dry")
        self.assertEqual(ex.apply("launch", helpers.sample_tree(), target="pacsi"), {})
        self.assertEqual(self.campaigns(), [])
        self.assertEqual([a["status"] for a in self.store.actions()], ["validated"])

    def test_live_mode_applies_and_logs_request_id(self):
        ex, _ = self.make("live")
        ex.apply("launch", helpers.sample_tree(), target="pacsi", reason="indulás", before=None, after={"campaign": "x"})
        self.assertEqual(len(self.campaigns()), 1)
        a = self.store.actions()[0]
        self.assertEqual((a["status"], a["mode"]), ("applied", "live"))
        self.assertTrue(a["request_id"])

    def test_rejected_preflight_is_logged_and_nothing_applied(self):
        ex, _ = self.make("live")
        with self.assertRaises(GoogleAdsError):
            ex.apply("launch", helpers.sample_tree(eu=False), target="pacsi")
        self.assertEqual(self.campaigns(), [])
        a = self.store.actions()[0]
        self.assertEqual(a["status"], "rejected")
        self.assertIn("EU", a["reason"])

    def test_stop_file_blocks_normal_writes_but_not_safety_pause(self):
        ex, s = self.make("live")
        ex.apply("launch", helpers.sample_tree(), target="pacsi")
        camp = self.client.search(CID, "SELECT campaign.resource_name FROM campaign")[0]["campaign"]["resourceName"]
        self.client.mutate(CID, [{"campaignOperation": {"update": {"resourceName": camp, "status": "ENABLED"}, "updateMask": "status"}}])
        (s.data_dir / "STOP").write_text("x")
        with self.assertRaises(WriteRefused):
            ex.apply("launch", helpers.sample_tree(name="Más"), target="pacsi")
        ex.pause_campaigns([camp], "próba-fék")
        self.assertEqual(self.client.search(CID, "SELECT campaign.status FROM campaign")[0]["campaign"]["status"], "PAUSED")
        kinds = [(a["kind"], a["status"]) for a in self.store.actions()]
        self.assertIn(("pause_campaigns", "applied"), kinds)
        self.assertIn(("launch", "blocked_stop"), kinds)

    def test_safety_pause_runs_even_in_dry_mode(self):
        ex, _ = self.make("live")
        ex.apply("launch", helpers.sample_tree(), target="pacsi")
        camp = self.client.search(CID, "SELECT campaign.resource_name FROM campaign")[0]["campaign"]["resourceName"]
        self.client.mutate(CID, [{"campaignOperation": {"update": {"resourceName": camp, "status": "ENABLED"}, "updateMask": "status"}}])
        dry, _ = self.make("dry")
        dry.pause_campaigns([camp], "napi túlköltés")
        self.assertEqual(self.client.search(CID, "SELECT campaign.status FROM campaign")[0]["campaign"]["status"], "PAUSED")
        self.assertEqual(self.store.actions(kind="pause_campaigns")[0]["mode"], "safety")

    def test_missing_customer_id_refused(self):
        ex, _ = self.make("live", customer="")
        with self.assertRaises(WriteRefused):
            ex.apply("launch", helpers.sample_tree(), target="pacsi")

    def test_empty_ops_are_noop(self):
        ex, _ = self.make("live")
        self.assertEqual(ex.apply("x", []), {})
        self.assertEqual(self.store.actions(), [])


def make_env(mock, tmp, key=True, login=MCC):
    env = {"DATA_DIR": tmp, "GADS_BASE_URL": mock.base_url}
    if key:
        env["GADS_SA_JSON_B64"] = base64.b64encode(json.dumps(mock.service_account_info()).encode()).decode()
    if login:
        env["GADS_LOGIN_CUSTOMER_ID"] = login
    return env


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.mock = helpers.start_mock()
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = pathlib.Path(self.tmp.name) / "projects.toml"
        self.write_projects("")

    def tearDown(self):
        self.mock.stop()
        self.tmp.cleanup()

    def write_projects(self, customer):
        self.projects.write_text(f'[[project]]\nslug="pacsi"\nname="Pacsi"\nsite="{self.mock.base_url}/"\nbrief_url="{self.mock.base_url}/ads/brief.json"\n'
                                 f'customer_id="{customer}"\ncurrency="HUF"\ntimezone="Europe/Budapest"\n', encoding="utf-8")

    def run_checks(self, key=True, login=MCC):
        from ads_engine import runtime
        env = make_env(self.mock, self.tmp.name, key=key, login=login)
        env["PROJECTS_FILE"] = str(self.projects)
        s = config.load(env)
        client = runtime.google_client(s) if key else None
        sa = runtime.service_account(s)
        res = checks.run_checks(s, s.project("pacsi"), client=client, sa=sa)
        return {c.id: c for c in res}, res

    def test_no_key_first_step_is_the_key(self):
        by, res = self.run_checks(key=False)
        self.assertEqual(by["google.key"].level, "todo")
        self.assertEqual(checks.next_step(res).id, "google.key")
        self.assertIn("3. és 6. lépés", checks.next_step(res).hint)

    def test_service_account_not_added_to_any_account(self):
        self.mock.sa_access.clear()
        by, res = self.run_checks()
        self.assertEqual(by["google.access"].level, "todo")
        self.assertIn("Access and security", by["google.access"].hint)

    def test_project_account_missing_is_next_step(self):
        by, res = self.run_checks()
        self.assertEqual(by["google.token"].level, "ok")
        self.assertEqual(by["google.access"].level, "ok")
        self.assertEqual(by["google.account"].level, "todo")
        self.assertEqual(checks.next_step(res).id, "google.account")

    def test_account_ok_with_warnings(self):
        self.write_projects(CID)
        self.mock.accounts[CID].update(autoTaggingEnabled=True, currencyCode="EUR")
        by, res = self.run_checks()
        self.assertEqual(by["google.account"].level, "ok")
        self.assertEqual(by["google.autotag"].level, "warn")
        self.assertEqual(by["google.currency"].level, "warn")
        self.assertEqual(by["google.identity"].level, "ok")

    def test_manager_account_is_rejected_as_project_account(self):
        self.write_projects(MCC)
        by, _ = self.run_checks()
        self.assertEqual(by["google.account_type"].level, "fail")

    def test_missing_login_customer_id_gives_permission_hint(self):
        self.write_projects(CID)
        by, _ = self.run_checks(login=None)
        self.assertEqual(by["google.account"].level, "fail")
        self.assertIn("login-customer-id", by["google.account"].detail)
        self.assertEqual(by["google.login"].level, "warn")

    def test_identity_verification_pending_is_todo(self):
        self.write_projects(CID)
        self.mock.identity = {"verificationProgram": "ADVERTISER_IDENTITY_VERIFICATION",
                              "verificationProgress": {"programStatus": "PENDING_USER_ACTION", "actionUrl": "https://ads.google.com/verify"},
                              "identityVerificationRequirement": {"verificationCompletionDeadlineTime": "2026-11-01 00:00:00"}}
        by, res = self.run_checks()
        self.assertEqual(by["google.identity"].level, "todo")
        self.assertIn("2026-11-01", by["google.identity"].detail)
        self.assertIn("https://ads.google.com/verify", by["google.identity"].hint)

    def test_auto_apply_recommendations_warned(self):
        self.write_projects(CID)
        self.mock.state[CID]["recommendationSubscription"] = {"x": {"type": "KEYWORD", "status": "ENABLED", "resourceName": "x"}}
        by, _ = self.run_checks()
        self.assertEqual(by["google.autoapply"].level, "warn")
        self.assertIn("KEYWORD", by["google.autoapply"].detail)

    def test_missing_eu_declaration_on_existing_campaign_warned(self):
        self.write_projects(CID)
        self.mock.state[CID]["campaign"] = {"c": {"resourceName": "c", "name": "Régi", "status": "ENABLED", "missingEuPoliticalAdvertisingDeclaration": True}}
        by, _ = self.run_checks()
        self.assertEqual(by["google.eu"].level, "warn")
        self.assertIn("Régi", by["google.eu"].detail)

    def test_bad_login_customer_id_fails(self):
        self.write_projects(CID)
        by, _ = self.run_checks(login="9999999999")
        self.assertEqual(by["google.login"].level, "fail")

    def test_render_shows_next_step(self):
        _, res = self.run_checks()
        text = checks.render(res)
        self.assertIn("KÖVETKEZŐ LÉPÉS", text)
        self.assertIn("ügyfélfiók", text)


class CliTests(unittest.TestCase):
    def run_cli(self, argv, env=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv, env=env if env is not None else {})
        return code, buf.getvalue()

    def test_version(self):
        code, out = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"ads-engine \d+\.\d+\.\d+ \([0-9a-f]{7}\)")

    def test_check_offline_without_key_returns_1_and_names_next_step(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(["check", "--offline"], env={"DATA_DIR": d})
        self.assertEqual(code, 1)
        self.assertIn("KÖVETKEZŐ LÉPÉS", out)
        self.assertIn("szolgáltatásfiók", out)

    def test_status_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(["status"], env={"DATA_DIR": d})
        self.assertEqual(code, 0)
        self.assertIn("Üzemmód: dry", out)
        self.assertIn("Jóváhagyott keret: nincs", out)

    def test_bad_config_exit_code_2(self):
        code, out = self.run_cli(["status"], env={"ENGINE_MODE": "xx"})
        self.assertEqual(code, 2)
        self.assertIn("Beállítási hiba", out)

    def test_build_id_is_stable_and_version_visible(self):
        from ads_engine import version
        self.assertEqual(version.build_id(), version.build_id())
        self.assertRegex(version.label(), r"^v\d+\.\d+\.\d+ · [0-9a-f]{7}$")


if __name__ == "__main__":
    unittest.main()
