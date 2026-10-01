"""Próbák: az ütemező (mi esedékes, pótlás, újrapróbálás, zár, függőségek), az állapot-végpont és a szolgáltatás-ciklus."""
import dataclasses
import datetime as dt
import json
import pathlib
import tempfile
import threading
import time
import unittest
import unittest.mock
from zoneinfo import ZoneInfo

import _path  # noqa: F401
import helpers
from ads_engine import config, http, net, review, scheduler, web
from ads_engine.store import LeaseBusy, Store
from test_launch import Env
from test_review import ReviewBase

TZ = ZoneInfo("Europe/Budapest")
M = 1_000_000


def at(h, m=0, day=12):
    return dt.datetime(2026, 10, day, h, m, tzinfo=TZ)


class SchedBase(ReviewBase):
    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(scheduler.discovery, "api_check", return_value={"version": "v25", "ok": True, "notes": []})
        self.api_check = patcher.start()                           # a próbák nem érnek a Google leíró-dokumentumhoz
        self.addCleanup(patcher.stop)
        self.e.store.job_mark("pacsi", "apicheck", "2026-10", "ok")      # a havi ellenőrzés és a havi terv külön tesztekben szerepel
        self.e.store.job_mark("pacsi", "monthly", "2026-10", "ok")
        self.deps = scheduler.Deps(client=self.e.client, llm=self.llm, umami=self.umami, fetch=self.fetch_ok, fetch_kw=self.e.kw, sleep=lambda s: None)

    @staticmethod
    def fetch_ok(url, hosts, **kw):                               # a próbák soha nem érnek a valódi hálózathoz (a nyitóoldal-őr is ezt használja)
        if "/ads/" in url:
            return net.fetch(url, hosts, **kw)
        return net.Fetched(200, {}, b"<html>ok</html>", url)

    def kinds(self, jobs):
        return [j[0] for j in jobs]

    def due(self, now):
        return scheduler.due(self.e.project, self.e.store, now)

    def tick(self, now, **kw):
        self.e.store.clock = lambda: now.timestamp()               # a tárolt időbélyegek a szimulált időt követik
        return scheduler.tick(self.e.settings, self.e.store, now=now, deps=kw.pop("deps", self.deps), **kw)

    def job(self, kind, period):
        return (self.e.store.job("pacsi", kind, period) or {}).get("status")

    def mails(self):
        return [m["Subject"] for m in self.smtp.messages]


class ApiCheckTests(SchedBase):
    NOV = dt.datetime(2026, 11, 3, 7, 0, tzinfo=TZ)                  # új hónap, kedd

    def test_monthly_api_check_is_due_once_a_month_from_0600(self):
        self.assertIn(("apicheck", "2026-11"), self.due(self.NOV))
        self.assertNotIn("apicheck", self.kinds(self.due(dt.datetime(2026, 11, 3, 5, 59, tzinfo=TZ))))
        self.assertNotIn("apicheck", self.kinds(self.due(at(9, 0))))                        # októberben már lefutott

    def test_it_runs_silently_when_there_is_nothing_to_report(self):
        done = self.tick(self.NOV)
        self.assertIn(("apicheck", "ok"), [(d["kind"], d["status"]) for d in done])
        self.api_check.assert_called_once_with("v25", refresh=True)
        self.assertEqual([m for m in self.mails() if "API" in m], [])

    def test_warnings_are_mailed_once_and_a_network_outage_is_not_a_failure(self):
        self.api_check.return_value = {"version": "v25", "ok": True, "notes": ["A v25 120 napon belül lejár."]}
        done = self.tick(self.NOV)
        self.assertEqual([d["status"] for d in done if d["kind"] == "apicheck"], ["ok"])
        mails = [m for m in self.smtp.messages if "API" in m["Subject"]]
        self.assertEqual(len(mails), 1)
        self.assertIn("120 napon belül", self.smtp.text_of(mails[0]))
        self.e.store.job_mark("pacsi", "apicheck", "2026-11", "ok")
        self.api_check.return_value = {"version": "v25", "ok": False, "notes": []}
        done = self.tick(dt.datetime(2026, 12, 1, 7, 0, tzinfo=TZ))
        self.assertEqual([d["status"] for d in done if d["kind"] == "apicheck"], ["ok"])


