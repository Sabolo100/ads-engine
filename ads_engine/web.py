"""Minimális állapot-végpont a konténernek (Docker HEALTHCHECK, külső figyelő): GET /healthz. Titkot és üzleti adatot nem ad ki.

· /healthz           200, ha az ütemező-ciklus él (az első tickig is); 503, ha a ciklus 10 perce nem lépett
· /healthz?strict=1  ráadásul 503, ha éles kampány mellett 36 órája nem volt sikeres napi szinkron (külső figyelőnek: Uptime Kuma stb.)
A Docker-ellenőrzés a nem szigorú változatot használja: egy tartósan hibázó Google-kapcsolat miatt a konténer ne induljon újra folyton
(az újraindítás nem javít rajta; a hibáról a motor levelet küld).
"""
import datetime as dt
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import label, version

LOOP_STALE_S = 600
SYNC_STALE_H = 36


class HealthState:
    def __init__(self):
        self.started = time.time()
        self.last_tick = None
        self.last_error = ""

    def touch(self, error=""):
        self.last_tick, self.last_error = time.time(), error


def snapshot(state, store, settings, now=None):
    """(HTTP-állapot, törzs) – a szigorú ellenőrzés külön mezőben."""
    now = now or time.time()
    loop_age = (now - state.last_tick) if state.last_tick else None
    loop_ok = (loop_age is not None and loop_age < LOOP_STALE_S) or (loop_age is None and now - state.started < LOOP_STALE_S)
    body = {"ok": loop_ok, "version": version.version(), "build": version.build_id(), "mode": settings.mode, "label": label(),
            "loop_age_s": None if loop_age is None else int(loop_age), "last_error": state.last_error, "projects": {}}
    strict_ok = True
    for slug, p in settings.projects.items():
        live = store.get(f"{slug}.approved_daily_micros") is not None
        ts = store.get(f"{slug}.last_sync_ts")
        age_h = None
        if ts:
            age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ts)).total_seconds() / 3600
        stale = live and (age_h is None or age_h > SYNC_STALE_H)
        strict_ok = strict_ok and not stale
        body["projects"][slug] = {"live": live, "last_sync_age_h": None if age_h is None else round(age_h, 1), "sync_stale": bool(stale)}
    body["strict_ok"] = strict_ok
    return body


def make_handler(state, store, settings):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, status, payload, ctype="application/json"):
            data = (json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/healthz":
                body = snapshot(state, store, settings)
                ok = body["ok"] and (body["strict_ok"] if q.get("strict") == ["1"] else True)
                return self.reply(200 if ok else 503, body)
            if u.path == "/":
                return self.reply(200, f"ads-engine {label()}\n", "text/plain")
            return self.reply(404, {"error": "nincs ilyen út"})

    return H


def start(state, store, settings, port=8080, host="0.0.0.0"):
    server = ThreadingHTTPServer((host, port), make_handler(state, store, settings))
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.2), daemon=True).start()
    return server
