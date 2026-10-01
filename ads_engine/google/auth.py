"""Google szolgáltatásfiókos belépés: RS256 JWT → access token.

Nincs frissítő token, nincs OAuth-képernyő, nincs 7 napos lejárat (a Google Ads 2024 novembere óta engedi, hogy a
szolgáltatásfiók e-mail-címét felhasználóként adjuk hozzá a fiókhoz/MCC-hez). A kulcs (JSON) a Coolify titkos
környezeti változójában él:
  GADS_SA_JSON_B64   a JSON-kulcsfájl base64-ben (ez az ajánlott: a sortörések nem vesznek el)
  GADS_SA_JSON_FILE  vagy a fájl útvonala (helyi próbákhoz)
  GADS_SA_JSON       vagy maga a JSON (nyers)
A privát kulcs soha nem kerül a naplóba (log.add_secret).
"""
import base64
import json
import pathlib
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .. import http, log

SCOPE_ADS = "https://www.googleapis.com/auth/adwords"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class AuthError(Exception):
    pass


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class ServiceAccount:
    def __init__(self, info):
        missing = [k for k in ("client_email", "private_key") if not info.get(k)]
        if missing:
            raise AuthError(f"A szolgáltatásfiók-kulcsból hiányzik: {', '.join(missing)} – a letöltött JSON-kulcsfájlt kell használni.")
        self.client_email = info["client_email"]
        self.private_key_id = info.get("private_key_id")
        self.token_uri = info.get("token_uri") or DEFAULT_TOKEN_URI
        self.project_id = info.get("project_id")
        pem = info["private_key"]
        if "\\n" in pem and "\n" not in pem:      # környezeti változóban elveszett sortörések
            pem = pem.replace("\\n", "\n")
        try:
            self._key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        except Exception as e:
            raise AuthError("A privát kulcs (private_key) nem érvényes PEM – a JSON-kulcsfájl sérült. "
                            "Töltsd le újra a Google Cloudból (új kulcs), és add meg base64-ben.") from e
        log.add_secret(pem)
        log.add_secret(info["private_key"])

    @classmethod
    def from_env(cls, env):
        raw = None
        if env.get("GADS_SA_JSON_B64"):
            try:
                raw = base64.b64decode(env["GADS_SA_JSON_B64"].strip(), validate=True).decode("utf-8")
            except Exception as e:
                raise AuthError("A GADS_SA_JSON_B64 nem érvényes base64 (a teljes JSON-fájl tartalmát kell kódolni).") from e
        elif env.get("GADS_SA_JSON_FILE"):
            try:
                raw = pathlib.Path(env["GADS_SA_JSON_FILE"]).read_text(encoding="utf-8")
            except OSError as e:
                raise AuthError(f"A GADS_SA_JSON_FILE nem olvasható: {e.strerror}") from e
        elif env.get("GADS_SA_JSON"):
            raw = env["GADS_SA_JSON"]
        if not raw:
            return None
        try:
            return cls(json.loads(raw))
        except ValueError as e:
            raise AuthError("A szolgáltatásfiók-kulcs nem érvényes JSON.") from e

    def sign_jwt(self, scope, now, lifetime=3600):
        header = {"alg": "RS256", "typ": "JWT"}
        if self.private_key_id:
            header["kid"] = self.private_key_id
        claims = {"iss": self.client_email, "scope": scope, "aud": self.token_uri, "iat": int(now), "exp": int(now) + lifetime}
        signing_input = (b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
                         b64url(json.dumps(claims, separators=(",", ":")).encode()))
        sig = self._key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return signing_input + "." + b64url(sig)


_HINTS = {
    "invalid_grant": "a kulcs érvénytelen, visszavont vagy a szolgáltatásfiók törölve van – hozz létre új JSON-kulcsot",
    "invalid_scope": "a kért jogkör (adwords) nem engedélyezett",
    "unauthorized_client": "a szolgáltatásfiók nem jogosult ehhez a belépéshez",
    "invalid_request": "hibás belépési kérés",
    "disabled_client": "a szolgáltatásfiók le van tiltva",
}


class TokenProvider:
    def __init__(self, sa, scope=SCOPE_ADS, request=http.request, clock=time.time, token_uri=None):
        self.sa, self.scope, self._request, self._clock = sa, scope, request, clock
        self.token_uri = token_uri or sa.token_uri
        self._token, self._exp = None, 0

    def token(self):
        now = self._clock()
        if self._token and now < self._exp - 120:
            return self._token
        assertion = self.sa.sign_jwt(self.scope, now)
        try:
            r = self._request("POST", self.token_uri, form={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                            "assertion": assertion}, retries=2)
        except http.HttpError as e:
            body = e.json() or {}
            code = body.get("error", "")
            hint = _HINTS.get(code, "")
            desc = body.get("error_description", "")
            if "Invalid JWT Signature" in desc:
                hint = "a JSON-kulcsot visszavonták vagy hibás – hozz létre újat"
            if "Token must be a short-lived token" in desc or "iat" in desc:
                hint = "a szerver órája eltér – ellenőrizd az időt"
            raise AuthError(f"Google belépési hiba: {code or e.status} {desc}".strip() + (f" – {hint}" if hint else "")) from None
        data = r.json()
        self._token = data["access_token"]
        self._exp = now + int(data.get("expires_in", 3600))
        log.add_secret(self._token)
        return self._token
