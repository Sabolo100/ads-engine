"""Umami (süti nélküli webes statisztika) – csak-olvasó hozzáférés.

Belépés: POST /api/auth/login {username, password} → {token}; utána `Authorization: Bearer <token>`. Az önhosztolt Umami
(v3.0.x) megosztási linkje és API-kulcsa (csak v3.4.0-tól) nem használható, ezért egy csak-olvasó („View only”) felhasználó kell.

A hirdetésekből jövő látogatást az ESEMÉNYEK listájából számoljuk (GET /api/websites/{id}/events?event=…): az `inditas` minden
látogatáskor egyszer tüzel (ez a „látogatás”), a `bevont` oldalbetöltésenként egyszer az első érdemi interakciónál (ez a „bevont
látogatás”). Mindkettő sorában benne van az oldal lekérdezése (`urlQuery`), így az UTM (kampány, hirdetés, kulcsszó) szerint
csoportosítható – ehhez nem kell az Umami UTM-támogatása. A DNT/GPC látogatókat az Umami nem látja: arányt hasonlítunk, nem abszolút számot.
"""
import datetime as dt
import urllib.parse

from . import http, log


class UmamiError(Exception):
    pass


class Umami:
    def __init__(self, base_url, username, password, *, request=http.request, sleep=None):
        self.base_url = base_url.rstrip("/")
        self.username, self.password = username, password
        self._request = request
        self._token = None
        log.add_secret(password)

    # ------------------------------------------------------------------ alap
    def _login(self):
        try:
            r = self._request("POST", f"{self.base_url}/api/auth/login", json_body={"username": self.username, "password": self.password}, retries=1)
        except http.HttpError as e:
            if e.status in (400, 401, 403):
                raise UmamiError("Az Umami bejelentkezés nem sikerült: hibás felhasználónév vagy jelszó (UMAMI_USER, UMAMI_PASSWORD).") from None
            raise UmamiError(f"Az Umami nem érhető el: {e}") from None
        token = (r.json() or {}).get("token")
        if not token:
            raise UmamiError("Az Umami bejelentkezés válaszában nincs token.")
        log.add_secret(token)
        self._token = token

    def get(self, path, params=None, _retry=True):
        if not self._token:
            self._login()
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            return self._request("GET", url, headers={"Authorization": f"Bearer {self._token}"}, retries=1).json()
        except http.HttpError as e:
            if e.status == 401 and _retry:             # lejárt token: újra belépünk
                self._token = None
                return self.get(path, params, _retry=False)
            if e.status == 404:
                raise UmamiError(f"Az Umami nem találja: {path} (webhely-azonosító vagy jogosultság hibás?)") from None
            raise UmamiError(f"Umami hiba ({e.status}): {path}") from None

    # ------------------------------------------------------------------ lekérdezések
    def website(self, website_id):
        return self.get(f"/api/websites/{website_id}")

    def stats(self, website_id, start_ms, end_ms):
        return self.get(f"/api/websites/{website_id}/stats", {"startAt": start_ms, "endAt": end_ms})

    def events(self, website_id, start_ms, end_ms, event=None, page_size=100, max_pages=100):
        """Az események sorai (a `urlQuery`-vel), az összes oldal. Legfeljebb max_pages × page_size sor."""
        rows, page = [], 1
        while page <= max_pages:
            data = self.get(f"/api/websites/{website_id}/events",
                            {"startAt": start_ms, "endAt": end_ms, "event": event, "page": page, "pageSize": page_size})
            batch = data.get("data", []) if isinstance(data, dict) else []
            rows.extend(batch)
            total = data.get("count", len(rows)) if isinstance(data, dict) else len(rows)
            if not batch or len(rows) >= total:
                break
            page += 1
        return rows


def ms(date_time):
    return int(date_time.timestamp() * 1000)


def parse_utm(url_query):
    q = urllib.parse.parse_qs((url_query or "").lstrip("?"), keep_blank_values=True)
    return {k: v[0] for k, v in q.items() if k.startswith("utm_")}


def aggregate_ads(visit_rows, engaged_rows, key_rows=None, *, medium="cpc", campaign_prefix=""):
    """A hirdetésekből (utm_medium=cpc) jövő látogatások és bevont látogatások, UTM szerint.

    visit_rows: `inditas` események, engaged_rows: `bevont` események, key_rows: kulcscselekvések (pl. kviz-kesz), mind az Umami sorai.
    Munkamenet-azonosítóval párosítunk, így egy látogató egyszer számít. Visszaad: összesítést és bontást campaign/content/term szerint."""
    def ad_rows(rows):
        out = {}
        for r in rows or []:
            utm = parse_utm(r.get("urlQuery"))
            if utm.get("utm_medium") != medium or not utm.get("utm_campaign", "").startswith(campaign_prefix):
                continue
            out.setdefault(r.get("sessionId") or r.get("id"), utm)
        return out

    visits, engaged, keys = ad_rows(visit_rows), ad_rows(engaged_rows), ad_rows(key_rows)
    for sid, utm in {**engaged, **keys}.items():               # bevont/kulcsesemény látogatás nélkül (pl. az `inditas` kimaradt): is látogatás
        visits.setdefault(sid, utm)
    total = {"visits": len(visits), "engaged": len([s for s in visits if s in engaged]), "key_actions": len([s for s in visits if s in keys])}
    by = {"campaign": {}, "content": {}, "term": {}}
    for sid, utm in visits.items():
        for dim, key in (("campaign", "utm_campaign"), ("content", "utm_content"), ("term", "utm_term")):
            v = utm.get(key)
            if not v:
                continue
            slot = by[dim].setdefault(v, {"visits": 0, "engaged": 0, "key_actions": 0})
            slot["visits"] += 1
            slot["engaged"] += int(sid in engaged)
            slot["key_actions"] += int(sid in keys)
    return {"total": total, "by": by}


def week_bounds(today, tz):
    """Az előző teljes hét (hétfő 00:00 – vasárnap 23:59:59.999, helyi idő) epoch-ezredmásodpercben + a dátumok."""
    monday_this = today - dt.timedelta(days=today.weekday())
    start = monday_this - dt.timedelta(days=7)
    end_exclusive = monday_this
    s = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
    e = dt.datetime.combine(end_exclusive, dt.time.min, tzinfo=tz) - dt.timedelta(milliseconds=1)
    return ms(s), ms(e), start, end_exclusive - dt.timedelta(days=1)
