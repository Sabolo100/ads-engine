"""Vékony HTTP-segéd (csak szabványos Python): újrapróbálás visszalépéssel, JSON-kezelés, egységes hiba.

Újrapróbál: hálózati hibánál és 429/500/502/503/504-nél (a Retry-After fejlécet tiszteletben tartja).
A 4xx hibákat (a 429 kivételével) nem próbálja újra, azonnal HttpError-t dob a törzzsel együtt.
"""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = (429, 500, 502, 503, 504)


class HttpError(Exception):
    def __init__(self, status, body=b"", headers=None, url=""):
        self.status, self.body, self.headers, self.url = status, body or b"", headers or {}, url
        snippet = self.body[:300].decode("utf-8", "replace").replace("\n", " ")
        super().__init__(f"HTTP {status} {url} {snippet}".strip())

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body.decode("utf-8") or "null")


def request(method, url, *, headers=None, json_body=None, form=None, data=None, timeout=30, retries=3,
            backoff=1.5, sleep=time.sleep):
    """Egy HTTP-kérés. json_body → JSON, form → application/x-www-form-urlencoded, data → nyers bájtok."""
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            headers_in = {k.lower(): v for k, v in e.headers.items()}
            last = HttpError(e.code, body, headers_in, url)
            if e.code not in RETRY_STATUS or attempt >= retries:
                raise last from None
            wait = _retry_after(headers_in) or backoff * (2 ** attempt) + random.random()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = HttpError(0, str(e).encode("utf-8"), {}, url)
            if attempt >= retries:
                raise last from None
            wait = backoff * (2 ** attempt) + random.random()
        sleep(min(wait, 60))
    raise last  # pragma: no cover


def _retry_after(headers):
    try:
        return float(headers.get("retry-after", ""))
    except ValueError:
        return None
