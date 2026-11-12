"""Fake Anthropic Messages + OpenAI chat completions server, for tests with no real key.

Serves POST /v1/messages and POST /v1/chat/completions on 127.0.0.1. The last request's
headers and parsed body are kept on the server. Set server.fail_next to an HTTP status
(e.g. 429 or 500) to answer that status once; server.retry_after adds a Retry-After header.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANTHROPIC_REPLY = {
    "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
    "usage": {"input_tokens": 12, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 3, "output_tokens": 5},
}
OPENAI_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "hi there"}}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 6, "prompt_tokens_details": {"cached_tokens": 8}},
}
PATHS = {"/v1/messages": ANTHROPIC_REPLY, "/v1/chat/completions": OPENAI_REPLY}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, extra=None):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        srv = self.server
        srv.last_headers = {k.lower(): v for k, v in self.headers.items()}
        srv.last_body = json.loads(raw) if raw else None
        srv.last_path = self.path
        reply = PATHS.get(self.path)
        if reply is None:
            self._send(404, {"error": "not found"})
            return
        if srv.fail_next is not None:
            status, srv.fail_next = srv.fail_next, None
            extra = {"Retry-After": srv.retry_after} if srv.retry_after is not None else None
            self._send(status, {"error": "fake failure"}, extra)
            return
        self._send(200, reply)


class FakeApi(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fail_next = None
        self.retry_after = None
        self.last_headers = {}
        self.last_body = None
        self.last_path = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


def start():
    srv = FakeApi()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
