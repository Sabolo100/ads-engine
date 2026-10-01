"""Google Ads REST-kliens (v25), csak szabványos Pythonnal.

Végpontok (https://googleads.googleapis.com/v25/…):
  customers:listAccessibleCustomers · customers/{id}/googleAds:search · …/googleAds:mutate · …/getIdentityVerification
Fejlécek: Authorization: Bearer + login-customer-id (az MCC). A `developer-token` fejlécet NEM küldjük: 2026-09-09 óta
megszűnt, a hozzáférési szint a Google Cloud-projekthez kötött (a fejléc opcionális, a szerver figyelmen kívül hagyja).

A mutate-ot mindig egy kérésben, atomian küldjük (partialFailure=false), ideiglenes (negatív) azonosítókkal; a `validate_only`
próbafuttatás: a Google ellenőrzi, de nem hajtja végre.
"""
import time

from .. import http, log

API_VERSION = "v25"
BASE_URL = "https://googleads.googleapis.com"

# a Google által átmenetinek jelölt hibák: rövid várakozás után érdemes újra
TRANSIENT = {"internalError.INTERNAL_ERROR", "internalError.TRANSIENT_ERROR", "databaseError.CONCURRENT_MODIFICATION",
             "databaseError.DATA_CONSTRAINT_VIOLATION_RETRYABLE", "quotaError.RESOURCE_TEMPORARILY_EXHAUSTED"}

_HINTS = {
    "authorizationError.USER_PERMISSION_DENIED": "a szolgáltatásfiók nincs hozzáadva ehhez a fiókhoz (vagy az MCC-hez), "
                                                  "vagy a login-customer-id hibás – add hozzá a Google Ads Access and security-ben",
    "authorizationError.CUSTOMER_NOT_ENABLED": "a Google Ads-fiók nincs engedélyezve (még nem aktív, felfüggesztett vagy lezárt)",
    "authorizationError.DEVELOPER_TOKEN_NOT_APPROVED": "a Cloud-projektnek még nincs Google Ads API hozzáférési szintje (Apply for access)",
    "authenticationError.NOT_ADS_USER": "ehhez a Google-fiókhoz nincs Google Ads-fiók társítva",
    "authenticationError.OAUTH_TOKEN_INVALID": "érvénytelen belépési token",
    "quotaError.RESOURCE_EXHAUSTED": "elfogyott a napi API-keret (Explorer szint: 2 880 művelet/nap)",
    "quotaError.RESOURCE_TEMPORARILY_EXHAUSTED": "a Google ideiglenesen korlátoz – lassítunk",
    "campaignError.MISSING_EU_POLITICAL_ADVERTISING_SELF_DECLARATION": "a kampányból hiányzik az EU politikai hirdetési nyilatkozat (containsEuPoliticalAdvertising)",
    "criterionError.MISSING_EU_POLITICAL_ADVERTISING_SELF_DECLARATION": "a kampányból hiányzik az EU politikai hirdetési nyilatkozat (containsEuPoliticalAdvertising)",
    "campaignError.DUPLICATE_CAMPAIGN_NAME": "már van ilyen nevű kampány (az újrafuttatás nem duplikál)",
    "mutateError.RESOURCE_DOES_NOT_SUPPORT_VALIDATE_ONLY": "ez az erőforrás nem támogatja a próbafuttatást (validate_only)",
    "requestError.CUSTOMER_NOT_FOUND": "hibás vagy nem elérhető ügyfélfiók-azonosító",
    "policyViolationError.POLICY_ERROR": "a hirdetés szabályt sért – a szöveget javítani kell",
}


class GoogleAdsError(Exception):
    def __init__(self, status, errors, message="", request_id="", raw=None):
        self.status, self.errors, self.request_id, self.raw = status, errors or [], request_id, raw
        self.message = message
        codes = ", ".join(self.codes) or "?"
        hint = self.hint()
        loc = next((e["path"] for e in self.errors if e.get("path")), "")
        super().__init__(f"Google Ads hiba {status}: {message or ''} [{codes}]" + (f" @ {loc}" if loc else "") +
                         (f" – {hint}" if hint else "") + (f" · request-id {request_id}" if request_id else ""))

    @property
    def codes(self):
        return [e["code"] for e in self.errors if e.get("code")]

    def has(self, code):
        """code: teljes ('campaignError.DUPLICATE_CAMPAIGN_NAME') vagy csak a vége ('DUPLICATE_CAMPAIGN_NAME')."""
        return any(c == code or c.endswith("." + code) for c in self.codes)

    @property
    def transient(self):
        """Érdemes-e újra próbálni: hálózati hiba (0), kvóta (429), szerverhiba, vagy a Google átmenetinek jelölt kódja."""
        return self.status in (0, 429, 500, 502, 503, 504) or any(c in TRANSIENT for c in self.codes)

    def hint(self):
        for c in self.codes:
            if c in _HINTS:
                return _HINTS[c]
        return ""


def _path_of(location):
    parts = []
    for el in (location or {}).get("fieldPathElements", []):
        name = el.get("fieldName", "")
        parts.append(name + (f"[{el['index']}]" if "index" in el else ""))
    return ".".join(parts)