class DueTests(SchedBase):
    def test_nothing_but_the_guard_before_the_sync_time(self):
        self.assertEqual(self.kinds(self.due(at(6, 29))), ["guard"])

    def test_sync_is_due_from_0630_and_the_weekly_from_monday_0700(self):
        self.assertEqual(self.due(at(6, 31)), [("sync", "2026-10-12"), ("guard", "2026-10-12T06")])
        self.assertEqual(self.kinds(self.due(at(7, 5))), ["sync", "weekly", "guard"])

    def test_done_jobs_are_not_due_again(self):
        self.e.store.job_mark("pacsi", "sync", "2026-10-12", "ok")
        self.e.store.job_mark("pacsi", "weekly", "2026-W42", "ok")
        self.e.store.job_mark("pacsi", "guard", "2026-10-12T07", "ok")
        self.assertEqual(self.due(at(7, 30)), [])
        self.assertEqual(self.kinds(self.due(at(8, 0))), ["guard"])                        # óránként újra

    def test_weekly_is_caught_up_until_the_end_of_the_week(self):
        self.e.store.job_mark("pacsi", "sync", "2026-10-14", "ok")
        self.assertIn(("weekly", "2026-W42"), self.due(at(10, 0, day=14)))                 # szerdán pótolja
        self.assertIn(("weekly", "2026-W42"), self.due(at(23, 0, day=18)))                 # vasárnap este is
        self.assertNotIn("weekly", self.kinds(self.due(at(6, 50, day=19))))                # a következő hétfő 7 előtt még nem
        self.assertIn(("weekly", "2026-W43"), self.due(at(7, 1, day=19)))

    def test_weekly_needs_a_successful_or_pending_sync(self):
        self.e.store.job_mark("pacsi", "sync", "2026-10-14", "failed")
        self.e.store.put("pacsi.attempts.sync.2026-10-14", 3)                              # a szinkron már nem próbálkozik
        self.assertNotIn("weekly", self.kinds(self.due(at(10, 0, day=14))))

    def test_failed_job_is_retried_after_30_minutes_up_to_three_attempts(self):
        self.e.store.job_mark("pacsi", "sync", "2026-10-12", "failed")                     # a tárolt idő: NOW (07:00)
        self.e.store.put("pacsi.attempts.sync.2026-10-12", 1)
        self.assertNotIn("sync", self.kinds(self.due(at(7, 20))))
        self.assertIn("sync", self.kinds(self.due(at(7, 31))))
        self.e.store.put("pacsi.attempts.sync.2026-10-12", 3)
        self.assertNotIn("sync", self.kinds(self.due(at(12, 0))))

    def test_interrupted_run_is_resumed(self):
        self.e.store.job_mark("pacsi", "sync", "2026-10-12", "running")
        self.assertIn("sync", self.kinds(self.due(at(7, 5))))

    def test_no_guard_before_go_live_and_no_jobs_without_a_customer(self):
        self.e.store.delete("pacsi.approved_daily_micros")
        self.assertNotIn("guard", self.kinds(self.due(at(7, 5))))
        p = dataclasses.replace(self.e.project, customer_id="")
        self.assertEqual(scheduler.due(p, self.e.store, at(7, 5)), [])


