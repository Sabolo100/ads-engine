"""Próbák: riport-lekérdezések (GAQL) – a mezőnevek a hivatalos v25 leíró ellen, a normalizálás az álszerverrel."""
import datetime as dt
import re
import unittest

import _path  # noqa: F401
import helpers
from ads_engine import gaql, launch, reportdata
from mock_google_ads import camel
from test_launch import Env

CID = helpers.CID
D = dt.date


class GaqlFieldTests(unittest.TestCase):
    """Minden lekérdezés minden mezője létezik a v25 GoogleAdsRow-ban (a valódi Google ezt nem tolerálja)."""

    @classmethod
    def setUpClass(cls):
        cls.d = helpers.discovery()

    def setUp(self):
        if not self.d:
            self.skipTest("nincs leíró-dokumentum")

    def exists(self, path):
        schemas = self.d.schemas
        node = schemas[self.d.schema_name("GoogleAdsRow")]
        for part in path.split("."):
            props = node.get("properties", {})
            key = camel(part)
            if key not in props:
                return False
            sub = props[key]
            if sub.get("type") == "array":
                sub = sub.get("items", {})
            node = schemas[sub["$ref"]] if "$ref" in sub else sub
        return True

    def fields_of(self, query):
        select = re.search(r"SELECT\s+(.+?)\s+FROM", query, re.S).group(1)
        fields = [f.strip() for f in select.split(",")]
        tail = query.split(" FROM ", 1)[1]
        fields += re.findall(r"\b([a-z_]+\.[a-z_.]+)\s*(?:=|!=|IN|BETWEEN|>=|<=|<|>)", tail)
        fields += re.findall(r"ORDER BY\s+([a-z_.]+)", tail)
        return sorted(set(fields))

    def test_all_queries_use_real_fields(self):
        ids, since, until = [1, 2], D(2026, 9, 28), D(2026, 10, 4)
        queries = {"owned": gaql.OWNED_CAMPAIGNS, "daily": gaql.campaign_daily(ids, since, until), "keywords": gaql.keywords(ids, since, until),
                   "negatives": gaql.negatives(ids), "campaign_negatives": gaql.campaign_negatives(ids),
                   "terms": gaql.search_terms(ids, since, until), "ads": gaql.ads(ids, since, until), "labels": gaql.asset_labels(ids),
                   "changes": gaql.change_events("2026-09-01 00:00:00", "2026-10-01 00:00:00"), "ad_content": gaql.ad_content(ids),
                   "keyword_status": gaql.keyword_status(ids), "disapproved": gaql.disapproved_details(ids)}
        for name, q in queries.items():
            for f in self.fields_of(q):
                self.assertTrue(self.exists(f), f"{name}: nincs ilyen mező a v25-ben: {f}")

    def test_the_checker_itself_rejects_typos(self):
        self.assertFalse(self.exists("campaign.nincs_ilyen"))
        self.assertFalse(self.exists("metrics.cost_micro"))
        self.assertTrue(self.exists("ad_group_ad.policy_summary.approval_status"))

    def test_quoting_and_helpers(self):
        self.assertEqual(gaql.quote("o'brien \\ x"), "'o\\'brien \\\\ x'")
        self.assertEqual(gaql.ids_in([1, "2"]), "(1, 2)")
        self.assertEqual(gaql.between(D(2026, 9, 28), D(2026, 10, 4)), "segments.date BETWEEN '2026-09-28' AND '2026-10-04'")
        with self.assertRaises(ValueError):
            gaql.ids_in(["1; DROP"])