def parse_failure(body):
    """A Google hibatörzséből (error.details[].errors[]) egységes lista: {code, message, path}."""
    err = (body or {}).get("error", body or {}) if isinstance(body, dict) else {}
    out, request_id = [], ""
    for d in err.get("details", []) or []:
        request_id = d.get("requestId", request_id)
        for e in d.get("errors", []) or []:
            ec = e.get("errorCode", {}) or {}
            code = ""
            for k, v in ec.items():
                code = f"{k}.{v}"
                break
            out.append({"code": code, "message": e.get("message", ""), "path": _path_of(e.get("location"))})
    return err.get("message", ""), out, request_id


class GoogleAdsClient:
    def __init__(self, tokens, login_customer_id=None, version=API_VERSION, base_url=BASE_URL, request=http.request,
                 sleep=time.sleep, observer=None, transient_retries=3):
        self.tokens, self.login_customer_id = tokens, (str(login_customer_id) if login_customer_id else None)
        self.version, self.base_url, self._request, self._sleep = version, base_url.rstrip("/"), request, sleep
        self.observer = observer          # observer(method, path, request_body, response_body, error) – auditnapló
        self.transient_retries = transient_retries
        self.calls = 0
        self.last_request_id = ""         # a Google request-id fejléce (hibakereséshez, a művelet-naplóba kerül)

    # ------------------------------------------------------------------ alap
    def _headers(self):
        h = {"Authorization": f"Bearer {self.tokens.token()}"}
        if self.login_customer_id:
            h["login-customer-id"] = self.login_customer_id
        return h

    def _call(self, method, path, body=None):
        url = f"{self.base_url}/{self.version}/{path}"
        for attempt in range(self.transient_retries + 1):
            self.calls += 1
            try:
                # az újrapróbálást itt végezzük (a Google hibakódjai alapján), ezért a HTTP-rétegben nincs
                r = self._request(method, url, headers=self._headers(), json_body=body, timeout=60, retries=0)
                data = r.json() if r.body else {}
                self.last_request_id = r.headers.get("request-id", "")
                self._observe(method, path, body, data, None)
                return data
            except http.HttpError as e:
                self.last_request_id = (e.headers or {}).get("request-id", "")
                payload = e.json()
                message, errors, request_id = parse_failure(payload if isinstance(payload, dict) else {})
                if not errors and e.status and e.status != 0:
                    errors = [{"code": "", "message": e.body[:200].decode("utf-8", "replace"), "path": ""}]
                ge = GoogleAdsError(e.status, errors, message or str(e)[:200], request_id, payload)
                if ge.transient and attempt < self.transient_retries:
                    log.warn("google_ads.retry", path=path, attempt=attempt + 1, codes=ge.codes)
                    self._sleep(min(2 ** attempt * 2, 30))
                    continue
                self._observe(method, path, body, payload, ge)
                raise ge from None
        raise RuntimeError("unreachable")  # pragma: no cover

    def _observe(self, method, path, body, response, error):
        if self.observer:
            try:
                self.observer(method, path, body, response, error)
            except Exception as e:      # a naplózás hibája ne akassza meg a műveletet
                log.warn("google_ads.observer_error", error=str(e))

    # ------------------------------------------------------------------ olvasás
    def list_accessible_customers(self):
        """Azok az ügyfélfiókok, amelyekhez a szolgáltatásfióknak közvetlen hozzáférése van (azonosítók, kötőjel nélkül)."""
        data = self._call("GET", "customers:listAccessibleCustomers")
        return [n.split("/")[-1] for n in data.get("resourceNames", [])]

    def search(self, customer_id, query):
        """GAQL-lekérdezés, az összes oldallal (pageToken). A pageSize elavult, nem küldjük."""
        rows, token = [], None
        while True:
            body = {"query": query}
            if token:
                body["pageToken"] = token
            data = self._call("POST", f"customers/{customer_id}/googleAds:search", body)
            rows.extend(data.get("results", []))
            token = data.get("nextPageToken")
            if not token:
                return rows

    def get_identity_verification(self, customer_id):
        return self._call("GET", f"customers/{customer_id}/getIdentityVerification")

    # ------------------------------------------------------------------ írás
    def mutate(self, customer_id, operations, *, validate_only=False, partial_failure=False, response_content_type="RESOURCE_NAME_ONLY"):
        """Atomi mutate: operations = [{'campaignOperation': {'create': {...}}}, …]. Egy kérés, ideiglenes azonosítókkal."""
        body = {"mutateOperations": operations, "partialFailure": bool(partial_failure), "validateOnly": bool(validate_only),
                "responseContentType": response_content_type}
        return self._call("POST", f"customers/{customer_id}/googleAds:mutate", body)


def customer_id_clean(value):
    """'123-456-7890' → '1234567890'."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def micros(amount):
    """Pénzösszeg (pl. HUF) → micros (a Google egysége: 1 000 000 = 1 egység)."""
    return int(round(float(amount) * 1_000_000))


def from_micros(value):
    return int(value) / 1_000_000
