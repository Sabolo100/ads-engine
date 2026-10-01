"""Álszerver az Anthropic Messages API-hoz (POST /v1/messages) – a valódi szolgáltatáshoz nem ér.

Az Anthropic SDK base_url-jét erre állítjuk. Ellenőrzi, amit a Claude Sonnet 5.5 is ellenőrizne: nincs temperature/top_p/top_k,
nincs kényszerített tool_choice, nincs `thinking: disabled`, a strukturált kimenet sémájában minden objektum `additionalProperties:
false`. A válaszokat a próba adja (responder), vagy sorban a `queue`-ból veszi.
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def message(text, stop_reason="end_turn", model="claude-sonnet-5-5", input_tokens=120, output_tokens=80):
    return {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def error_body(kind, text):
    return {"type": "error", "error": {"type": kind, "message": text}}


def _objects_closed(schema):
    """Minden objektum additionalProperties: false-t kér-e (a strukturált kimenet feltétele)."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            return False
        return all(_objects_closed(v) for v in schema.values())
    if isinstance(schema, list):
        return all(_objects_closed(v) for v in schema)
    return True


class MockAnthropic:
    def __init__(self, api_key="sk-ant-test-key-123456"):
        self.api_key = api_key
        self.requests = []          # a kérések törzse
        self.queue = []             # sorban a válaszok: (állapot, törzs) vagy szöveg
        self.responder = None       # responder(törzs) → (állapot, törzs) | szöveg
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
                self.send_header("request-id", "req_" + uuid.uuid4().hex[:16])
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

    def handle(self, path, headers, body):
        if path != "/v1/messages":
            return 404, error_body("not_found_error", "nincs ilyen út")
        if headers.get("x-api-key") != self.api_key:
            return 401, error_body("authentication_error", "invalid x-api-key")
        self.requests.append(body)
        for forbidden in ("temperature", "top_p", "top_k"):
            if forbidden in body:
                return 400, error_body("invalid_request_error", f"{forbidden}: nem módosítható ezen a modellen")
        if (body.get("thinking") or {}).get("type") == "disabled":
            return 400, error_body("invalid_request_error", "thinking: disabled nem engedélyezett")
        if (body.get("tool_choice") or {}).get("type") in ("any", "tool"):
            return 400, error_body("invalid_request_error", "kényszerített tool_choice nem engedélyezett")
        fmt = (body.get("output_config") or {}).get("format")
        if fmt and not _objects_closed(fmt.get("schema", {})):
            return 400, error_body("invalid_request_error", "a séma objektumainak additionalProperties: false kell")
        if not body.get("messages"):
            return 400, error_body("invalid_request_error", "messages kötelező")
        if self.queue:
            item = self.queue.pop(0)
        elif self.responder:
            item = self.responder(body)
        else:
            return 500, error_body("api_error", "nincs beállított válasz")
        if isinstance(item, tuple):
            return item
        if isinstance(item, dict) and item.get("type") == "message":
            return 200, item
        return 200, message(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
