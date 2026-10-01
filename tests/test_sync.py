"""Próbák: napi szinkron és védelmek – túlköltés, keretemelés-védelem, elutasított hirdetés, kézi módosítás, nyitóoldal-őr, levelek."""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import _path  # noqa: F401
import helpers
from ads_engine import alerts, config, launch, net, sync
from ads_engine.executor import WriteRefused
from mock_smtp import MockSMTP
from test_launch import Env

CID = helpers.CID
M = 1_000_000
TZ = ZoneInfo("Europe/Budapest")
TODAY = dt.date(2026, 10, 12)
NOW = dt.datetime(2026, 10, 12, 6, 30, tzinfo=TZ)


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.e = Env(mode="live")
        self.smtp = MockSMTP().start()
        self.e.env.update({"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.port), "SMTP_USER": self.smtp.user, "SMTP_PASSWORD": self.smtp.password,
                           "SMTP_FROM": "Pacsi Ads <hello@pacsit.hu>", "REPORT_TO": "ember@pelda.hu", "SMTP_SECURITY": "plain"})
        self.e.with_mode("live")
        launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)
        self.camp_rn = next(iter(self.e.mock.state[CID]["campaign"]))
        self.budget_rn = next(iter(self.e.mock.state[CID]["campaignBudget"]))
        self.cid_ = self.camp_rn.rsplit("/", 1)[-1]
        self.fetched = []

    def tearDown(self):
        self.smtp.stop()
        self.e.close()

    def go_live(self, weekly=14_000):
        launch.go_live(self.e.settings, self.e.project, self.e.store, self.e.client, 2, weekly)      # napi 2 000

    def fetch_ok(self, url, hosts, **kw):
        self.fetched.append(url)
        return net.Fetched(200, {}, b"<html>ok</html>", url)

    def run_sync(self, today=TODAY, now=NOW, fetch=None):
        return sync.run_sync(self.e.settings, self.e.project, self.e.store, self.e.client, 9, today=today, now=now,
                             fetch=fetch or self.fetch_ok, sleep=lambda s: None)

    def cost(self, day, micros):
        self.e.mock.set_metrics(CID, "campaign", self.cid_, day, clicks=10, impressions=100, costMicros=micros)

    def status(self):
        return self.e.mock.state[CID]["campaign"][self.camp_rn]["status"]

    def mails(self):
        return self.smtp.messages


class GuardTests(SyncBase):
    def test_no_findings_for_a_healthy_campaign(self):
        self.go_live()
        self.cost("2026-10-11", 1_500 * M)
        r = self.run_sync()
        self.assertEqual([f for f in r.findings if f.level == "pause"], [])
        self.assertEqual(self.status(), "ENABLED")
        self.assertEqual(self.mails(), [])
        self.assertEqual(r.cost_yesterday, 1_500 * M)

    def test_overspend_pauses_campaign_and_emails_even_in_dry_mode(self):
        self.go_live()
        self.cost("2026-10-11", 4_300 * M)                         # a napi 2 000 több mint 2,1-szerese
        self.e.with_mode("dry")
        r = self.run_sync()
        self.assertEqual([f.code for f in r.findings if f.level == "pause"], ["daily_overspend"])
        self.assertEqual(self.status(), "PAUSED")                  # a fék dry módban is él
        self.assertEqual(len(self.mails()), 1)
        self.assertIn("FÉK", self.mails()[0]["Subject"])
        self.assertIn("confirm-budget", self.smtp.text_of(self.mails()[0]))
        self.assertTrue(self.e.store.get("pacsi.guard_pause"))

    def test_typo_in_budget_pauses_and_asks_for_confirmation(self):
        self.go_live()
        self.e.mock.state[CID]["campaignBudget"][self.budget_rn]["amountMicros"] = str(20_000 * M)       # extra nulla
        r = self.run_sync()
        self.assertIn("budget_confirm", [f.code for f in r.findings])
        self.assertEqual(self.status(), "PAUSED")
        self.assertEqual(self.e.store.get("pacsi.approved_daily_micros"), 2_000 * M)                 # NEM íródik át
        self.assertTrue(self.e.store.get("pacsi.needs_budget_confirmation"))
        self.assertIn("erősíted", self.smtp.text_of(self.mails()[0]))

    def test_small_raise_is_adopted_and_noted_without_email(self):
        self.go_live()
        self.e.mock.state[CID]["campaignBudget"][self.budget_rn]["amountMicros"] = str(3_000 * M)
        r = self.run_sync()
        self.assertEqual([f.code for f in r.findings], ["budget_raised"])
        self.assertEqual(self.e.store.get("pacsi.approved_daily_micros"), 3_000 * M)
        self.assertEqual(self.status(), "ENABLED")
        self.assertEqual(self.mails(), [])
        self.assertIn("felemelted", self.e.store.get("pacsi.notices")[0]["text"])

    def test_enabled_without_approval_is_paused(self):
        self.e.mock.state[CID]["campaign"][self.camp_rn]["status"] = "ENABLED"         # a felhasználó bekapcsolta, de nincs go-live
        r = self.run_sync()
        self.assertEqual([f.code for f in r.findings], ["not_approved"])
        self.assertEqual(self.status(), "PAUSED")

    def test_paused_before_go_live_is_quiet(self):
        r = self.run_sync()
        self.assertEqual(r.findings, [])
        self.assertEqual(self.mails(), [])

    def test_monthly_limit_uses_budget_history(self):
        self.go_live()
        for i in range(1, 31):
            self.cost((TODAY - dt.timedelta(days=i)).isoformat(), 1_000 * M)      # 30 000: a keret-előzményből várt összeg 60 000 → rendben
        r = self.run_sync()
        self.assertEqual([f for f in r.findings if f.level == "pause"], [])
        self.assertEqual(len(self.e.store.get("pacsi.budget_history")), 1)

    def test_alert_is_not_repeated_on_the_same_day(self):
        self.go_live()
        self.cost("2026-10-11", 4_300 * M)
        self.run_sync()
        self.e.mock.state[CID]["campaign"][self.camp_rn]["status"] = "ENABLED"
        self.run_sync()
        self.assertEqual(len(self.mails()), 1)


