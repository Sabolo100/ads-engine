"""Próbák: Umami-kliens (belépés, újrabelépés, lapozás) és a hirdetés-látogatások összesítése UTM szerint."""
import datetime as dt
import unittest
import unittest.mock
from zoneinfo import ZoneInfo

import _path  # noqa: F401
from ads_engine import umami
from mock_umami import MockUmami


def q(content, term="kutyafajta", medium="cpc", campaign="pacsi-kereso", source="google"):
    return f"?utm_source={source}&utm_medium={medium}&utm_campaign={campaign}&utm_content={content}&utm_term={term}"


class UmamiClientTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockUmami().start()
        self.client = umami.Umami(self.mock.base_url, self.mock.username, self.mock.password)

    def tearDown(self):
        self.mock.stop()

    def test_login_once_and_token_reused(self):
        self.client.website(self.mock.website_id)
        self.client.website(self.mock.website_id)
        self.assertEqual(self.mock.logins, 1)

    def test_bad_password_gives_hungarian_error(self):
        c = umami.Umami(self.mock.base_url, "ads-engine", "rossz")
        with self.assertRaises(umami.UmamiError) as cm:
            c.website(self.mock.website_id)
        self.assertIn("UMAMI_PASSWORD", str(cm.exception))

    def test_expired_token_triggers_relogin(self):
        self.client.website(self.mock.website_id)
        self.mock.expire_tokens()
        self.client.website(self.mock.website_id)
        self.assertEqual(self.mock.logins, 2)

    def test_unknown_website_is_a_clear_error(self):
        with self.assertRaises(umami.UmamiError) as cm:
            self.client.website("nincs-ilyen")
        self.assertIn("webhely-azonosító", str(cm.exception))

    def test_unreachable(self):
        self.mock.stop()
        with self.assertRaises(umami.UmamiError):
            self.client.website(self.mock.website_id)

    def test_events_paging_and_filters(self):
        for i in range(250):
            self.mock.add_event(f"s{i}", "inditas", q("1"), ts=1000 + i)
        self.mock.add_event("sx", "bevont", q("1"), ts=1500)
        rows = self.client.events(self.mock.website_id, 0, 10 ** 12, event="inditas", page_size=100)
        self.assertEqual(len(rows), 250)
        self.assertEqual(len({r["sessionId"] for r in rows}), 250)
        self.assertEqual(len(self.client.events(self.mock.website_id, 1100, 1199, event="inditas")), 100)
        self.assertEqual(len(self.client.events(self.mock.website_id, 0, 10 ** 12, event="bevont")), 1)
        paged = [r for r in self.mock.requests if "/events" in r[1]]
        self.assertGreaterEqual(len(paged), 3)

    def test_max_pages_cap(self):
        for i in range(30):
            self.mock.add_event(f"s{i}", "inditas", q("1"), ts=i)
        self.assertEqual(len(self.client.events(self.mock.website_id, 0, 10 ** 9, event="inditas", page_size=10, max_pages=2)), 20)

    def test_password_never_logged(self):
        import contextlib
        import io
        from ads_engine import log
        buf = io.StringIO()
        with unittest.mock.patch.dict("os.environ", {"ADS_LOG_LEVEL": "info"}), contextlib.redirect_stdout(buf):
            log.info("teszt", pw=self.mock.password)
        self.assertNotIn(self.mock.password, buf.getvalue())
        self.assertIn("***", buf.getvalue())


class AggregateTests(unittest.TestCase):
    def rows(self, spec):
        out = []
        for i, (sid, query) in enumerate(spec):
            out.append({"id": f"e{i}", "sessionId": sid, "urlQuery": query})
        return out

    def test_joins_by_session_and_groups_by_utm(self):
        visits = self.rows([("s1", q("A", "kutya kvíz")), ("s2", q("A", "kutya kvíz")), ("s3", q("B", "kutyafajta teszt")), ("s4", q("B", "x"))])
        engaged = self.rows([("s1", q("A", "kutya kvíz")), ("s3", q("B", "kutyafajta teszt"))])
        keys = self.rows([("s1", q("A", "kutya kvíz"))])
        r = umami.aggregate_ads(visits, engaged, keys)
        self.assertEqual(r["total"], {"visits": 4, "engaged": 2, "key_actions": 1})
        self.assertEqual(r["by"]["content"]["A"], {"visits": 2, "engaged": 1, "key_actions": 1})
        self.assertEqual(r["by"]["content"]["B"], {"visits": 2, "engaged": 1, "key_actions": 0})
        self.assertEqual(r["by"]["term"]["kutya kvíz"]["visits"], 2)
        self.assertEqual(r["by"]["campaign"]["pacsi-kereso"]["visits"], 4)

    def test_ignores_non_ads_and_dedupes_sessions(self):
        visits = self.rows([("s1", q("A")), ("s1", q("A")),                       # ugyanaz a munkamenet kétszer: egy látogatás
                            ("s2", q("A", medium="social")),                      # nem hirdetés
                            ("s3", ""), ("s4", "?utm_medium=cpc&utm_campaign=masik-kampany")])
        r = umami.aggregate_ads(visits, [], campaign_prefix="pacsi")
        self.assertEqual(r["total"]["visits"], 1)

    def test_engaged_without_visit_event_still_counts_as_visit(self):
        r = umami.aggregate_ads([], self.rows([("s9", q("A"))]))
        self.assertEqual(r["total"], {"visits": 1, "engaged": 1, "key_actions": 0})

    def test_empty(self):
        self.assertEqual(umami.aggregate_ads([], [])["total"], {"visits": 0, "engaged": 0, "key_actions": 0})

    def test_parse_utm_handles_encoding_and_missing(self):
        self.assertEqual(umami.parse_utm("?utm_term=kutya%20kv%C3%ADz&x=1")["utm_term"], "kutya kvíz")
        self.assertEqual(umami.parse_utm(None), {})

    def test_week_bounds_previous_full_week_budapest(self):
        tz = ZoneInfo("Europe/Budapest")
        s, e, start, end = umami.week_bounds(dt.date(2026, 10, 5), tz)       # hétfő
        self.assertEqual((start, end), (dt.date(2026, 9, 28), dt.date(2026, 10, 4)))
        self.assertEqual(e - s, 7 * 24 * 3600 * 1000 - 1)
        s2, e2, start2, end2 = umami.week_bounds(dt.date(2026, 10, 8), tz)   # csütörtök: ugyanaz az előző hét
        self.assertEqual((s2, e2), (s, e))


if __name__ == "__main__":
    unittest.main()
