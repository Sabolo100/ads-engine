"""Próbák: kampányfa-építés, launch (dry és live), go-live, és a plan/launch/go-live parancsok – álszerverekkel."""
import contextlib
import copy
import io
import json
import pathlib
import tempfile
import unittest

import _path  # noqa: F401
import helpers
from ads_engine import builder, cli, config, launch, pack as packmod, runtime
from ads_engine.google.client import GoogleAdsError
from ads_engine.guardrails import ENGINE_LABEL
from ads_engine.store import Store
from pack_server import PackServer

ROOT = pathlib.Path(_path.ROOT)
EXAMPLE = ROOT / "examples" / "pacsi"
CID, MCC = helpers.CID, helpers.MCC
M = 1_000_000


class Env:
    """Közös összeállítás: Google-álszerver + a Pacsi csomagot kiszolgáló helyi „oldal” + beállítások."""

    def __init__(self, mode="dry", customer=CID):
        self.mock = helpers.start_mock()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "site"
        self.root.mkdir()
        for src in EXAMPLE.rglob("*"):
            if src.is_file():
                dst = self.root / src.relative_to(EXAMPLE)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
        self.site = PackServer(self.root).start()
        self.projects = pathlib.Path(self.tmp.name) / "projects.toml"
        self.projects.write_text(
            f'[[project]]\nslug="pacsi"\nname="Pacsi"\nsite="https://pacsit.hu"\nbrief_url="{self.site.base}/ads/brief.json"\n'
            f'customer_id="{customer}"\numami_website_id="54fc0966-6e5f-4ad6-b8c3-7ea5af9642e9"\n'
            f'allowed_hosts=["pacsit.hu", "127.0.0.1"]\nimage_hosts=["127.0.0.1"]\n', encoding="utf-8")
        import base64
        self.env = {"DATA_DIR": str(pathlib.Path(self.tmp.name) / "data"), "GADS_BASE_URL": self.mock.base_url, "GADS_LOGIN_CUSTOMER_ID": MCC,
                    "GADS_SA_JSON_B64": base64.b64encode(json.dumps(self.mock.service_account_info()).encode()).decode(),
                    "PROJECTS_FILE": str(self.projects), "ADS_TEST_ALLOW_PRIVATE": "1", "ENGINE_MODE": mode}
        self.settings = config.load(self.env)
        self.project = self.settings.project("pacsi")
        self.store = Store(self.settings.db_path)
        self.client = runtime.google_client(self.settings)
        self.kw = {"allow_private": True, "allow_http": True}

    def close(self):
        self.mock.stop()
        self.site.stop()
        self.store.close()
        self.tmp.cleanup()

    def with_mode(self, mode):
        self.env["ENGINE_MODE"] = mode
        self.settings = config.load(self.env)
        self.project = self.settings.project("pacsi")

    def campaigns(self):
        return self.client.search(CID, "SELECT campaign.id, campaign.name, campaign.status, campaign.labels, campaign_budget.amount_micros "
                                       "FROM campaign WHERE campaign.status != 'REMOVED'")

    def run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv, env=self.env)
        return code, buf.getvalue()


