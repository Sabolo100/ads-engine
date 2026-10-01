"""Álszerver az OpenAI képgeneráláshoz (POST /v1/images/generations) – a valódi szolgáltatáshoz nem ér.

Ellenőrzi, amit a gpt-image-2 is: kulcs, modell, méret (leghosszabb él ≤ 3840, ≤ ~4,2 megapixel), kötelező prompt. A kép egy a prompt
alapján színezett, szintetikus PNG a kért méretben (a próbák ettől függetlenül vizsgálhatók). A hibákat a `fail` sor adja.
"""
import base64
import hashlib
import io
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw


def synthetic_png(size, seed="x", text=None):
    w, h = size
    d = hashlib.sha256(seed.encode("utf-8")).digest()
    img = Image.new("RGB", (w, h), (d[0], d[1], d[2]))
    draw = ImageDraw.Draw(img)
    draw.ellipse((w * 0.25, h * 0.2, w * 0.75, h * 0.8), fill=(d[3], d[4], d[5]))
    if text:
        draw.text((w // 10, h // 10), text, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class MockOpenAI:
    def __init__(self, api_key="sk-openai-test-key-123456"):
        self.api_key = api_key
        self.requests = []          # a kérések törzse
        self.fail = []              # sorban: (állapot, törzs) – a következő hívások ezeket kapják
        self.delay = 0
        self.server = None
        self.port = 0

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
                status, payload = outer.handle(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("x-request-id", "req_" + uuid.uuid4().hex[:12])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @staticmethod
    def error(status, code, message):
        return status, {"error": {"message": message, "type": "invalid_request_error", "code": code}}

    def handle(self, path, headers, body):
        if path != "/v1/images/generations":
            return self.error(404, "not_found", "nincs ilyen út")
        if headers.get("authorization") != f"Bearer {self.api_key}":
            return self.error(401, "invalid_api_key", "Incorrect API key provided")
        self.requests.append(body)
        if self.fail:
            return self.fail.pop(0)
        if body.get("model") != "gpt-image-2":
            return self.error(400, "model_not_found", "ismeretlen modell")
        if not body.get("prompt"):
            return self.error(400, "missing_required_parameter", "prompt kötelező")
        try:
            w, h = (int(x) for x in str(body.get("size", "")).split("x"))
        except ValueError:
            return self.error(400, "invalid_value", "érvénytelen méret")
        if max(w, h) > 3840 or w * h > 4_200_000 or max(w, h) / min(w, h) > 3:
            return self.error(400, "invalid_value", f"a méret ({w}x{h}) túl nagy")
        png = synthetic_png((w, h), seed=body["prompt"])
        return 200, {"created": 1, "data": [{"b64_json": base64.b64encode(png).decode("ascii")}], "usage": {"total_tokens": 100}}
