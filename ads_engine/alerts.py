"""Azonnali értesítő levelek (fékek, elutasított hirdetés, kézzel módosított kampány): ismétlés ellen védve.

Ugyanazt a kulcsú értesítést `dedupe_days` napon belül nem küldjük újra. A levél hibája (SMTP) soha nem állítja meg a motort.
"""
import datetime as dt

from . import log, mailer, version


def footer(settings):
    return f"\n\n—\nAds Engine {version.label()} · üzemmód: {settings.mode}"


def notify(settings, store, project, key, subject, lines, *, dedupe_days=7, send=mailer.send, now=None):
    """True, ha elment (vagy dry/üres címzett miatt kihagyva False). A kulcs alapján ismétlődés ellen védett."""
    now = now or dt.datetime.now(dt.timezone.utc)
    sent = store.get(f"{project.slug}.alerts", {}) or {}
    last = sent.get(key)
    if last and (now - dt.datetime.fromisoformat(last)) < dt.timedelta(days=dedupe_days):
        return False
    body = "\n".join(lines) + footer(settings)
    try:
        send(settings, f"[{project.name}] {subject}", body)
    except mailer.MailError as e:
        log.warn("alert.mail_failed", key=key, error=str(e))
        return False
    sent[key] = now.isoformat(timespec="seconds")
    store.put(f"{project.slug}.alerts", sent)
    return True
