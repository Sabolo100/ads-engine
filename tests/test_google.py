"""Google-oldali próbák: belépés (JWT), REST-kliens, hibaértelmezés, mutate (atomi, validateOnly), GAQL, leíró-ellenőrzés."""
import base64
import unittest

import _path  # noqa: F401
import helpers
from ads_engine.google.auth import AuthError, ServiceAccount, TokenProvider
from ads_engine.google.client import GoogleAdsClient, GoogleAdsError, customer_id_clean, from_micros, micros
from ads_engine.google.discovery import Discovery, api_check, days_until
from ads_engine import http
from mock_google_ads import make_service_account

CID, MCC = helpers.CID, helpers.MCC


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.mock = helpers.start_mock()

    def tearDown(self):
        self.mock.stop()

    def test_jwt_signature_is_verified_and_token_cached(self):
        sa = ServiceAccount(self.mock.service_account_info())
        tp = TokenProvider(sa)
        t1 = tp.token()
        t2 = tp.token()
        self.assertEqual(t1, t2)
        self.assertEqual(sum(1 for r in self.mock.requests if r[1] == "/token"), 1)

    def test_token_refreshes_near_expiry(self):
        import time
        sa = ServiceAccount(self.mock.service_account_info())
        now = [time.time()]
        tp = TokenProvider(sa, clock=lambda: now[0])
        t1 = tp.token()
        now[0] += 3600 - 60            # a lejárat előtt 60 mp: újra kell kérni
        t2 = tp.token()
        self.assertNotEqual(t1, t2)

    def test_wrong_key_gives_hungarian_hint(self):
        info, _ = make_service_account(self.mock.token_uri, self.mock.email)     # más kulcs, mint amit az álszerver ismer
        tp = TokenProvider(ServiceAccount(info))
        with self.assertRaises(AuthError) as cm:
            tp.token()
        self.assertIn("invalid_grant", str(cm.exception))
        self.assertIn("új", str(cm.exception))

    def test_wrong_email_rejected(self):
        info = self.mock.service_account_info()
        info["client_email"] = "masik@test-project.iam.gserviceaccount.com"
        with self.assertRaises(AuthError):
            TokenProvider(ServiceAccount(info)).token()

    def test_broken_private_key_message(self):
        info = self.mock.service_account_info()
        info["private_key"] = "nem pem"
        with self.assertRaises(AuthError) as cm:
            ServiceAccount(info)
        self.assertIn("PEM", str(cm.exception))

    def test_missing_fields(self):
        with self.assertRaises(AuthError):
            ServiceAccount({"client_email": "x@y"})

    def test_private_key_with_literal_backslash_n_is_repaired(self):
        info = self.mock.service_account_info()
        info["private_key"] = info["private_key"].replace("\n", "\\n")
        TokenProvider(ServiceAccount(info)).token()

    def test_from_env_variants(self):
        import json
        info = self.mock.service_account_info()
        raw = json.dumps(info)
        self.assertIsNone(ServiceAccount.from_env({}))
        sa = ServiceAccount.from_env({"GADS_SA_JSON_B64": base64.b64encode(raw.encode()).decode()})
        self.assertEqual(sa.client_email, self.mock.email)
        self.assertEqual(ServiceAccount.from_env({"GADS_SA_JSON": raw}).client_email, self.mock.email)
        with self.assertRaises(AuthError):
            ServiceAccount.from_env({"GADS_SA_JSON_B64": "###"})
        with self.assertRaises(AuthError):
            ServiceAccount.from_env({"GADS_SA_JSON": "{nem json"})

    def test_private_key_never_logged(self):
        import io
        import contextlib
        from ads_engine import log
        info = self.mock.service_account_info()
        ServiceAccount(info)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            log.info("teszt", key=info["private_key"], lines=info["private_key"].splitlines()[1])
        self.assertNotIn("BEGIN PRIVATE KEY", buf.getvalue())
        self.assertIn("***", buf.getvalue())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.mock = helpers.start_mock()
        self.client = helpers.make_client(self.mock)

    def tearDown(self):
        self.mock.stop()

    def test_list_accessible_customers(self):
        self.assertEqual(self.client.list_accessible_customers(), [MCC])

    def test_headers_login_customer_id_and_no_developer_token(self):
        self.client.search(CID, "SELECT customer.id FROM customer")
        _, _, headers, _ = [r for r in self.mock.requests if "googleAds:search" in r[1]][-1]
        self.assertEqual(headers["login-customer-id"], MCC)
        self.assertTrue(headers["authorization"].startswith("Bearer "))
        self.assertNotIn("developer-token", headers)

    def test_child_without_login_customer_id_is_denied_with_hint(self):
        c = helpers.make_client(self.mock, login=None)
        with self.assertRaises(GoogleAdsError) as cm:
            c.search(CID, "SELECT customer.id FROM customer")
        e = cm.exception
        self.assertEqual(e.status, 403)
        self.assertTrue(e.has("USER_PERMISSION_DENIED"))
        self.assertIn("hozzá", str(e))          # magyar útmutatás a hozzáadásról

    def test_customer_query(self):
        rows = self.client.search(CID, "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, "
                                       "customer.manager, customer.test_account, customer.status, customer.auto_tagging_enabled FROM customer")
        c = rows[0]["customer"]
        self.assertEqual((c["descriptiveName"], c["currencyCode"], c["timeZone"], c["manager"]), ("Pacsit", "HUF", "Europe/Budapest", False))

    def test_transient_error_is_retried_then_succeeds(self):
        self.mock.fail_next(503, "internalError.TRANSIENT_ERROR", n=2, path="googleAds:search")
        slept = []
        c = helpers.make_client(self.mock, sleep=slept.append)
        rows = c.search(CID, "SELECT customer.id FROM customer")
        self.assertEqual(rows[0]["customer"]["id"], CID)

    def test_google_transient_code_on_400_is_retried(self):
        self.mock.fail_next(400, "databaseError.CONCURRENT_MODIFICATION", n=1, path="googleAds:search")
        self.client.search(CID, "SELECT customer.id FROM customer")

    def test_rate_limit_gives_up_after_retries(self):
        self.mock.fail_next(429, "quotaError.RESOURCE_TEMPORARILY_EXHAUSTED", n=50, path="googleAds:search")
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.search(CID, "SELECT customer.id FROM customer")
        self.assertTrue(cm.exception.transient)

    def test_non_transient_error_not_retried(self):
        self.mock.fail_next(400, "requestError.INVALID_CUSTOMER_ID", n=1, path="googleAds:search")
        self.client.list_accessible_customers()        # a belépési token már megvan, csak a keresést számoljuk
        before = len(self.mock.requests)
        with self.assertRaises(GoogleAdsError):
            self.client.search(CID, "SELECT customer.id FROM customer")
        self.assertEqual(len(self.mock.requests) - before, 1)

    def test_observer_sees_requests_and_errors(self):
        seen = []
        self.client.observer = lambda m, p, b, r, e: seen.append((p, bool(e)))
        self.client.search(CID, "SELECT customer.id FROM customer")
        with self.assertRaises(GoogleAdsError):
            self.client.search("9999999999", "SELECT customer.id FROM customer")
        self.assertEqual([s[1] for s in seen], [False, True])

    def test_observer_failure_does_not_break_call(self):
        def boom(*a):
            raise RuntimeError("naplózási hiba")
        self.client.observer = boom
        self.assertTrue(self.client.search(CID, "SELECT customer.id FROM customer"))

    def test_bad_query_error(self):
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.search(CID, "NEM LEKÉRDEZÉS")
        self.assertTrue(cm.exception.has("BAD_QUERY"))

    def test_helpers(self):
        self.assertEqual(customer_id_clean("222-222-2222"), "2222222222")
        self.assertEqual(micros(1500), 1_500_000_000)
        self.assertEqual(from_micros("2500000"), 2.5)


