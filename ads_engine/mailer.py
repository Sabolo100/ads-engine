"""Levélküldés (SMTP, csak szabványos Python): szöveg + HTML változat, UTF-8.

SMTP_SECURITY: ssl (alap, 465-ös port) | starttls (587) | plain (csak próbákhoz). A jelszó soha nem kerül a naplóba.
Hiba esetén MailError (a hívó eldönti, mi történjen: a motor ettől nem áll le).
"""
import email.utils
import smtplib
import ssl
from email.message import EmailMessage

from . import log


class MailError(Exception):
    pass


def build(sender, recipients, subject, text, html=None, *, reply_to=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=(sender.split("@")[-1].strip("> ") or "localhost"))
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send(settings, subject, text, html=None, *, recipients=None, security=None):
    """Egy levél a beállított SMTP-n a REPORT_TO címzetteknek. Visszaadja a címzetteket; MailError, ha nem megy."""
    rcpt = list(recipients or settings.report_to)
    if not settings.smtp_host or not rcpt:
        raise MailError("Nincs beállítva az SMTP vagy a címzett (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, REPORT_TO).")
    sender = settings.smtp_from or settings.smtp_user
    msg = build(sender, rcpt, subject, text, html)
    mode = (security or settings.env.get("SMTP_SECURITY") or ("ssl" if settings.smtp_port == 465 else "starttls")).lower()
    try:
        if mode == "ssl":
            smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        with smtp:
            if mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("Az SMTP bejelentkezés nem sikerült (SMTP_USER / SMTP_PASSWORD).") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"A levél küldése nem sikerült: {e}") from e
    log.info("mail.sent", subject=subject, to=rcpt)
    return rcpt
