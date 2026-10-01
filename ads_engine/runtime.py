"""Közös összeszerelés: a beállításokból Google-kliens és állapottároló.

A tesztek az álszerver címét a GADS_BASE_URL környezeti változóval adják meg.
"""
from . import log
from .google.auth import ServiceAccount, TokenProvider
from .google.client import GoogleAdsClient
from .llm import LLM
from .openai_images import OpenAIImages
from .store import Store
from .umami import Umami


def open_store(settings):
    return Store(settings.db_path)


def service_account(settings):
    """A beállított szolgáltatásfiók (vagy None, ha nincs kulcs megadva). AuthError, ha a kulcs hibás."""
    return ServiceAccount.from_env(settings.env)


def fetch_kwargs(settings):
    """Csak próbákhoz: a helyi álszerverről is letölthessen az Ads Pack (a valódi üzemben a privát címek és a http tiltott)."""
    if settings.env.get("ADS_TEST_ALLOW_PRIVATE") == "1":
        return {"allow_private": True, "allow_http": True}
    return {}


def llm(settings, store=None):
    """Az AI-kliens (Claude); None, ha nincs ANTHROPIC_API_KEY: a motor ilyenkor AI nélkül dolgozik (kevesebbet módosít, de szól)."""
    if not settings.anthropic_key:
        return None
    return LLM(settings.anthropic_key, settings.llm_model, base_url=settings.env.get("ANTHROPIC_BASE_URL") or None, store=store)


def openai(settings):
    """Az OpenAI-képkliens (gpt-image-2); None, ha nincs OPENAI_API_KEY: a motor ilyenkor új AI-képet nem kér."""
    if not settings.openai_key:
        return None
    env = settings.env
    return OpenAIImages(settings.openai_key, model=env.get("IMAGE_MODEL") or "gpt-image-2", quality=env.get("IMAGE_QUALITY") or "high",
                        base_url=env.get("OPENAI_BASE_URL") or "https://api.openai.com")


def umami(settings):
    """A csak-olvasó Umami-kliens; None, ha nincs beállítva."""
    if not (settings.umami_url and settings.umami_user and settings.umami_password):
        return None
    return Umami(settings.umami_url, settings.umami_user, settings.umami_password)


def google_client(settings, observer=None):
    sa = service_account(settings)
    if sa is None:
        return None
    client = GoogleAdsClient(TokenProvider(sa), login_customer_id=settings.login_customer_id or None,
                             version=settings.api_version, base_url=settings.base_url, observer=observer)
    log.info("google.client", email=sa.client_email, version=settings.api_version, login=settings.login_customer_id or "-")
    return client
