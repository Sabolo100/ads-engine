"""Helyi HTTP-szerver a próbákhoz: egy projekt „oldalát” utánozza (az Ads Pack /ads/ alatt), ETag-gal, átirányítással, nagy fájllal."""
import hashlib
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class PackServer:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.requests = []             # (útvonal, If-None-Match)
        self.overrides = {}            # útvonal → (állapot, fejlécek, törzs) – a próbák felülírhatják
        self.server = None
        self.port = 0

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.requests.append((self.path, self.headers.get("If-None-Match")))
                if path in outer.overrides:
                    status, headers, body = outer.overrides[path]
                    return self.reply(status, headers, body)
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                if path == "/":
                    return self.reply(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html><body>Pacsi</body></html>")
                if path == "/kviz":                  # mint az éles nginx: a lekérdezés megmarad, a # utáni rész az app állapota
                    return self.reply(302, {"Location": f"/?{query}#kviz"}, b"")
                if path == "/drops":                 # hibás átirányítás: a lekérdezés elvész
                    return self.reply(302, {"Location": "/"}, b"")
                if path == "/redirect-evil":
                    return self.reply(302, {"Location": "http://evil.example.com/x"}, b"")
                if path == "/redirect-loop":
                    return self.reply(302, {"Location": "/redirect-loop"}, b"")
                if path == "/redirect-ok":
                    return self.reply(302, {"Location": "/ads/brief.json"}, b"")
                if path == "/big":
                    return self.reply(200, {"Content-Type": "application/octet-stream"}, b"x" * 3_000_000)
                if path.startswith("/ads/"):
                    f = outer.root / path[len("/ads/"):]
                    if f.is_file():
                        body = f.read_bytes()
                        etag = '"' + hashlib.md5(body).hexdigest() + '"'
                        if self.headers.get("If-None-Match") == etag:
                            return self.reply(304, {"ETag": etag}, b"")
                        ctype = "application/json" if f.suffix == ".json" else ("image/jpeg" if f.suffix == ".jpg" else "application/octet-stream")
                        return self.reply(200, {"Content-Type": ctype, "ETag": etag}, body)
                return self.reply(404, {"Content-Type": "text/plain"}, b"nincs")

            def reply(self, status, headers, body):
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"