class MutateTests(unittest.TestCase):
    def setUp(self):
        self.mock = helpers.start_mock()
        self.client = helpers.make_client(self.mock)

    def tearDown(self):
        self.mock.stop()

    def campaigns(self):
        return self.client.search(CID, "SELECT campaign.id, campaign.name, campaign.status, campaign.labels, campaign_budget.amount_micros "
                                       "FROM campaign WHERE campaign.status != 'REMOVED'")

    def test_validate_only_changes_nothing(self):
        r = self.client.mutate(CID, helpers.sample_tree(), validate_only=True)
        self.assertEqual(r, {})
        self.assertEqual(self.campaigns(), [])

    def test_full_tree_creates_everything_atomically(self):
        r = self.client.mutate(CID, helpers.sample_tree())
        self.assertEqual(len(r["mutateOperationResponses"]), len(helpers.sample_tree()))
        rows = self.campaigns()
        self.assertEqual(len(rows), 1)
        c = rows[0]
        self.assertEqual(c["campaign"]["name"], "Pacsi | Kereső")
        self.assertEqual(c["campaign"]["status"], "PAUSED")
        self.assertEqual(c["campaignBudget"]["amountMicros"], "1000000000")
        self.assertEqual(len(c["campaign"]["labels"]), 1)

    def test_temp_ids_resolve_to_real_names(self):
        self.client.mutate(CID, helpers.sample_tree())
        ads = self.client.search(CID, "SELECT ad_group_ad.ad.id, ad_group_ad.status, ad_group.name, campaign.name FROM ad_group_ad")
        self.assertEqual(ads[0]["adGroup"]["name"], "Kvíz")
        self.assertEqual(ads[0]["campaign"]["name"], "Pacsi | Kereső")

    def test_missing_eu_declaration_rejected_and_nothing_created(self):
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(CID, helpers.sample_tree(eu=False))
        self.assertTrue(cm.exception.has("MISSING_EU_POLITICAL_ADVERTISING_SELF_DECLARATION"))
        self.assertIn("EU", str(cm.exception))
        self.assertEqual(self.campaigns(), [])        # atomi: a keret sem jött létre

    def test_duplicate_campaign_name(self):
        self.client.mutate(CID, helpers.sample_tree())
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(CID, helpers.sample_tree(with_label=False))
        self.assertTrue(cm.exception.has("DUPLICATE_CAMPAIGN_NAME"))

    def test_headline_too_long_rejected(self):
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(CID, helpers.sample_tree(headlines=["x" * 31, "b", "c"]))
        self.assertTrue(cm.exception.has("TOO_LONG"))

    def test_schema_violation_unknown_field(self):
        if not helpers.discovery():
            self.skipTest("nincs leíró-dokumentum")
        ops = helpers.sample_tree()
        ops[0]["campaignBudgetOperation"]["create"]["nincsIlyenMezo"] = 1
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(CID, ops, validate_only=True)
        self.assertIn("ismeretlen mező", str(cm.exception.errors))

    def test_schema_violation_readonly_field(self):
        if not helpers.discovery():
            self.skipTest("nincs leíró-dokumentum")
        ops = helpers.sample_tree()
        ops[2]["campaignOperation"]["create"]["primaryStatus"] = "ELIGIBLE"
        with self.assertRaises(GoogleAdsError):
            self.client.mutate(CID, ops, validate_only=True)

    def test_second_rsa_enabled_limit(self):
        self.client.mutate(CID, helpers.sample_tree())
        ag = self.client.search(CID, "SELECT ad_group.id FROM ad_group")[0]["adGroup"]["id"]
        ad_group = f"customers/{CID}/adGroups/{ag}"

        def rsa(n):
            return [{"adGroupAdOperation": {"create": {"adGroup": ad_group, "status": "ENABLED", "ad": {
                "finalUrls": ["https://pacsit.hu/"], "responsiveSearchAd": {
                    "headlines": [{"text": f"Cím {n} a"}, {"text": f"Cím {n} b"}, {"text": f"Cím {n} c"}],
                    "descriptions": [{"text": "Leírás egy"}, {"text": "Leírás kettő"}]}}}}}]
        self.client.mutate(CID, rsa(2))
        self.client.mutate(CID, rsa(3))
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(CID, rsa(4))
        self.assertTrue(cm.exception.has("TOO_MANY_ENABLED_ADS"))

    def test_update_with_mask_and_remove(self):
        self.client.mutate(CID, helpers.sample_tree())
        camp = self.client.search(CID, "SELECT campaign.resource_name FROM campaign")[0]["campaign"]["resourceName"]
        self.client.mutate(CID, [{"campaignOperation": {"update": {"resourceName": camp, "status": "ENABLED"}, "updateMask": "status"}}])
        self.assertEqual(self.campaigns()[0]["campaign"]["status"], "ENABLED")
        self.client.mutate(CID, [{"campaignOperation": {"remove": camp}}])
        self.assertEqual(self.campaigns(), [])

    def test_unresolved_temp_id_rejected(self):
        ops = [{"campaignOperation": {"create": {"resourceName": helpers.tmp(CID, "campaigns", 9), "name": "X", "status": "PAUSED",
                                                 "advertisingChannelType": "SEARCH",
                                                 "campaignBudget": helpers.tmp(CID, "campaignBudgets", 8),
                                                 "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}}}]
        with self.assertRaises(GoogleAdsError):
            self.client.mutate(CID, ops)

    def test_shared_budget_default_trap_is_modelled(self):
        """A keret explicitlyShared nélkül megosztott lesz (az API alapértelmezése) – a Demand Gen ezt elutasítja."""
        cid = CID
        ops = [{"campaignBudgetOperation": {"create": {"resourceName": helpers.tmp(cid, "campaignBudgets", 1), "name": "k", "amountMicros": "5000000"}}},
               {"campaignOperation": {"create": {"resourceName": helpers.tmp(cid, "campaigns", 2), "name": "DG", "status": "PAUSED",
                                                 "advertisingChannelType": "DEMAND_GEN",
                                                 "campaignBudget": helpers.tmp(cid, "campaignBudgets", 1),
                                                 "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}}}]
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(cid, ops)
        self.assertTrue(cm.exception.has("CAMPAIGN_BUDGET_NOT_SHAREABLE"))

    def test_image_asset_via_data_is_accepted(self):
        self.client.mutate(CID, helpers.sample_tree())
        assets = self.client.search(CID, "SELECT asset.id, asset.type FROM asset")
        self.assertIn("IMAGE", [a["asset"]["type"] for a in assets])

    def test_bad_image_rejected(self):
        ops = [{"assetOperation": {"create": {"resourceName": helpers.tmp(CID, "assets", 1), "imageAsset": {"data": base64.b64encode(b"x").decode()}}}}]
        with self.assertRaises(GoogleAdsError) as cm:
            self.client.mutate(CID, ops)
        self.assertTrue(cm.exception.has("INVALID_IMAGE"))


class GaqlTests(unittest.TestCase):
    def setUp(self):
        self.mock = helpers.start_mock()
        self.client = helpers.make_client(self.mock)
        self.client.mutate(CID, helpers.sample_tree())
        self.camp_id = self.client.search(CID, "SELECT campaign.id FROM campaign")[0]["campaign"]["id"]

    def tearDown(self):
        self.mock.stop()

    def test_where_in_and_not_equal(self):
        q = f"SELECT campaign.id, campaign.name FROM campaign WHERE campaign.id IN ({self.camp_id}, 5) AND campaign.status != 'REMOVED'"
        self.assertEqual(len(self.client.search(CID, q)), 1)
        self.assertEqual(self.client.search(CID, "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'"), [])

    def test_metrics_aggregate_and_by_date(self):
        for d, clicks, imp, cost in (("2026-09-28", 10, 200, 1_500_000), ("2026-09-29", 5, 100, 800_000)):
            self.mock.set_metrics(CID, "campaign", self.camp_id, d, clicks=clicks, impressions=imp, costMicros=cost)
        agg = self.client.search(CID, "SELECT campaign.id, metrics.clicks, metrics.impressions, metrics.cost_micros, metrics.ctr FROM campaign")
        m = agg[0]["metrics"]
        self.assertEqual((m["clicks"], m["impressions"], m["costMicros"]), ("15", "300", "2300000"))
        self.assertAlmostEqual(m["ctr"], 0.05)
        daily = self.client.search(CID, "SELECT campaign.id, segments.date, metrics.clicks FROM campaign "
                                        "WHERE segments.date BETWEEN '2026-09-29' AND '2026-09-30'")
        self.assertEqual([(r["segments"]["date"], r["metrics"]["clicks"]) for r in daily], [("2026-09-29", "5")])

    def test_label_listing(self):
        rows = self.client.search(CID, "SELECT label.id, label.name FROM label WHERE label.name = 'ads-engine'")
        self.assertEqual(rows[0]["label"]["name"], "ads-engine")

    def test_missing_table(self):
        with self.assertRaises(GoogleAdsError):
            self.client.search(CID, "SELECT x.y FROM nincs_ilyen")


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = helpers.discovery()

    def setUp(self):
        if not self.d:
            self.skipTest("nincs leíró-dokumentum")

    def test_valid_campaign_payload(self):
        self.assertEqual(self.d.validate("Campaign", {"name": "x", "status": "PAUSED", "advertisingChannelType": "SEARCH",
                                                      "targetSpend": {"cpcBidCeilingMicros": "300000000"},
                                                      "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                                                      "aiMaxSetting": {"enableAiMax": False}}), [])

    def test_unknown_enum_type_readonly(self):
        errs = self.d.validate("Campaign", {"status": "PAUZA", "nincs": 1, "id": "5", "name": 7})
        text = " | ".join(errs)
        self.assertIn("érvénytelen érték", text)
        self.assertIn("ismeretlen mező", text)
        self.assertIn("readOnly", text)
        self.assertIn("szöveget vártunk", text)

    def test_int64_accepts_string_or_int_rejects_garbage(self):
        self.assertEqual(self.d.validate("TargetSpend", {"cpcBidCeilingMicros": 5}), [])
        self.assertEqual(self.d.validate("TargetSpend", {"cpcBidCeilingMicros": "5"}), [])
        self.assertTrue(self.d.validate("TargetSpend", {"cpcBidCeilingMicros": "öt"}))

    def test_byte_field_must_be_base64(self):
        self.assertEqual(self.d.validate("Asset", {"imageAsset": {"data": "aGVsbG8="}}), [])
        self.assertTrue(self.d.validate("Asset", {"imageAsset": {"data": "nem base64!"}}))

    def test_mutate_request_needs_exactly_one_operation_key(self):
        errs = self.d.validate_mutate_request({"mutateOperations": [{"campaignOperation": {"create": {}}, "adGroupOperation": {"create": {}}}]})
        self.assertTrue(any("pontosan egy" in e for e in errs))

    def test_sample_tree_is_valid(self):
        body = {"mutateOperations": helpers.sample_tree(), "validateOnly": True, "partialFailure": False, "responseContentType": "RESOURCE_NAME_ONLY"}
        self.assertEqual(self.d.validate_mutate_request(body), [])


class ApiCheckTests(unittest.TestCase):
    def test_days_until(self):
        import datetime as dt
        self.assertEqual(days_until("2027-08-31", dt.date(2027, 8, 1)), 30)

    def test_api_check_offline_is_graceful(self):
        def offline(*a, **k):
            raise http.HttpError(0, "nincs hálózat".encode("utf-8"), {}, "")
        out = api_check("v25", refresh=True, request=offline)
        self.assertFalse(out["ok"])
        self.assertTrue(any("nem tölthető" in n for n in out["notes"]))

    def test_api_check_warns_near_sunset(self):
        import datetime as dt
        d = helpers.discovery()
        if not d:
            self.skipTest("nincs leíró-dokumentum")

        class R:
            def __init__(s, doc):
                s.doc = doc

            def json(s):
                return s.doc

        def fake(method, url, **kw):
            if "version=v25" in url:
                return R(d.doc)
            raise http.HttpError(404, b"", {}, url)
        out = api_check("v25", refresh=True, request=fake, today=dt.date(2027, 7, 15))
        self.assertTrue(any("lejár" in n for n in out["notes"]))
        self.assertNotIn("newer", out)

    def test_api_check_detects_newer_version(self):
        d = helpers.discovery()
        if not d:
            self.skipTest("nincs leíró-dokumentum")

        class R:
            def __init__(s, doc):
                s.doc = doc

            def json(s):
                return s.doc

        def fake(method, url, **kw):
            if "version=v25" in url or "version=v26" in url:
                return R(d.doc)
            raise http.HttpError(404, b"", {}, url)
        out = api_check("v25", refresh=False, request=fake)
        self.assertEqual(out["newer"], ["v26"])


if __name__ == "__main__":
    unittest.main()