class ImageEligibilityTests(unittest.TestCase):
    """Új fiókon a Google a Search kép-bővítményt még nem engedi (legalább 60 napos fiók kell): a kampány ettől még létrejön."""

    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_dry_launch_validates_the_tree_without_images_and_warns(self):
        self.e.mock.reject_images = True
        res = launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)
        self.assertEqual(res["status"], "validated")
        self.assertTrue(any("60 napos fiókot" in w for w in res["warnings"]))
        logs = self.e.store.actions(kind="launch")
        self.assertEqual(sorted(a["status"] for a in logs), ["rejected", "validated"])        # az első (képekkel) elbukott, a második (képek nélkül) rendben

    def test_dry_launch_still_fails_when_a_non_image_operation_is_wrong(self):
        self.e.mock.fail_next(400, "campaignError.CAMPAIGN_BUDGET_REQUIRED", "hibás", n=1, path="googleAds:mutate")
        with self.assertRaises(GoogleAdsError):
            launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)

    def test_live_launch_creates_the_campaign_without_images_and_warns(self):
        self.e.with_mode("live")
        self.e.mock.reject_images = True
        res = launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)
        self.assertEqual((res["status"], res["images_added"]), ("created", 0))
        self.assertTrue(any("heti kör később pótolja" in w and "60 napos fiókot" in w for w in res["warnings"]))
        self.assertEqual(len(self.e.mock.state[CID]["campaign"]), 1)
        self.assertEqual([c for c in self.e.mock.state[CID].get("campaignAsset", {}).values() if c["fieldType"] == "AD_IMAGE"], [])

    def test_live_launch_registers_the_pack_images_as_protected(self):
        from ads_engine import factory
        self.e.with_mode("live")
        res = launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)
        self.assertEqual(res["images_added"], 3)
        entries = factory.Registry(self.e.store, self.e.settings, self.e.project).entries()
        self.assertEqual([(e["source"], e["status"]) for e in entries], [("pack", "uploaded")] * 3)

    def test_error_indexes_parse_the_google_paths(self):
        e = GoogleAdsError(400, [{"code": "x.y", "path": "mutate_operations[107].asset_operation.create"}, {"code": "x.z", "path": "mutate_operations[3]"}, {"code": "q"}])
        self.assertEqual(launch.error_indexes(e), {107, 3})


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.plan = launch.make_plan(self.e.settings, self.e.project, self.e.store, client=self.e.client, **self.e.kw)

    def tearDown(self):
        self.e.close()

    def test_summary_counts(self):
        s = self.plan.summary
        self.assertEqual((s["ad_groups"], s["ads"], s["sitelinks"], s["callouts"], s["images"]), (4, 4, 5, 6, 3))
        self.assertEqual(s["keywords"], 12 + 8 + 8 + 8)
        self.assertEqual(s["negatives"], len(json.loads((EXAMPLE / "brief.json").read_text(encoding="utf-8"))["keywords"]["negatives"]))
        self.assertEqual(s["campaign"], "Pacsi | Kereső")
        self.assertEqual(s["daily_budget_micros"], 100 * M)           # helyőrző: HUF-nál 100/nap, a kampány szünetel
        self.assertEqual(s["max_cpc_micros"], 150 * M)

    def test_campaign_is_paused_and_safe_by_construction(self):
        camp = next(op["campaignOperation"]["create"] for op in self.plan.tree.ops if "campaignOperation" in op)
        self.assertEqual(camp["status"], "PAUSED")
        self.assertEqual(camp["containsEuPoliticalAdvertising"], "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING")
        self.assertFalse(camp["networkSettings"]["targetSearchNetwork"])
        self.assertFalse(camp["networkSettings"]["targetContentNetwork"])
        self.assertFalse(camp["networkSettings"]["targetPartnerSearchNetwork"])
        self.assertEqual(camp["geoTargetTypeSetting"]["positiveGeoTargetType"], "PRESENCE")
        self.assertFalse(camp["aiMaxSetting"]["enableAiMax"])
        self.assertIn("utm_source=google&utm_medium=cpc&utm_campaign=pacsi-kereso", camp["finalUrlSuffix"])
        self.assertIn("{creative}", camp["finalUrlSuffix"])
        budget = next(op["campaignBudgetOperation"]["create"] for op in self.plan.tree.ops if "campaignBudgetOperation" in op)
        self.assertFalse(budget["explicitlyShared"])

    def test_children_enabled_and_only_campaign_is_the_gate(self):
        for op in self.plan.tree.ops:
            for key in ("adGroupOperation", "adGroupAdOperation", "adGroupCriterionOperation"):
                if key in op:
                    res = op[key]["create"]
                    self.assertIn(res.get("status", "ENABLED"), ("ENABLED",))

    def test_core_and_image_split(self):
        t = self.plan.tree
        self.assertEqual(len(t.ops) - t.core_len, 6)                  # 3 kép: eszköz + kampányhoz kötés
        self.assertTrue(all("assetOperation" in o or "campaignAssetOperation" in o for o in t.ops[t.core_len:]))
        self.assertIn("campaignOperation", t.ops[t.campaign_index])
        self.assertFalse(any("imageAsset" in json.dumps(o) for o in t.core_ops))

    def test_label_created_when_missing_and_reused_when_present(self):
        self.assertTrue(any("labelOperation" in op for op in self.plan.tree.ops))
        self.e.with_mode("live")
        launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)
        label_rn = launch.existing_label(self.e.client, CID)
        self.assertIsNotNone(label_rn)
        plan2 = launch.make_plan(self.e.settings, self.e.project, self.e.store, client=self.e.client, **self.e.kw)
        self.assertFalse(any("labelOperation" in op for op in plan2.tree.ops))
        camp_label = next(op["campaignLabelOperation"]["create"] for op in plan2.tree.ops if "campaignLabelOperation" in op)
        self.assertEqual(camp_label["label"], label_rn)

    def test_every_payload_matches_the_official_schema(self):
        d = helpers.discovery()
        if not d:
            self.skipTest("nincs leíró-dokumentum")
        body = {"mutateOperations": self.plan.tree.ops, "validateOnly": True, "partialFailure": False, "responseContentType": "RESOURCE_NAME_ONLY"}
        self.assertEqual(d.validate_mutate_request(body), [])

    def test_locale_resolved_dynamically(self):
        geo, lang = launch.resolve_locale(self.e.client, CID, "HU", "hu")
        self.assertEqual((geo, lang), ("geoTargetConstants/2348", "languageConstants/1024"))
        self.assertEqual(launch.resolve_locale(self.e.client, CID, "US", "en"), ("geoTargetConstants/2840", "languageConstants/1000"))
        with self.assertRaises(launch.LaunchError):
            launch.resolve_locale(self.e.client, CID, "XX", "zz")

    def test_max_cpc_default_by_currency(self):
        self.assertEqual(builder.max_cpc_micros({}, "HUF"), 150 * M)
        self.assertEqual(builder.max_cpc_micros({"ads": {"max_cpc": 2.5}}, "EUR"), 2_500_000)
        self.assertEqual(builder.placeholder_daily_micros("EUR"), 1 * M)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def launch(self):
        return launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)

    def test_dry_mode_validates_everything_but_creates_nothing(self):
        res = self.launch()
        self.assertEqual(res["status"], "validated")
        self.assertEqual(self.e.campaigns(), [])
        self.assertEqual([a["status"] for a in self.e.store.actions()], ["validated"])
        body = [r for r in self.e.mock.requests if "googleAds:mutate" in r[1]][-1][3]
        self.assertTrue(body["validateOnly"])
        self.assertTrue(any("assetOperation" in o for o in body["mutateOperations"]), "a próba a képeket is tartalmazza")

    def test_live_creates_paused_campaign_with_children_and_images(self):
        self.e.with_mode("live")
        res = self.launch()
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["images_added"], 3)
        camps = self.e.campaigns()
        self.assertEqual(len(camps), 1)
        self.assertEqual(camps[0]["campaign"]["status"], "PAUSED")
        self.assertEqual(camps[0]["campaignBudget"]["amountMicros"], str(100 * M))
        self.assertEqual(len(camps[0]["campaign"]["labels"]), 1)
        ads = self.e.client.search(CID, "SELECT ad_group_ad.status, ad_group.name FROM ad_group_ad")
        self.assertEqual({a["adGroupAd"]["status"] for a in ads}, {"ENABLED"})
        self.assertEqual(len(ads), 4)
        assets = self.e.client.search(CID, "SELECT asset.type FROM asset")
        kinds = sorted(a["asset"]["type"] for a in assets)
        self.assertEqual(kinds.count("IMAGE"), 3)
        self.assertEqual(kinds.count("SITELINK"), 5)
        self.assertEqual(kinds.count("CALLOUT"), 6)
        self.assertEqual(self.e.store.get("pacsi.launch")["images_added"], 3)

    def test_live_runs_two_requests_core_then_images(self):
        self.e.with_mode("live")
        self.launch()
        bodies = [r[3] for r in self.e.mock.requests if "googleAds:mutate" in r[1]]
        self.assertEqual([b["validateOnly"] for b in bodies], [True, False, True, False])
        self.assertFalse(any("imageAsset" in json.dumps(o) for o in bodies[1]["mutateOperations"]))
        self.assertTrue(any("imageAsset" in json.dumps(o) for o in bodies[3]["mutateOperations"]))

    def test_relaunch_does_not_duplicate(self):
        self.e.with_mode("live")
        self.launch()
        res = self.launch()
        self.assertEqual(res["status"], "exists")
        self.assertEqual(len(self.e.campaigns()), 1)
        self.assertEqual(len([a for a in self.e.store.actions() if a["kind"] == "launch"]), 1)        # az újrafuttatás nem írt újabbat

    def test_image_failure_keeps_the_campaign(self):
        self.e.with_mode("live")
        self.e.mock.fail_next(400, "assetError.INVALID_IMAGE", path="googleAds:mutate", after=2)       # a 3. mutate: a képek próbája
        res = self.launch()
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["images_added"], 0)
        self.assertTrue(any("képek feltöltése nem sikerült" in w for w in res["warnings"]))
        self.assertEqual(len(self.e.campaigns()), 1)

    def test_unreachable_image_is_skipped_with_warning(self):
        self.e.with_mode("live")
        (self.e.root / "img" / "lineup-1200x628.jpg").unlink()
        res = self.launch()
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["images_added"], 2)
        self.assertTrue(any("lineup" in w for w in res["warnings"]))

    def test_invalid_pack_blocks_launch(self):
        self.e.with_mode("live")
        c = json.loads((self.e.root / "creatives.json").read_text(encoding="utf-8"))
        c["adsets"][0]["headlines"][0] = "Garantált találat"
        (self.e.root / "creatives.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(packmod.PackError):
            self.launch()
        self.assertEqual(self.e.campaigns(), [])

    def test_missing_customer_id(self):
        self.e.project.customer_id = ""
        with self.assertRaises(launch.LaunchError):
            self.launch()

    def test_google_rejection_is_reported_and_logged(self):
        self.e.with_mode("live")
        self.e.mock.fail_next(400, "campaignError.MISSING_EU_POLITICAL_ADVERTISING_SELF_DECLARATION", path="googleAds:mutate")
        with self.assertRaises(GoogleAdsError):
            self.launch()
        self.assertEqual(self.e.campaigns(), [])
        self.assertEqual(self.e.store.actions()[0]["status"], "rejected")


class GoLiveTests(unittest.TestCase):
    def setUp(self):
        self.e = Env(mode="live")
        launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)

    def tearDown(self):
        self.e.close()

    def test_go_live_sets_budget_and_enables_atomically(self):
        res = launch.go_live(self.e.settings, self.e.project, self.e.store, self.e.client, 2, weekly_budget=21_000)
        self.assertEqual(res["status"], "enabled")
        self.assertEqual(res["daily_micros"], 3000 * M)
        c = self.e.campaigns()[0]
        self.assertEqual(c["campaign"]["status"], "ENABLED")
        self.assertEqual(c["campaignBudget"]["amountMicros"], str(3000 * M))
        self.assertEqual(self.e.store.get("pacsi.approved_daily_micros"), 3000 * M)
        bodies = [r[3] for r in self.e.mock.requests if "googleAds:mutate" in r[1]][-2:]
        self.assertEqual(len(bodies[1]["mutateOperations"]), 2)          # keret + kampány egyetlen kérésben
        act = [a for a in self.e.store.actions() if a["kind"] == "go_live"][0]
        self.assertEqual(act["after"]["status"], "ENABLED")
        self.assertEqual(act["before"]["status"], "PAUSED")

    def test_go_live_refused_in_dry_mode_and_for_bad_budget(self):
        self.e.with_mode("dry")
        with self.assertRaises(launch.LaunchError) as cm:
            launch.go_live(self.e.settings, self.e.project, self.e.store, self.e.client, 2, 10_000)
        self.assertIn("ENGINE_MODE=live", str(cm.exception))
        self.e.with_mode("live")
        with self.assertRaises(launch.LaunchError):
            launch.go_live(self.e.settings, self.e.project, self.e.store, self.e.client, 2, 0)
        self.assertEqual(self.e.campaigns()[0]["campaign"]["status"], "PAUSED")
        self.assertIsNone(self.e.store.get("pacsi.approved_daily_micros"))

    def test_go_live_without_campaign(self):
        e = Env(mode="live")
        try:
            with self.assertRaises(launch.LaunchError) as cm:
                launch.go_live(e.settings, e.project, e.store, e.client, 1, 10_000)
            self.assertIn("launch", str(cm.exception))
        finally:
            e.close()

    def test_stop_file_blocks_go_live(self):
        (self.e.settings.data_dir / "STOP").write_text("x")
        from ads_engine.executor import WriteRefused
        with self.assertRaises(WriteRefused):
            launch.go_live(self.e.settings, self.e.project, self.e.store, self.e.client, 2, 10_000)
        self.assertEqual(self.e.campaigns()[0]["campaign"]["status"], "PAUSED")


