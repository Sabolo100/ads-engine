"""Álszerver a Google OAuth-hoz és a Google Ads REST (v25) API-hoz – a valódi Google-höz nem ér.

Mit tud (a próbákhoz elegendő, a valós szabályok egy részét utánozza):
  · POST /token            szolgáltatásfiókos JWT-belépés: az RS256 aláírást, az aud/scope/exp mezőt ellenőrzi
  · GET  /v25/customers:listAccessibleCustomers
  · POST /v25/customers/{id}/googleAds:search   kis GAQL-értelmező (SELECT … FROM … WHERE … LIMIT), metrikákkal
  · POST /v25/customers/{id}/googleAds:mutate   a kérést a HIVATALOS v25 leíró-dokumentum ellen ellenőrzi (ismeretlen mező,
                                                readOnly, enum, típus), ideiglenes (negatív) azonosítók, atomi végrehajtás,
                                                validateOnly, tipikus üzleti szabályok (EU-nyilatkozat, RSA-hosszok, név-ütközés…)
  · GET  /v25/customers/{id}/getIdentityVerification
  · hibabeinjektálás: fail_next(status, code) – átmeneti hibák, kvóta
Az állapot memóriában él; a tesztek közvetlenül is módosíthatják (accounts, state, metrics).
"""
import base64
import datetime as dt
import json
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

OPS = {  # művelet-kulcs → (erőforrás-típus, REST-gyűjtemény)
    "campaignBudgetOperation": ("campaignBudget", "campaignBudgets"),
    "campaignOperation": ("campaign", "campaigns"),
    "adGroupOperation": ("adGroup", "adGroups"),
    "adGroupAdOperation": ("adGroupAd", "adGroupAds"),
    "adGroupCriterionOperation": ("adGroupCriterion", "adGroupCriteria"),
    "campaignCriterionOperation": ("campaignCriterion", "campaignCriteria"),
    "assetOperation": ("asset", "assets"),
    "campaignAssetOperation": ("campaignAsset", "campaignAssets"),
    "labelOperation": ("label", "labels"),
    "campaignLabelOperation": ("campaignLabel", "campaignLabels"),
    "adGroupLabelOperation": ("adGroupLabel", "adGroupLabels"),
    "adGroupAdLabelOperation": ("adGroupAdLabel", "adGroupAdLabels"),
    "adGroupCriterionLabelOperation": ("adGroupCriterionLabel", "adGroupCriterionLabels"),
    "adGroupBidModifierOperation": ("adGroupBidModifier", "adGroupBidModifiers"),
    "campaignBidModifierOperation": ("campaignBidModifier", "campaignBidModifiers"),
}
GAQL_TABLES = {  # GAQL FROM → állapot-típus
    "campaign": "campaign", "campaign_budget": "campaignBudget", "ad_group": "adGroup", "ad_group_ad": "adGroupAd",
    "ad_group_criterion": "adGroupCriterion", "campaign_criterion": "campaignCriterion", "label": "label",
    "campaign_label": "campaignLabel", "ad_group_label": "adGroupLabel", "ad_group_ad_label": "adGroupAdLabel",
    "ad_group_criterion_label": "adGroupCriterionLabel", "asset": "asset", "campaign_asset": "campaignAsset",
    "ad_group_asset": "adGroupAsset", "keyword_view": "adGroupCriterion", "customer": "customer",
    "change_event": "changeEvent", "search_term_view": "searchTerm", "ad_group_ad_asset_view": "adAssetView",
    "recommendation_subscription": "recommendationSubscription", "language_constant": "languageConstant",
    "geo_target_constant": "geoTargetConstant",
}
INT64_KEYS = re.compile(r"^(id|.*Micros|impressions|clicks|criterionId|adGroupId|campaignId|assetId|labelId)$")


def b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def camel(s):
    parts = s.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def make_service_account(token_uri, email="ads-engine@test-project.iam.gserviceaccount.com", key=None):
    """(JSON-info, privát kulcs) a próbákhoz: friss RSA-kulcs, a token_uri az álszerverre mutat."""
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return {"type": "service_account", "project_id": "test-project", "private_key_id": "kid-1", "private_key": pem,
            "client_email": email, "token_uri": token_uri}, key