class DisapprovalAndHumanTests(SyncBase):
    def test_disapproved_ad_is_reported_once(self):
        self.go_live()
        ad = next(iter(self.e.mock.state[CID]["adGroupAd"]))
        self.e.mock.state[CID]["adGroupAd"][ad]["policySummary"] = {"approvalStatus": "DISAPPROVED", "reviewStatus": "REVIEWED"}
        r = self.run_sync()
        self.assertEqual(len(r.disapproved), 1)
        self.assertEqual(len(self.mails()), 1)
        self.assertIn("elutasított", self.mails()[0]["Subject"])
        self.run_sync(today=TODAY + dt.timedelta(days=1), now=NOW + dt.timedelta(days=1))
        self.assertEqual(len(self.mails()), 1)                         # 3 napig nem ismétli

    def test_human_pause_detected_from_snapshot_and_change_event(self):
        self.go_live()
        self.run_sync()                                                # alapállapot: pillanatkép
        self.e.mock.state[CID]["campaign"][self.camp_rn]["status"] = "PAUSED"      # ember szüneteltette a felületen
        self.e.mock.state[CID]["changeEvent"] = {"x": {"resourceName": "x", "changeDateTime": "2026-10-12 05:00:00", "changeResourceName": self.camp_rn,
                                                       "campaign": self.camp_rn, "clientType": "GOOGLE_ADS_WEB_CLIENT", "userEmail": "ember@pelda.hu"}}
        r = self.run_sync(today=TODAY + dt.timedelta(days=1), now=NOW + dt.timedelta(days=1))
        self.assertTrue(r.human_changes)
        self.assertIn(self.camp_rn, self.e.store.get("pacsi.hands_off"))
        self.assertTrue(any("Kézzel módosítottál" in m["Subject"] for m in self.mails()))
        self.assertEqual(self.status(), "PAUSED")                      # a motor nem kapcsolja vissza

    def test_own_writes_are_not_mistaken_for_human_edits(self):
        self.go_live()
        self.run_sync()
        # a motor saját fékje (szünet) után a következő szinkron nem tekinti emberi módosításnak
        self.cost("2026-10-12", 9_000 * M)
        self.run_sync(today=TODAY + dt.timedelta(days=1), now=NOW + dt.timedelta(days=1))
        r = self.run_sync(today=TODAY + dt.timedelta(days=2), now=NOW + dt.timedelta(days=2))
        self.assertEqual(r.human_changes, [])

    def test_google_added_ad_noted(self):
        self.go_live()
        ad = next(iter(self.e.mock.state[CID]["adGroupAd"]))
        self.e.mock.state[CID]["adGroupAd"][ad]["ad"]["addedByGoogleAds"] = True
        r = self.run_sync()
        self.assertTrue(any("magától adott hozzá" in n for n in r.notes))

    def test_no_campaigns_yet(self):
        e = Env(mode="live")
        try:
            r = sync.run_sync(e.settings, e.project, e.store, e.client, 1, today=TODAY, now=NOW, fetch=self.fetch_ok)
            self.assertEqual(r.campaigns, [])
            self.assertIn("launch", r.notes[0])
        finally:
            e.close()