class ReportDataTests(unittest.TestCase):
    def setUp(self):
        self.e = Env(mode="live")
        launch.launch(self.e.settings, self.e.project, self.e.store, self.e.client, 1, **self.e.kw)
        self.client = self.e.client
        self.camps = reportdata.owned_campaigns(self.client, CID)
        self.ids = [c["id"] for c in self.camps]

    def tearDown(self):
        self.e.close()

    def state(self, typ):
        return self.e.mock.state[CID][typ]

    def test_owned_campaigns_only_with_label(self):
        self.assertEqual(len(self.camps), 1)
        c = self.camps[0]
        self.assertEqual((c["name"], c["status"], c["daily_micros"]), ("Pacsi | Kereső", "PAUSED", 100_000_000))
        # egy kézzel felvett kampány címke nélkül: nem a motoré
        self.e.mock.state[CID]["campaign"]["manual"] = {"resourceName": "customers/2222222222/campaigns/999", "id": "999", "name": "Kézi", "status": "ENABLED",
                                                         "campaignBudget": c["budget_rn"]}
        self.assertEqual([x["name"] for x in reportdata.owned_campaigns(self.client, CID)], ["Pacsi | Kereső"])

    def test_no_label_means_no_owned_campaigns(self):
        for k in list(self.state("label")):
            self.state("label")[k]["status"] = "REMOVED"
        self.assertEqual(reportdata.owned_campaigns(self.client, CID), [])

    def test_daily_cost_and_totals_in_date_range(self):
        cid_ = self.ids[0]
        for day, (cl, im, cost) in {"2026-09-27": (99, 999, 9_000_000), "2026-09-28": (10, 200, 1_500_000), "2026-09-29": (5, 100, 800_000),
                                    "2026-10-05": (77, 777, 7_000_000)}.items():
            self.e.mock.set_metrics(CID, "campaign", cid_, day, clicks=cl, impressions=im, costMicros=cost)
        daily = reportdata.daily_cost(self.client, CID, self.ids, D(2026, 9, 28), D(2026, 10, 4))
        self.assertEqual(sorted(daily), ["2026-09-28", "2026-09-29"])
        t = reportdata.totals(daily)
        self.assertEqual((t["clicks"], t["impressions"], t["cost_micros"]), (15, 300, 2_300_000))
        self.assertAlmostEqual(t["ctr"], 0.05)
        self.assertAlmostEqual(t["avg_cpc_micros"], 2_300_000 / 15)

    def test_no_metrics_gives_zero_totals(self):
        t = reportdata.totals(reportdata.daily_cost(self.client, CID, self.ids, D(2026, 9, 28), D(2026, 10, 4)))
        self.assertEqual((t["clicks"], t["cost_micros"], t["ctr"], t["avg_cpc_micros"]), (0, 0, 0.0, 0.0))

    def test_keyword_rows_with_metrics_and_positive_only(self):
        crit = next(iter(self.state("adGroupCriterion").values()))
        self.e.mock.set_metrics(CID, "adGroupCriterion", crit["resourceName"].rsplit("/", 1)[-1], "2026-09-30", clicks=12, impressions=300, costMicros=900_000)
        rows = reportdata.keyword_rows(self.client, CID, self.ids, D(2026, 9, 28), D(2026, 10, 4))
        self.assertEqual(len(rows), 36)                                             # csak a pozitív kulcsszavak (a negatívok nem)
        hit = next(r for r in rows if r["criterion_id"] == crit["criterionId"])
        self.assertEqual((hit["clicks"], hit["impressions"], hit["cost_micros"]), (12, 300, 900_000))
        self.assertEqual(hit["match"] in ("PHRASE", "EXACT"), True)
        self.assertTrue(all(r["ad_group"] for r in rows))

    def test_negative_rows_campaign_and_adgroup(self):
        negs = reportdata.negative_rows(self.client, CID, self.ids)
        self.assertEqual(len([n for n in negs if n["scope"] == "campaign"]), 33)
        self.assertTrue(any(n["text"] == "eladó" for n in negs))

    def test_search_term_rows(self):
        ag = next(iter(self.state("adGroup")))
        camp = next(iter(self.state("campaign")))
        self.e.mock.state[CID]["searchTerm"] = {"t1": {"resourceName": f"customers/{CID}/searchTermViews/t1", "searchTerm": "kutyafajta teszt ingyen",
                                                       "status": "NONE", "adGroup": ag, "campaign": camp}}
        self.e.mock.set_metrics(CID, "searchTerm", "t1", "2026-09-29", clicks=3, impressions=40, costMicros=300_000)
        rows = reportdata.term_rows(self.client, CID, self.ids, D(2026, 9, 28), D(2026, 10, 4))
        self.assertEqual([(r["term"], r["clicks"], r["cost_micros"]) for r in rows], [("kutyafajta teszt ingyen", 3, 300_000)])
        self.assertEqual(rows[0]["campaign_id"], self.ids[0])

    def test_ad_rows_with_policy(self):
        ad = next(iter(self.state("adGroupAd")))
        self.state("adGroupAd")[ad]["policySummary"] = {"approvalStatus": "DISAPPROVED", "reviewStatus": "REVIEWED"}
        rows = reportdata.ad_rows(self.client, CID, self.ids, D(2026, 9, 28), D(2026, 10, 4))
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["approval"] for r in rows].count("DISAPPROVED"), 1)
        self.assertFalse(any(r["added_by_google"] for r in rows))

    def test_asset_label_rows(self):
        ag = next(iter(self.state("adGroup")))
        ad = next(iter(self.state("adGroupAd")))
        self.e.mock.state[CID]["adAssetView"] = {
            "v1": {"resourceName": f"customers/{CID}/adGroupAdAssetViews/v1", "fieldType": "HEADLINE", "performanceLabel": "LOW", "adGroup": ag,
                   "adGroupAd": ad, "asset": "x"},
            "v2": {"resourceName": f"customers/{CID}/adGroupAdAssetViews/v2", "fieldType": "DESCRIPTION", "performanceLabel": "BEST", "adGroup": ag,
                   "adGroupAd": ad, "asset": "x"}}
        rows = reportdata.asset_label_rows(self.client, CID, self.ids)
        self.assertEqual(sorted((r["field_type"], r["label"]) for r in rows), [("DESCRIPTION", "BEST"), ("HEADLINE", "LOW")])

    def test_change_events_by_time_window(self):
        self.e.mock.state[CID]["changeEvent"] = {
            "a": {"resourceName": "a", "changeDateTime": "2026-09-30 10:00:00", "changeResourceName": "x", "clientType": "GOOGLE_ADS_WEB_CLIENT", "userEmail": "ember@x.hu"},
            "b": {"resourceName": "b", "changeDateTime": "2026-08-01 10:00:00", "changeResourceName": "y", "clientType": "GOOGLE_ADS_API"}}
        rows = reportdata.change_event_rows(self.client, CID, "2026-09-01 00:00:00", "2026-10-01 00:00:00")
        self.assertEqual([r["changeEvent"]["changeResourceName"] for r in rows], ["x"])

    def test_snapshot_shape(self):
        snap = reportdata.snapshot(self.client, CID, self.camps)
        self.assertEqual(snap[self.camps[0]["resource_name"]], {"status": "PAUSED", "name": "Pacsi | Kereső"})
        self.assertEqual(snap[self.camps[0]["budget_rn"]], {"amountMicros": "100000000"})

    def test_empty_ids_return_empty_without_calls(self):
        n = len(self.e.mock.requests)
        self.assertEqual(reportdata.keyword_rows(self.client, CID, [], D(2026, 9, 28), D(2026, 10, 4)), [])
        self.assertEqual(reportdata.term_rows(self.client, CID, [], D(2026, 9, 28), D(2026, 10, 4)), [])
        self.assertEqual(len(self.e.mock.requests), n)


if __name__ == "__main__":
    unittest.main()
