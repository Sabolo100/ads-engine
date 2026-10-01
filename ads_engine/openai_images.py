"""OpenAI képgenerálás (gpt-image-2, POST /v1/images/generations), csak szabványos Python.

A hívások száma KEMÉNY korlát alatt van (projektenként, ISO-hetenként): a számláló az OpenAI-hívás ELŐTT nő, a sikertelen
próba is beleszámít (pénzbe kerülhetett), ezért nincs automatikus újrapróbálás. A méretek a modell korlátain belül maradnak
(leghosszabb él ≤ 3840, ~4,2 megapixel), és a Google Ads képarányaihoz igazodnak: a végső (1200 px körüli) méretet a vágás adja.
"""
import base64
import dataclasses

from . import http, log

SIZES = {"landscape": "1920x1008", "square": "1536x1536", "portrait": "1536x1920"}      # 1,905:1 · 1:1 · 4:5 (a Google arányai ±1 %-on belül)
MAX_PIXELS = 4_200_000


class ImageAPIError(Exception):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal                    # kulcs-/keret-/hálózati hiba: a további kéréseknek sincs értelme


class ImageBudgetExceeded(ImageAPIError):
    """A heti AI-képkeret elfogyott: a motor nem hív az OpenAI-ra."""


@dataclasses.dataclass
class Generated:
    data: bytes
    size: str
    request_id: str = ""


class OpenAIImages:
    def __init__(self, api_key, *, model="gpt-image-2", quality="high", base_url="https://api.openai.com", request=http.request, timeout=900):
        if not api_key:
            raise ImageAPIError("Nincs OPENAI_API_KEY beállítva.")
        self.model, self.quality, self.base_url, self._request, self.timeout = model, quality, base_url.rstrip("/"), request, timeout
        self._key = api_key
        log.add_secret(api_key)

    def generate(self, prompt, kind):
        """Egy kép a megadott fajtára (landscape | square | portrait). ImageAPIError minden hibára, emberi nyelvű üzenettel."""
        size = SIZES[kind]
        w, h = (int(x) for x in size.split("x"))
        if w * h > MAX_PIXELS or max(w, h) > 3840:
            raise ImageAPIError(f"a kért méret ({size}) túl nagy a modellnek")
        try:
            r = self._request("POST", f"{self.base_url}/v1/images/generations", headers={"Authorization": f"Bearer {self._key}"},
                              json_body={"model": self.model, "prompt": prompt, "size": size, "quality": self.quality, "n": 1},
                              timeout=self.timeout, retries=0)
            body = r.json()
            data = base64.b64decode(body["data"][0]["b64_json"], validate=True)
        except http.HttpError as e:
            msg, fatal = self._explain(e)
            raise ImageAPIError(msg, fatal=fatal) from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ImageAPIError("Az OpenAI válasza nem tartalmaz képet.") from None
        return Generated(data, size, r.headers.get("x-request-id", ""))

    @staticmethod
    def _explain(e):
        """(emberi nyelvű üzenet, végzetes-e): végzetes a kulcs-, a keret- és a hálózati hiba; a biztonsági szűrő csak az adott kérést vágja ki."""
        err = ((e.json() or {}).get("error") or {}) if e.status else {}
        code, msg = err.get("code") or "", err.get("message") or ""
        if e.status == 401:
            return "Az OPENAI_API_KEY érvénytelen.", True
        if code in ("moderation_blocked", "content_policy_violation") or "safety" in msg.lower():
            return "Az OpenAI biztonsági szűrője visszautasította a képkérést.", False
        if e.status == 429 or code in ("insufficient_quota", "billing_hard_limit_reached"):
            return "Az OpenAI korlátoz vagy elfogyott a számlakeret (429): új képet most nem kérek.", True
        if e.status == 0:
            return "Az OpenAI nem érhető el (hálózati hiba vagy időtúllépés).", True
        return f"Az OpenAI hibát adott ({e.status}): {msg[:160] or code or 'ismeretlen hiba'}", False
