"""Álszerver az Umami (önhosztolt, v3.0.x) API-hoz: belépés tokennel, események listája lapozással, statisztika."""
import json
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockUmami:
    def __init__(self, username="ads-engine", password="titkos-jelszo-123", website_id="54fc0966-6e5f-4ad6-b8c3-7ea5af9642e9"):
        self.username, self.password, self.website_id = username, password, website_id
        self.tokens = set()
        self.events = []            # {id, sessionId, createdAt(ms), eventName, urlQuery, urlPath, eventType}
        self.logins = 0
        self.requests = []
        self.fail_status = None
        self.server = None
        self.port = 0

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def add_event(self, session, name, url_query="", ts=0, path="/"):
        self.events.append({"id": uuid.uuid4().hex, "websiteId": self.website_id, "sessionId": session, "createdAt": ts, "eventName": name,
                            "urlQuery": url_query, "urlPath": path, "eventType": 2 if name else 1})

    def expire_tokens(self):
        self.tokens.clear()

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
                outer.requests.append(("POST", self.path))
                if self.path == "/api/auth/login":
                    outer.logins += 1
                    if body.get("username") == outer.username and body.get("password") == outer.password:
                        tok = "umami-" + uuid.uuid4().hex[:12]
                        outer.tokens.add(tok)
                        return self.reply(200, {"token": tok, "user": {"id": "u1", "username": outer.username, "role": "view-only", "isAdmin": False}})
                    return self.reply(401, {"error": "Unauthorized"})
                return self.reply(404, {"error": "nincs"})

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                if outer.fail_status:
                    return self.reply(outer.fail_status, {"error": "hiba"})
                u = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in outer.tokens:
                    return self.reply(401, {"error": "Unauthorized"})
                parts = u.path.strip("/").split("/")          # api websites <id> [events|stats]
                if len(parts) >= 3 and parts[:2] == ["api", "websites"]:
                    if parts[2] != outer.website_id:
                        return self.reply(404, {"error": "Not found"})
                    if len(parts) == 3:
                        return self.reply(200, {"id": outer.website_id, "name": "Pacsi", "domain": "pacsit.hu"})
                    if parts[3] == "events":
                        start, end = int(q.get("startAt", 0)), int(q.get("endAt", 9 * 10 ** 15))
                        rows = [e for e in outer.events if start <= e["createdAt"] <= end and (not q.get("event") or e["eventName"] == q["event"])]
                        rows.sort(key=lambda e: -e["createdAt"])
                        page, size = int(q.get("page", 1)), int(q.get("pageSize", 20))
                        return self.reply(200, {"data": rows[(page - 1) * size: page * size], "count": len(rows), "page": page, "pageSize": size})
                    if parts[3] == "stats":
                        n = len([e for e in outer.events if not e["eventName"]])
                        return self.reply(200, {"pageviews": n, "visitors": n, "visits": n, "bounces": 0, "totaltime": 0})
                return self.reply(404, {"error": "Not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
