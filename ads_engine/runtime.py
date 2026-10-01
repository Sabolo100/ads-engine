"""Közös összeszerelés: a beállításokból Google-kliens és állapottároló.

A tesztek az álszerver címét a GADS_BASE_URL környezeti változóval adják meg.
"""
from . import log
from .google.auth import ServiceAccount, TokenProvider
from .google.client import GoogleAdsClient
from .store import Store


def open_store(settings):
    return Store(settings.db_path)


def service_account(settings):
    """A beállított szolgáltatásfiók (vagy None, ha nincs kulcs megadva). AuthError, ha a kulcs hibás."""
    return ServiceAccount.from_env(settings.env)


def google_client(settings, observer=None):
    sa = service_account(settings)
    if sa is None:
        return None
    client = GoogleAdsClient(TokenProvider(sa), login_customer_id=settings.login_customer_id or None,
                             version=settings.api_version, base_url=settings.base_url, observer=observer)
    log.info("google.client", email=sa.client_email, version=settings.api_version, login=settings.login_customer_id or "-")
    return client