class Fail:
    def __init__(self, status, code=None, message="hiba", n=1, path=None, after=0):
        self.status, self.code, self.message, self.n, self.path, self.after = status, code, message, n, path, after


class MockGoogleAds:
    def __init__(self, discovery=None, email="ads-engine@test-project.iam.gserviceaccount.com", key=None):
        self.discovery = discovery
        self.email = email
        self.key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.requests = []                       # (method, path, headers, body) minden kérés
        self.failures = []
        self.lock = threading.RLock()
        self.next_id = 1000
        self.token_ok = True
        self.tokens = set()
        # fiókok: azonosító → adatok; a szolgáltatásfiók az `sa_access` fiókokhoz fér hozzá közvetlenül (az MCC alatti gyerekekhez is)
        self.accounts = {}
        self.sa_access = set()
        self.state = {}                          # customer_id → {típus: {resourceName: dict}}
        self.metrics = {}                        # customer_id → [(típus, azonosító, dátum, {metrika})]
        self.change_events = {}                  # customer_id → [dict]
        self.identity = {"verificationProgram": "ADVERTISER_IDENTITY_VERIFICATION", "verificationProgress": {"programStatus": "SUCCESS"}}
        self.constants = {
            "languageConstant": [{"resourceName": "languageConstants/1024", "id": "1024", "code": "hu", "name": "Hungarian", "targetable": True},
                                 {"resourceName": "languageConstants/1000", "id": "1000", "code": "en", "name": "English", "targetable": True}],
            "geoTargetConstant": [{"resourceName": "geoTargetConstants/2348", "id": "2348", "name": "Hungary", "countryCode": "HU",
                                   "targetType": "Country", "status": "ENABLED"},
                                  {"resourceName": "geoTargetConstants/2840", "id": "2840", "name": "United States", "countryCode": "US",
                                   "targetType": "Country", "status": "ENABLED"}]}
        self.server = None
        self.port = 0

    # ------------------------------------------------------------------ indítás
    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.handle(self, "GET")

            def do_POST(self):
                outer.handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def token_uri(self):
        return f"{self.base_url}/token"

    def service_account_info(self):
        return make_service_account(self.token_uri, self.email, self.key)[0]

    # ------------------------------------------------------------------ fiókok és állapot
    def add_account(self, cid, name="Teszt", manager=False, parent=None, currency="HUF", tz="Europe/Budapest", status="ENABLED",
                    auto_tagging=False, test=False, grant=False):
        self.accounts[cid] = {"id": cid, "descriptiveName": name, "manager": manager, "parent": parent, "currencyCode": currency,
                              "timeZone": tz, "status": status, "autoTaggingEnabled": auto_tagging, "testAccount": test,
                              "containsEuPoliticalAdvertising": "UNSPECIFIED"}
        self.state.setdefault(cid, {})
        self.metrics.setdefault(cid, [])
        self.change_events.setdefault(cid, [])
        if grant:
            self.sa_access.add(cid)
        return self

    def fail_next(self, status, code=None, message="hiba", n=1, path=None, after=0):
        """A következő n illeszkedő kérés hibát ad; after: ennyi illeszkedő kérést még átenged előtte."""
        self.failures.append(Fail(status, code, message, n, path, after))

    def table(self, cid, typ):
        return self.state.setdefault(cid, {}).setdefault(typ, {})

    def new_id(self):
        with self.lock:
            self.next_id += 1
            return self.next_id

    def set_metrics(self, cid, typ, rid, date, **metrics):
        self.metrics.setdefault(cid, []).append((typ, str(rid), date, metrics))

    # ------------------------------------------------------------------ HTTP
    def handle(self, h, method):
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        path = h.path
        ctype = h.headers.get("Content-Type", "")
        body = None
        if raw:
            if "json" in ctype:
                body = json.loads(raw.decode("utf-8"))
            else:
                body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
        with self.lock:
            self.requests.append((method, path, {k.lower(): v for k, v in h.headers.items()}, body))
        status, payload = self.route(method, path, {k.lower(): v for k, v in h.headers.items()}, body)
        data = json.dumps(payload).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("request-id", uuid.uuid4().hex[:16])
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def route(self, method, path, headers, body):
        if path == "/token" and method == "POST":
            return self.token(body or {})
        if not path.startswith("/v25/"):
            return 404, {"error": {"code": 404, "message": "nincs ilyen út"}}
        with self.lock:
            for f in self.failures:
                if f.n > 0 and (f.path is None or f.path in path):
                    if f.after > 0:
                        f.after -= 1
                        continue
                    f.n -= 1
                    return f.status, self.err_body(f.status, f.code, f.message)
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return 401, self.err_body(401, "authenticationError.OAUTH_TOKEN_INVALID", "érvénytelen token")
        if "developer-token" in headers:
            pass                                  # opcionális, a szerver figyelmen kívül hagyja
        rest = path[len("/v25/"):]
        if rest == "customers:listAccessibleCustomers":
            return 200, {"resourceNames": [f"customers/{c}" for c in sorted(self.sa_access)]}
        m = re.match(r"customers/(\d+)/(.+)$", rest)
        if not m:
            return 404, {"error": {"code": 404, "message": "ismeretlen út"}}
        cid, tail = m.group(1), m.group(2)
        err = self.check_access(cid, headers.get("login-customer-id"))
        if err:
            return err
        if tail == "googleAds:search":
            return self.search(cid, body or {})
        if tail == "googleAds:mutate":
            return self.mutate(cid, body or {})
        if tail == "getIdentityVerification":
            return 200, {"identityVerification": [dict(self.identity)]}
        return 404, {"error": {"code": 404, "message": f"nem támogatott: {tail}"}}

    def check_access(self, cid, login):
        acc = self.accounts.get(cid)
        if not acc:
            return 400, self.err_body(400, "requestError.INVALID_CUSTOMER_ID", "ismeretlen ügyfélazonosító")
        direct = cid in self.sa_access
        via_parent = acc.get("parent") in self.sa_access
        if direct and (login in (None, cid) or not acc.get("parent")):
            return None
        if via_parent and login == acc["parent"]:
            return None
        if direct:
            return None
        return 403, self.err_body(403, "authorizationError.USER_PERMISSION_DENIED",
                                  "User doesn't have permission to access customer. Note: If you're accessing a client customer, "
                                  "the manager's customer id must be set in the 'login-customer-id' header.")

    @staticmethod
    def err_body(status, code=None, message="hiba", errors=None):
        errs = errors
        if errs is None and code:
            cat, val = code.split(".", 1)
            errs = [{"errorCode": {cat: val}, "message": message}]
        body = {"error": {"code": status, "message": message,
                          "status": {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 429: "RESOURCE_EXHAUSTED",
                                     500: "INTERNAL", 503: "UNAVAILABLE"}.get(status, "UNKNOWN")}}
        if errs:
            body["error"]["details"] = [{"@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                                         "errors": errs, "requestId": uuid.uuid4().hex[:16]}]
        return body

    # ------------------------------------------------------------------ OAuth
    def token(self, form):
        if not self.token_ok:
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return 400, {"error": "unsupported_grant_type"}
        try:
            h64, c64, s64 = form["assertion"].split(".")
            claims = json.loads(b64url_decode(c64))
            self.key.public_key().verify(b64url_decode(s64), f"{h64}.{c64}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
        if claims.get("iss") != self.email:
            return 400, {"error": "invalid_grant", "error_description": "Not a valid email or user ID."}
        if claims.get("aud") != self.token_uri:
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT: aud mismatch."}
        if "https://www.googleapis.com/auth/adwords" not in claims.get("scope", ""):
            return 400, {"error": "invalid_scope", "error_description": "Invalid OAuth scope."}
        if claims.get("exp", 0) <= time.time():
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT: Token must be a short-lived token."}
        tok = f"ya29.mock-{uuid.uuid4().hex[:12]}"
        self.tokens.add(tok)
        return 200, {"access_token": tok, "expires_in": 3600, "token_type": "Bearer"}

    # ------------------------------------------------------------------ mutate
    def mutate(self, cid, body):
        if self.discovery:
            schema_errs = self.discovery.validate_mutate_request(body)
            if schema_errs:
                errs = [{"errorCode": {"requestError": "INVALID_ARGUMENT"}, "message": e} for e in schema_errs[:20]]
                return 400, self.err_body(400, message="Request contains an invalid argument.", errors=errs)
        ops = body.get("mutateOperations", [])
        validate_only = bool(body.get("validateOnly"))
        work = {t: dict(v) for t, v in self.state.setdefault(cid, {}).items()}      # sekély másolat (atomi: hiba esetén elvetjük)
        temp = {}
        results, errors = [], []
        for i, op in enumerate(ops):
            (key, inner), = op.items()
            if key not in OPS:
                errors.append(self.op_err(i, "requestError.UNSUPPORTED_OPERATION", f"a mock nem támogatja: {key}"))
                continue
            typ, coll = OPS[key]
            try:
                results.append(self.apply(cid, work, temp, i, typ, coll, inner, key))
            except MockError as e:
                errors.append(self.op_err(i, e.code, e.message, e.field))
        if errors:
            return 400, self.err_body(400, message="Request contains an invalid argument.", errors=errors)
        if validate_only:
            return 200, {}
        self.state[cid] = work
        return 200, {"mutateOperationResponses": results}

    @staticmethod
    def op_err(i, code, message, field=None):
        cat, val = code.split(".", 1)
        elements = [{"fieldName": "mutate_operations", "index": i}]
        if field:
            elements.append({"fieldName": field})
        return {"errorCode": {cat: val}, "message": message, "location": {"fieldPathElements": elements}}

    def resolve(self, cid, temp, name):
        """Ideiglenes (negatív) azonosítójú erőforrásnév → a valódi; ismeretlen ideiglenest elutasítja."""
        if isinstance(name, str) and re.search(r"/-\d+$", name):
            if name not in temp:
                raise MockError("mutateError.RESOURCE_NOT_FOUND", f"az ideiglenes azonosító még nincs létrehozva: {name}")
            return temp[name]
        return name

    def apply(self, cid, work, temp, i, typ, coll, inner, key):
        rkey = key.replace("Operation", "Result")
        tbl = work.setdefault(typ, {})
        if "create" in inner:
            res = json.loads(json.dumps(inner["create"]))
            rid = self.new_id()
            tmp_name = res.pop("resourceName", None)
            # hivatkozások feloldása
            for ref in ("campaignBudget", "campaign", "adGroup", "asset", "label", "adGroupAd", "adGroupCriterion"):
                if ref in res and ref != typ:
                    res[ref] = self.resolve(cid, temp, res[ref])
                    if res[ref] not in work.get(ref, {}) and res[ref] not in self.state.get(cid, {}).get(ref, {}):
                        raise MockError("mutateError.RESOURCE_NOT_FOUND", f"nem létező hivatkozás: {ref}={res[ref]}", ref)
            name = self.name_for(cid, coll, typ, rid, res)
            self.business_rules(cid, work, typ, res, create=True)
            res["resourceName"] = name
            self.assign_ids(typ, res, rid)
            tbl[name] = res
            if tmp_name:
                temp[tmp_name] = name
            return {rkey: {"resourceName": name}}
        if "update" in inner:
            res = inner["update"]
            name = self.resolve(cid, temp, res.get("resourceName"))
            cur = tbl.get(name) or self.state.get(cid, {}).get(typ, {}).get(name)
            if cur is None:
                raise MockError("mutateError.RESOURCE_NOT_FOUND", f"nincs ilyen erőforrás: {name}")
            tbl[name] = cur = json.loads(json.dumps(cur))
            mask = [m for m in (inner.get("updateMask") or "").split(",") if m]
            if not mask:
                raise MockError("fieldMaskError.FIELD_MASK_MISSING", "az update-hez updateMask kell")
            for field in mask:
                set_path(cur, field, get_path(res, field))
            self.business_rules(cid, work, typ, cur, create=False)
            return {rkey: {"resourceName": name}}
        if "remove" in inner:
            name = self.resolve(cid, temp, inner["remove"])
            cur = tbl.get(name) or self.state.get(cid, {}).get(typ, {}).get(name)
            if cur is None:
                raise MockError("mutateError.RESOURCE_NOT_FOUND", f"nincs ilyen erőforrás: {name}")
            tbl[name] = cur = json.loads(json.dumps(cur))
            cur["status"] = "REMOVED"
            return {rkey: {"resourceName": name}}
        raise MockError("requestError.INVALID_ARGUMENT", "create, update vagy remove kell")

    @staticmethod
    def name_for(cid, coll, typ, rid, res):
        base = f"customers/{cid}/{coll}/"
        if typ == "campaignCriterion":
            return f"{base}{res_id(res.get('campaign'))}~{rid}"
        if typ == "campaignLabel":
            return f"{base}{res_id(res.get('campaign'))}~{res_id(res.get('label'))}"
        if typ == "campaignAsset":
            return f"{base}{res_id(res.get('campaign'))}~{res_id(res.get('asset'))}~{res.get('fieldType', '')}"
        if typ in ("adGroupCriterion", "adGroupAd"):
            return f"{base}{res_id(res.get('adGroup'))}~{rid}"
        if typ == "adGroupLabel":
            return f"{base}{res_id(res.get('adGroup'))}~{res_id(res.get('label'))}"
        return f"{base}{rid}"

    @staticmethod
    def assign_ids(typ, res, rid):
        if typ in ("campaign", "campaignBudget", "adGroup", "asset", "label"):
            res["id"] = str(rid)
        if typ in ("adGroupCriterion", "campaignCriterion"):
            res["criterionId"] = str(rid)
        if typ == "adGroupAd":
            res.setdefault("ad", {})["id"] = str(rid)
        res.setdefault("status", "ENABLED")

    def business_rules(self, cid, work, typ, res, create):
        def all_of(t):
            merged = dict(self.state.get(cid, {}).get(t, {}))
            merged.update(work.get(t, {}))
            return merged

        if typ == "campaignBudget":
            if create and int(res.get("amountMicros", 0)) <= 0:
                raise MockError("campaignBudgetError.INVALID_BUDGET_AMOUNT", "a keret legyen nagyobb nullánál", "amount_micros")
            if create and res.get("explicitlyShared") is None:
                res["explicitlyShared"] = True            # az API alapértelmezése: névvel ellátott, megosztott keret
        if typ == "campaign":
            if res.get("containsEuPoliticalAdvertising") not in ("CONTAINS_EU_POLITICAL_ADVERTISING", "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING") and create:
                raise MockError("campaignError.MISSING_EU_POLITICAL_ADVERTISING_SELF_DECLARATION",
                                "hiányzik az EU politikai hirdetési nyilatkozat", "contains_eu_political_advertising")
            if create:
                dup = [c for c in all_of("campaign").values() if c.get("name") == res.get("name") and c.get("status") != "REMOVED"]
                if dup:
                    raise MockError("campaignError.DUPLICATE_CAMPAIGN_NAME", "már van ilyen nevű kampány", "name")
                budget = all_of("campaignBudget").get(res.get("campaignBudget"))
                if budget is None:
                    raise MockError("campaignError.CAMPAIGN_BUDGET_REQUIRED", "a kampányhoz keret kell", "campaign_budget")
                if res.get("advertisingChannelType") == "DEMAND_GEN" and budget.get("explicitlyShared"):
                    raise MockError("campaignError.CAMPAIGN_BUDGET_NOT_SHAREABLE", "a Demand Gen nem használhat megosztott keretet", "campaign_budget")
        if typ == "label" and create:
            if any(l.get("name") == res.get("name") and l.get("status") != "REMOVED" for l in all_of("label").values()):
                raise MockError("labelError.DUPLICATE_NAME", "már van ilyen címke", "name")
        if typ == "adGroup" and create:
            if any(a.get("name") == res.get("name") and a.get("campaign") == res.get("campaign") and a.get("status") != "REMOVED"
                   for a in all_of("adGroup").values()):
                raise MockError("adGroupError.DUPLICATE_ADGROUP_NAME", "már van ilyen nevű hirdetéscsoport", "name")
        if typ == "adGroupAd":
            rsa_ = (res.get("ad") or {}).get("responsiveSearchAd")
            if rsa_ is not None:
                self.check_rsa(rsa_)
                if create:
                    live = [a for a in all_of("adGroupAd").values() if a.get("adGroup") == res.get("adGroup") and a.get("status") == "ENABLED"
                            and (a.get("ad") or {}).get("responsiveSearchAd")]
                    if res.get("status", "ENABLED") == "ENABLED" and len(live) >= 3:
                        raise MockError("adGroupAdError.TOO_MANY_ENABLED_ADS", "hirdetéscsoportonként legfeljebb 3 aktív RSA", "ad")
                    if not (res.get("ad") or {}).get("finalUrls"):
                        raise MockError("adError.MISSING_FINAL_URLS", "a hirdetéshez végső URL kell", "ad.final_urls")
        if typ in ("adGroupCriterion", "campaignCriterion") and res.get("keyword"):
            kw = res["keyword"]
            text = kw.get("text", "")
            if len(text) > 80 or len(text.split()) > 10:
                raise MockError("criterionError.KEYWORD_TEXT_TOO_LONG" if len(text) > 80 else "criterionError.KEYWORD_HAS_TOO_MANY_WORDS",
                                "a kulcsszó túl hosszú", "keyword.text")
            if not text.strip():
                raise MockError("criterionError.INVALID_KEYWORD_TEXT", "üres kulcsszó", "keyword.text")
        if typ == "asset" and create:
            if res.get("imageAsset"):
                data = (res["imageAsset"] or {}).get("data")
                try:
                    raw = base64.b64decode(data or "", validate=True)
                except Exception:
                    raw = b""
                if len(raw) < 8:
                    raise MockError("assetError.INVALID_IMAGE", "érvénytelen képadat", "image_asset.data")
                if len(raw) > 5 * 1024 * 1024:
                    raise MockError("assetError.IMAGE_TOO_LARGE", "a kép legfeljebb 5 MB lehet", "image_asset.data")
                res["type"] = "IMAGE"
            elif res.get("sitelinkAsset"):
                s = res["sitelinkAsset"]
                if len(s.get("linkText", "")) > 25 or len(s.get("description1", "")) > 35 or len(s.get("description2", "")) > 35:
                    raise MockError("assetError.TOO_LONG", "a hivatkozás szövege túl hosszú", "sitelink_asset")
                res["type"] = "SITELINK"
            elif res.get("calloutAsset"):
                if len(res["calloutAsset"].get("calloutText", "")) > 25:
                    raise MockError("assetError.TOO_LONG", "a kiemelés túl hosszú", "callout_asset.callout_text")
                res["type"] = "CALLOUT"

    @staticmethod
    def check_rsa(r):
        heads, descs = r.get("headlines", []), r.get("descriptions", [])
        if not (3 <= len(heads) <= 15):
            raise MockError("adError.TOO_MANY_HEADLINES" if len(heads) > 15 else "adError.TOO_FEW_HEADLINES", "3–15 cím kell", "ad.responsive_search_ad.headlines")
        if not (2 <= len(descs) <= 4):
            raise MockError("adError.TOO_MANY_DESCRIPTIONS" if len(descs) > 4 else "adError.TOO_FEW_DESCRIPTIONS", "2–4 leírás kell", "ad.responsive_search_ad.descriptions")
        for h in heads:
            if len(h.get("text", "")) > 30:
                raise MockError("adError.TOO_LONG", f"a cím túl hosszú (30): {h['text']!r}", "ad.responsive_search_ad.headlines")
        for d in descs:
            if len(d.get("text", "")) > 90:
                raise MockError("adError.TOO_LONG", f"a leírás túl hosszú (90): {d['text'][:20]!r}…", "ad.responsive_search_ad.descriptions")
        if len({h["text"].lower() for h in heads}) != len(heads):
            raise MockError("adError.DUPLICATE_HEADLINES", "a címek nem ismétlődhetnek", "ad.responsive_search_ad.headlines")
        for p in ("path1", "path2"):
            if len(r.get(p, "")) > 15:
                raise MockError("adError.TOO_LONG", f"az útvonal túl hosszú (15): {p}", f"ad.responsive_search_ad.{p}")

    # ------------------------------------------------------------------ GAQL
    def search(self, cid, body):
        q = body.get("query", "")
        try:
            rows = self.run_gaql(cid, q)
        except GaqlError as e:
            return 400, self.err_body(400, message="Error in query", errors=[{"errorCode": {"queryError": "BAD_QUERY"}, "message": str(e)}])
        return 200, {"results": rows, "totalResultsCount": str(len(rows)), "fieldMask": ""}

    def run_gaql(self, cid, q):
        m = re.match(r"\s*SELECT\s+(.+?)\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+?))?(?:\s+ORDER\s+BY\s+.+?)?(?:\s+LIMIT\s+(\d+))?\s*$", q, re.I | re.S)
        if not m:
            raise GaqlError(f"nem értelmezhető lekérdezés: {q[:80]}")
        fields = [f.strip() for f in m.group(1).split(",")]
        frm, where, limit = m.group(2).lower(), m.group(3), m.group(4)
        if frm not in GAQL_TABLES:
            raise GaqlError(f"a mock nem támogatja a FROM {frm} táblát")
        typ = GAQL_TABLES[frm]
        conds = split_conditions(where) if where else []
        date_filter, date_conds = None, []
        sources = []
        if typ == "customer":
            acc = self.accounts[cid]
            sources.append({"customer": json.loads(json.dumps(acc))})
        elif typ in self.constants:
            for obj in self.constants[typ]:
                sources.append({camel(frm): json.loads(json.dumps(obj))})
        else:
            for name, r in self.state.get(cid, {}).get(typ, {}).items():
                # kapcsolt erőforrások (kampány → keret; hirdetés/kulcsszó → hirdetéscsoport → kampány)
                sources.append(self.join_row(cid, typ, frm, r))
        # metrikák: ha a SELECT tartalmaz metrics.*, minden sorhoz összegezzük (vagy dátumonként bontjuk)
        wants_metrics = any(f.startswith("metrics.") for f in fields)
        wants_date = "segments.date" in fields
        out = []
        for src in sources:
            variants = [(src, None)]
            if wants_metrics or wants_date:
                variants = self.metric_variants(cid, typ, src, wants_date)
            for s, date in variants:
                s = dict(s)
                if date:
                    s["segments"] = {"date": date}
                if wants_metrics and "metrics" not in s:
                    s["metrics"] = {}
                if all(self.match(s, c) for c in conds):
                    out.append(self.project(s, fields))
        if limit:
            out = out[:int(limit)]
        return out

    def join_row(self, cid, typ, frm, r):
        key = {"adGroupCriterion": "adGroupCriterion"}.get(typ, camel(frm))
        row = {key: json.loads(json.dumps(r))}
        st = self.state.get(cid, {})
        if typ == "campaign":
            b = st.get("campaignBudget", {}).get(r.get("campaignBudget"))
            if b:
                row["campaignBudget"] = json.loads(json.dumps(b))
        if "adGroup" in r and typ in ("adGroupAd", "adGroupCriterion"):
            ag = st.get("adGroup", {}).get(r["adGroup"])
            if ag:
                row["adGroup"] = json.loads(json.dumps(ag))
                c = st.get("campaign", {}).get(ag.get("campaign"))
                if c:
                    row["campaign"] = json.loads(json.dumps(c))
        if "campaign" in r and isinstance(r["campaign"], str) and typ in ("campaignCriterion", "campaignLabel", "campaignAsset", "adGroup"):
            c = st.get("campaign", {}).get(r["campaign"])
            if c:
                row["campaign"] = json.loads(json.dumps(c))
        if typ == "campaignLabel":
            l = st.get("label", {}).get(r.get("label"))
            if l:
                row["label"] = json.loads(json.dumps(l))
        if typ == "campaignAsset":
            a = st.get("asset", {}).get(r.get("asset"))
            if a:
                row["asset"] = json.loads(json.dumps(a))
        if typ == "campaign":
            row["campaign"].setdefault("primaryStatus", "ELIGIBLE" if r.get("status") == "ENABLED" else "PAUSED")
            labels = [lb for lb in st.get("campaignLabel", {}).values() if lb.get("campaign") == r["resourceName"] and lb.get("status") != "REMOVED"]
            row["campaign"]["labels"] = [lb["label"] for lb in labels]
        return row

    def metric_variants(self, cid, typ, src, wants_date):
        key_obj = {"campaign": "campaign", "adGroup": "adGroup", "adGroupAd": "adGroupAd", "adGroupCriterion": "adGroupCriterion"}.get(typ, typ)
        rec = src.get(key_obj, {}) or {}
        rid = str(rec.get("resourceName", "")).rsplit("/", 1)[-1] if typ in ("campaign", "adGroup", "adGroupAd", "adGroupCriterion") else None
        mm = [m for m in self.metrics.get(cid, []) if m[0] == typ and m[1] in (rid, str(rec.get("id", "")))]
        if not mm:
            return [(src, None)] if not wants_date else []
        if wants_date:
            by = {}
            for _, _, d, vals in mm:
                acc = by.setdefault(d, {})
                for k, v in vals.items():
                    acc[k] = acc.get(k, 0) + v
            return [(dict(src, metrics=self.fmt_metrics(v)), d) for d, v in sorted(by.items())]
        total = {}
        for _, _, d, vals in mm:
            for k, v in vals.items():
                total[k] = total.get(k, 0) + v
        return [(dict(src, metrics=self.fmt_metrics(total)), None)]

    @staticmethod
    def fmt_metrics(vals):
        out = {}
        for k, v in vals.items():
            out[k] = str(int(v)) if k in ("impressions", "clicks", "costMicros") else v
        if "clicks" in vals and "impressions" in vals and vals["impressions"]:
            out["ctr"] = vals["clicks"] / vals["impressions"]
        if "clicks" in vals and "costMicros" in vals and vals["clicks"]:
            out["averageCpc"] = vals["costMicros"] / vals["clicks"]
        return out

    @staticmethod
    def match(row, cond):
        field, op, raw = cond
        val = get_path(row, ".".join(camel(p) for p in field.split(".")))
        if op == "DURING":
            return True
        if op == "BETWEEN":
            lo, hi = re.findall(r"'([^']*)'", raw)[:2]
            return val is None or lo <= str(val) <= hi
        if isinstance(val, bool):
            val = str(val).upper()
        cmp = lambda v: v if v is None else str(v)
        target = parse_literal(raw)
        if op == "IN":
            return cmp(val) in [str(t) for t in target]
        if op == "NOT IN":
            return cmp(val) not in [str(t) for t in target]
        if op == "=":
            return cmp(val) == str(target)
        if op in ("!=", "<>"):
            return cmp(val) != str(target)
        if op == "LIKE":
            return re.fullmatch(re.escape(str(target)).replace("%", ".*"), cmp(val) or "") is not None
        try:
            a, b = float(val), float(target)
        except (TypeError, ValueError):
            return False
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}.get(op, False)

    @staticmethod
    def project(row, fields):
        out = {}
        for f in fields:
            path = ".".join(camel(p) for p in f.split("."))
            val = get_path(row, path)
            if val is not None:
                set_path(out, path, val)
        return out


class MockError(Exception):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


class GaqlError(Exception):
    pass


# ------------------------------------------------------------------ segédek
def res_id(name):
    return str(name or "").rsplit("/", 1)[-1].split("~")[0] if name else ""


def get_path(obj, path):
    cur = obj
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def set_path(obj, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj.setdefault(p, {})
    obj[parts[-1]] = value


def split_conditions(where):
    # a BETWEEN 'a' AND 'b' saját AND-jét ne vágjuk szét
    where = re.sub(r"(BETWEEN\s+'[^']*')\s+AND\s+('[^']*')", r"\1 __AND__ \2", where, flags=re.I)
    parts, depth, buf = [], 0, ""
    for t in re.split(r"(\s+AND\s+)", where, flags=re.I):
        if re.fullmatch(r"\s+AND\s+", t, re.I) and depth == 0:
            parts.append(buf)
            buf = ""
            continue
        buf += t
        depth += t.count("(") - t.count(")")
    if buf.strip():
        parts.append(buf)
    conds = []
    for p in parts:
        p = p.replace(" __AND__ ", " AND ")
        m = re.match(r"\s*([\w.]+)\s+(NOT IN|IN|DURING|BETWEEN|LIKE|!=|<>|>=|<=|=|>|<)\s+(.+?)\s*$", p, re.I | re.S)
        if not m:
            raise GaqlError(f"nem értelmezhető feltétel: {p.strip()[:60]}")
        conds.append((m.group(1), m.group(2).upper(), m.group(3)))
    return conds


def parse_literal(raw):
    raw = raw.strip()
    if raw.startswith("("):
        return [parse_literal(x) for x in re.findall(r"'[^']*'|[^,()\s]+", raw[1:-1])]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw.upper() if raw.lower() in ("true", "false") else raw
