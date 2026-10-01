"""JSON-soros napló (a Coolify naplónézetében olvasható), titokkitakarással.

Minden ismert titkot (API-kulcsok, jelszavak, a szolgáltatásfiók privát kulcsa) a kiírás előtt ***-ra cserél.
A titkokat a konfiguráció betöltésekor kell regisztrálni: log.add_secret(érték).
"""
import datetime as dt
import json
import os
import sys

_SECRETS = set()
_LEVELS = {"info": 0, "warn": 1, "error": 2}


def add_secret(value):
    """Titok regisztrálása. A JSON-ban kiírt (\\n-nel escape-elt) alakját és a többsoros titok soronkénti darabjait is kitakarjuk."""
    if not value or not isinstance(value, str) or len(value) < 6:
        return
    _SECRETS.add(value)
    _SECRETS.add(json.dumps(value, ensure_ascii=False)[1:-1])
    if "\n" in value:
        for line in value.splitlines():
            if len(line.strip()) >= 20:
                _SECRETS.add(line.strip())


def redact(text):
    if not isinstance(text, str):
        text = str(text)
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def _emit(level, event, **fields):
    """ADS_LOG_LEVEL=info|warn|error: a kisebb szintű sorokat elhagyja (a próbák csendben futnak)."""
    if _LEVELS[level] < _LEVELS.get(os.environ.get("ADS_LOG_LEVEL", "info"), 0):
        return
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "level": level, "event": event}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(redact(line), file=sys.stderr if level == "error" else sys.stdout, flush=True)


def info(event, **fields):
    _emit("info", event, **fields)


def warn(event, **fields):
    _emit("warn", event, **fields)


def error(event, **fields):
    _emit("error", event, **fields)