class LandingGuardTests(SyncBase):
    def flaky(self, ok_urls_after=None):
        state = {"down": True}

        def fetch(url, hosts, **kw):
            if state["down"]:
                raise net.FetchError("HTTP 503")
            return net.Fetched(200, {}, b"ok", url)
        return fetch, state

    def guard(self, fetch):
        return sync.run_landing_guard(self.e.settings, self.e.project, self.e.store, self.e.client, 9, fetch=fetch, sleep=lambda s: None)

    def test_two_failed_checks_pause_and_two_good_ones_resume(self):
        self.go_live()
        fetch, state = self.flaky()
        self.assertIsNone(self.guard(fetch)["action"])                 # az első hiba még nem fék
        self.assertEqual(self.status(), "ENABLED")
        r = self.guard(fetch)
        self.assertEqual(r["action"], "paused")
        self.assertEqual(self.status(), "PAUSED")
        self.assertTrue(any("nem elérhető" in m["Subject"] for m in self.mails()))
        state["down"] = False
        self.assertIsNone(self.guard(fetch)["action"])                 # egy jó ellenőrzés még kevés
        self.assertEqual(self.guard(fetch)["action"], "resumed")
        self.assertEqual(self.status(), "ENABLED")

    def test_a_big_landing_page_is_not_a_false_alarm(self):
        """Éles hiba volt: a pacsit.hu főoldala egyfájlos PWA (~460 KB), a korábbi 300 KB-os korlát miatt „nem elérhetőnek” látszott."""
        self.e.site.overrides["/"] = (200, {"Content-Type": "text/html"}, b"x" * 3_000_000)
        p = config.Project(slug="x", name="X", site=self.e.site.base, brief_url=self.e.site.base + "/ads/brief.json", allowed_hosts=("127.0.0.1",))
        ok, problems = sync.landing_check(p, None, **self.e.kw)
        self.assertEqual((ok, problems), (True, []))

    def test_utm_loss_is_reported_but_not_a_pause(self):
        self.go_live()
        r = self.guard(lambda url, hosts, **kw: net.Fetched(200, {}, b"ok", "https://pacsit.hu/"))
        self.assertTrue(r["ok"])
        self.assertTrue(any("UTM" in p for p in r["problems"]))
        self.assertEqual(self.status(), "ENABLED")

    def test_does_not_resume_if_budget_guard_paused_it(self):
        self.go_live()
        fetch, state = self.flaky()
        self.guard(fetch)
        self.guard(fetch)
        self.e.store.put("pacsi.guard_pause", {"codes": ["daily_overspend"]})
        state["down"] = False
        self.guard(fetch)
        self.assertIsNone(self.guard(fetch)["action"])
        self.assertEqual(self.status(), "PAUSED")

    def test_site_down_without_enabled_campaign_does_nothing(self):
        fetch, _ = self.flaky()
        self.guard(fetch)
        self.assertIsNone(self.guard(fetch)["action"])

    def test_landing_urls_come_from_the_cached_brief(self):
        self.e.store.put("pacsi.cache.brief", {"landing_pages": [{"id": "kviz", "url": "https://pacsit.hu/kviz"}]})
        urls = sync.landing_urls(self.e.project, self.e.store)
        self.assertEqual(urls[0], "https://pacsit.hu/")
        self.assertIn("https://pacsit.hu/kviz", urls)

    def test_stop_file_blocks_resume_but_not_pause(self):
        self.go_live()
        fetch, state = self.flaky()
        self.guard(fetch)
        self.guard(fetch)
        (self.e.settings.data_dir / "STOP").write_text("x")
        state["down"] = False
        self.guard(fetch)
        with self.assertRaises(WriteRefused):
            self.guard(fetch)


class AlertsTests(unittest.TestCase):
    def test_mail_failure_does_not_raise_and_is_not_marked_sent(self):
        from ads_engine.store import Store
        from ads_engine import mailer
        store = Store(":memory:")
        project = config.load({}).project("pacsi")

        def boom(*a, **k):
            raise mailer.MailError("nincs SMTP")
        self.assertFalse(alerts.notify(config.load({}), store, project, "k", "tárgy", ["sor"], send=boom))
        self.assertEqual(store.get("pacsi.alerts", {}), {})
        sent = []
        self.assertTrue(alerts.notify(config.load({}), store, project, "k", "tárgy", ["sor"], send=lambda *a, **k: sent.append(a)))
        self.assertFalse(alerts.notify(config.load({}), store, project, "k", "tárgy", ["sor"], send=lambda *a, **k: sent.append(a)))
        self.assertTrue(alerts.notify(config.load({}), store, project, "k", "tárgy", ["sor"], send=lambda *a, **k: sent.append(a),
                                      now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)))
        self.assertEqual(len(sent), 2)
        self.assertIn("Ads Engine v", sent[0][2])                   # a lábléc a verziót mutatja


if __name__ == "__main__":
    unittest.main()