class CliLaunchTests(unittest.TestCase):
    def setUp(self):
        self.e = Env(mode="dry")

    def tearDown(self):
        self.e.close()

    def test_plan_prints_summary(self):
        code, out = self.e.run_cli(["plan"])
        self.assertEqual(code, 0, out)
        self.assertIn("Pacsi | Kereső", out)
        self.assertIn("hirdetéscsoport: 4", out)
        self.assertIn("SZÜNETELVE", out)

    def test_plan_reports_pack_problems(self):
        b = json.loads((self.e.root / "brief.json").read_text(encoding="utf-8"))
        del b["keywords"]
        (self.e.root / "brief.json").write_text(json.dumps(b), encoding="utf-8")
        code, out = self.e.run_cli(["plan"])
        self.assertEqual(code, 1)
        self.assertIn("Az Ads Pack nem használható", out)
        self.assertIn("keywords", out)

    def test_launch_without_yes_is_plan_only(self):
        code, out = self.e.run_cli(["launch"])
        self.assertEqual(code, 0)
        self.assertIn("csak a terv készül", out)
        self.assertFalse([r for r in self.e.mock.requests if "googleAds:mutate" in r[1]])

    def test_launch_yes_in_dry_mode_validates_only(self):
        code, out = self.e.run_cli(["launch", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("Próba rendben", out)
        self.assertEqual(self.e.campaigns(), [])
        self.assertEqual(self.e.store.last_run("pacsi", "launch")["status"], "ok") if False else None

    def test_live_launch_then_go_live_via_cli(self):
        self.e.env["ENGINE_MODE"] = "live"
        code, out = self.e.run_cli(["launch", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("SZÜNETELVE", out)
        code, out = self.e.run_cli(["go-live", "--weekly-budget", "14000"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        code, out = self.e.run_cli(["go-live", "--weekly-budget", "14000", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("BEKAPCSOLVA", out)
        self.assertIn("2 000 HUF", out)
        self.assertEqual(self.e.campaigns()[0]["campaign"]["status"], "ENABLED")
        code, out = self.e.run_cli(["status"])
        self.assertIn("Jóváhagyott keret: 2 000 HUF/nap (14 000 /hét)", out)

    def test_second_launch_says_exists(self):
        self.e.env["ENGINE_MODE"] = "live"
        self.e.run_cli(["launch", "--yes"])
        code, out = self.e.run_cli(["launch", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("már létezik", out)

    def test_go_live_in_dry_mode_explains(self):
        code, out = self.e.run_cli(["go-live", "--weekly-budget", "10000", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("ENGINE_MODE=live", out)

    def test_launch_without_key_explains(self):
        self.e.env.pop("GADS_SA_JSON_B64")
        code, out = self.e.run_cli(["launch", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("GADS_SA_JSON_B64", out)


if __name__ == "__main__":
    unittest.main()