class TickTests(SchedBase):
    def test_monday_morning_runs_sync_weekly_and_guard_once(self):
        done = self.tick(at(7, 5))
        self.assertEqual([(d["kind"], d["status"]) for d in done], [("sync", "ok"), ("weekly", "ok"), ("guard", "ok")])
        self.assertEqual([self.job("sync", "2026-10-12"), self.job("weekly", "2026-W42"), self.job("guard", "2026-10-12T07")], ["ok", "ok", "ok"])
        self.assertEqual(len(self.mails()), 1)
        self.assertIn("Heti Google Ads jelentés", self.mails()[0])
        self.assertEqual(self.tick(at(7, 40)), [])                                          # semmi nem fut kétszer
        self.assertEqual([d["kind"] for d in self.tick(at(8, 5))], ["guard"])
        self.assertEqual(len(self.mails()), 1)
        runs = {r["kind"]: r for r in self.e.store.runs("pacsi")}
        self.assertEqual(runs["weekly"]["status"], "ok")
        self.assertTrue(runs["weekly"]["summary"]["mailed"])
        self.assertEqual(runs["sync"]["summary"]["pack"], "changed")                       # az első brief-ellenőrzés alapállapotot rögzít

    def test_weekly_report_is_saved_and_notices_are_cleared(self):
        self.e.store.put("pacsi.notices", [{"ts": "2026-10-08T06:30:00+02:00", "text": "A keretet felemelted."}])
        self.tick(at(7, 5))
        self.assertEqual(self.e.store.get("pacsi.notices"), [])
        self.assertIn("A keretet felemelted.", self.smtp.text_of(self.smtp.messages[0]))
        self.assertTrue((self.e.settings.data_dir / "reports" / "pacsi-2026-10-11.json").exists())
        self.assertEqual(self.e.store.get("pacsi.last_report")["mailed"], True)

    def test_the_narrative_appears_in_the_mail(self):
        self.tick(at(7, 5))
        self.assertIn("Nyugodt hét volt", self.smtp.text_of(self.smtp.messages[0]))

    def test_failing_google_alerts_once_and_retries_later(self):
        self.e.mock.fail_next(403, "authorizationError.USER_PERMISSION_DENIED", "nincs jog", n=1000, path="googleAds:search")
        done = self.tick(at(6, 40))
        self.assertEqual([(d["kind"], d["status"]) for d in done], [("sync", "failed"), ("guard", "ok")])
        self.assertEqual(self.job("sync", "2026-10-12"), "failed")
        self.assertEqual(len(self.mails()), 1)
        self.assertIn("napi szinkron hibára futott", self.mails()[0])
        self.assertIn("nincs jog", self.smtp.text_of(self.smtp.messages[0]))
        self.assertEqual(self.tick(at(6, 55)), [])                                          # 15 perc múlva még nem próbálja újra
        self.e.mock.failures.clear()
        done = self.tick(at(7, 15))                                                         # 35 perc múlva igen; a heti kör is sorra kerül
        self.assertEqual([(d["kind"], d["status"]) for d in done], [("sync", "ok"), ("weekly", "ok"), ("guard", "ok")])
        self.assertEqual(self.e.store.get("pacsi.attempts.sync.2026-10-12"), 2)
        self.assertEqual(len(self.mails()), 2)                                              # az első hibalevél + a heti jelentés

    def test_after_three_failed_attempts_it_stops_trying_and_says_so(self):
        self.e.mock.fail_next(403, "authorizationError.USER_PERMISSION_DENIED", "nincs jog", n=1000, path="googleAds:search")
        syncs = []
        for minutes in (0, 35, 70, 105, 140):
            syncs += [d for d in self.tick(at(7, 0) + dt.timedelta(minutes=minutes)) if d["kind"] == "sync"]
        self.assertEqual([d["status"] for d in syncs], ["failed"] * 3)
        self.assertEqual(self.e.store.get("pacsi.attempts.sync.2026-10-12"), 3)
        self.assertEqual(len(self.mails()), 2)                                              # az 1. hiba + a végső (a 2. ismétlődés-védett)
        self.assertIn("több próbát nem tesz erre az időszakra", self.smtp.text_of(self.smtp.messages[-1]))

    def test_weekly_waits_for_the_daily_sync_to_succeed(self):
        self.e.mock.fail_next(403, "authorizationError.USER_PERMISSION_DENIED", "nincs jog", n=1000, path="googleAds:search")
        done = self.tick(at(7, 5))
        self.assertEqual([(d["kind"], d["status"]) for d in done], [("sync", "failed"), ("guard", "ok")])        # a heti kör ki sem indult
        self.assertIsNone(self.e.store.job("pacsi", "weekly", "2026-W42"))
        self.assertEqual(self.anth.requests, [])

    def test_lease_busy_means_no_work(self):
        self.assertTrue(self.e.store.acquire("engine", "masik-peldany", 900))
        with self.assertRaises(LeaseBusy):
            self.tick(at(7, 5))
        self.assertEqual(self.e.store.runs("pacsi"), [])
        self.assertIsNone(self.e.store.job("pacsi", "sync", "2026-10-12"))

    def test_missing_google_key_skips_everything(self):
        self.assertEqual(self.tick(at(7, 5), deps=scheduler.Deps(client=None)), [])
        self.assertIsNone(self.e.store.job("pacsi", "sync", "2026-10-12"))

    def test_lease_is_released_after_the_tick(self):
        self.tick(at(7, 5))
        self.assertIsNone(self.e.store.lease_holder("engine"))

    def test_mail_failure_of_the_weekly_report_resends_the_saved_one(self):
        def boom(*a, **k):
            raise review.alerts.mailer.MailError("nincs SMTP")
        failing = dataclasses.replace(self.deps, send=boom)
        with unittest.mock.patch.object(review, "run_weekly", wraps=review.run_weekly) as spy:
            done = self.tick(at(7, 5), deps=failing)
            self.assertEqual([(d["kind"], d["status"]) for d in done], [("sync", "ok"), ("weekly", "failed"), ("guard", "ok")])
            self.assertEqual(spy.call_count, 1)
            pending = self.e.store.get("pacsi.pending_report")
            self.assertEqual(pending["week"], "2026-W42")
            self.assertTrue(pathlib.Path(pending["path"]).exists())
            self.assertEqual(self.mails(), [])
            done = self.tick(at(7, 45))                                                      # most már működik az SMTP: ugyanazt a jelentést küldi
            self.assertEqual([(d["kind"], d["status"]) for d in done if d["kind"] == "weekly"], [("weekly", "ok")])
            self.assertEqual(spy.call_count, 1)                                              # a kiértékelés NEM futott újra
        self.assertEqual(len(self.mails()), 1)
        self.assertIsNone(self.e.store.get("pacsi.pending_report"))
        self.assertEqual(self.e.store.runs("pacsi")[0]["summary"]["resent"], True)

    def test_heartbeat_pings_after_the_daily_sync_without_logging_the_url(self):
        self.e.env["HEARTBEAT_URL"] = f"{self.e.site.base}/titkos-ping-123"
        self.e.with_mode("live")
        with unittest.mock.patch.object(scheduler.log, "warn") as warn:
            self.tick(at(6, 40))
        self.assertTrue(any(r[0].startswith("/titkos-ping-123") for r in self.e.site.requests))
        for call in warn.call_args_list:
            self.assertNotIn("titkos-ping-123", json.dumps([str(a) for a in call.args] + [str(v) for v in call.kwargs.values()]))

    def test_project_without_a_campaign_yet_reports_nothing(self):
        e = Env(mode="live")
        try:
            e.store.clock = lambda: at(7, 5).timestamp()
            deps = scheduler.Deps(client=e.client, fetch_kw=e.kw)
            done = scheduler.tick(e.settings, e.store, now=at(7, 5), deps=deps)
            self.assertEqual([(d["kind"], d["status"]) for d in done], [("sync", "ok"), ("weekly", "ok"), ("apicheck", "ok")])
            self.assertEqual([r["summary"] for r in e.store.runs("pacsi") if r["kind"] == "weekly"], [{"skipped": "no_campaigns"}])
            self.assertEqual(self.smtp.messages, [])
        finally:
            e.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        proj = pathlib.Path(self.tmp.name) / "p.toml"
        proj.write_text('[[project]]\nslug="pacsi"\nname="Pacsi"\nsite="https://pacsit.hu"\nbrief_url="https://pacsit.hu/ads/brief.json"\ncustomer_id="2222222222"\n', encoding="utf-8")
        self.settings = config.load({"DATA_DIR": self.tmp.name, "PROJECTS_FILE": str(proj), "ANTHROPIC_API_KEY": "sk-ant-titkos-kulcs-999", "UMAMI_PASSWORD": "titkos-jelszo"})
        self.store = Store(":memory:")
        self.state = web.HealthState()
        self.server = web.start(self.state, self.store, self.settings, port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def get(self, path):
        try:
            r = http.request("GET", f"http://127.0.0.1:{self.port}{path}", retries=0, timeout=5)
            return r.status, r.text()
        except http.HttpError as e:
            return e.status, e.body.decode("utf-8")

    def test_healthy_while_starting_and_after_a_tick(self):
        status, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.state.touch()
        status, body = self.get("/healthz")
        d = json.loads(body)
        self.assertEqual((status, d["ok"], d["mode"]), (200, True, "dry"))
        self.assertTrue(d["label"].startswith("v"))

    def test_stale_loop_is_unhealthy(self):
        self.state.touch()
        self.state.last_tick -= web.LOOP_STALE_S + 5
        self.assertEqual(self.get("/healthz")[0], 503)
        self.state.started -= web.LOOP_STALE_S + 5
        self.state.last_tick = None
        self.assertEqual(self.get("/healthz")[0], 503)

    def test_strict_mode_watches_the_daily_sync_when_live(self):
        self.state.touch()
        self.assertEqual(self.get("/healthz?strict=1")[0], 200)                          # nincs go-live: nincs mit figyelni
        self.store.put("pacsi.approved_daily_micros", 2000 * M)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=50)).isoformat(timespec="seconds")
        self.store.put("pacsi.last_sync_ts", old)
        status, body = self.get("/healthz?strict=1")
        self.assertEqual(status, 503)
        self.assertTrue(json.loads(body)["projects"]["pacsi"]["sync_stale"])
        self.assertEqual(self.get("/healthz")[0], 200)                                   # a Docker-ellenőrzés nem szigorú: nem indítja újra a konténert
        self.store.put("pacsi.last_sync_ts", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.assertEqual(self.get("/healthz?strict=1")[0], 200)

    def test_no_secrets_and_other_paths(self):
        self.state.touch()
        body = self.get("/healthz")[1] + self.get("/")[1]
        for secret in ("sk-ant-titkos-kulcs-999", "titkos-jelszo"):
            self.assertNotIn(secret, body)
        self.assertIn("ads-engine v", self.get("/")[1])
        self.assertEqual(self.get("/admin")[0], 404)


class ServeTests(SchedBase):
    def test_serve_runs_due_jobs_and_stops_cleanly(self):
        stop = threading.Event()
        t = threading.Thread(target=scheduler.serve, args=(self.e.settings,), daemon=True,
                             kwargs=dict(port=0, tick_seconds=0.05, stop_event=stop, deps=self.deps, install_signals=False, now_fn=lambda: at(6, 40)))
        t.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline and self.job("sync", "2026-10-12") != "ok":
                time.sleep(0.1)
            self.assertEqual(self.job("sync", "2026-10-12"), "ok")
            self.assertEqual(self.e.store.get("pacsi.last_sync")["date"], "2026-10-12")
        finally:
            stop.set()
            t.join(15)
        self.assertFalse(t.is_alive())

    def test_serve_survives_an_unexpected_error_in_a_tick(self):
        stop = threading.Event()
        calls = []

        def broken_now():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("váratlan hiba")
            stop.set()
            return at(5, 0)
        t = threading.Thread(target=scheduler.serve, args=(self.e.settings,), daemon=True,
                             kwargs=dict(port=0, tick_seconds=0.05, stop_event=stop, deps=self.deps, install_signals=False, now_fn=broken_now))
        t.start()
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(len(calls), 2)                                          # az első hiba után a ciklus ment tovább


if __name__ == "__main__":
    unittest.main()
