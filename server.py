"""Vera challenge bot — HTTP server (Python stdlib only, no dependencies).

Endpoints: GET /v1/healthz, GET /v1/metadata, POST /v1/context, POST /v1/tick,
POST /v1/reply, POST /v1/teardown.   Run:  python server.py   (PORT env, default 8080)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

from vera.llm import GEMINI            # noqa: E402  (env must be loaded first)
from vera.planner import tick, prewarm  # noqa: E402
from vera.replies import handle_reply  # noqa: E402
from vera.store import STORE, SCOPES   # noqa: E402

VERSION = "1.0.0"
SUBMITTED_AT = os.environ.get("SUBMITTED_AT", "2026-09-27T12:00:00Z")
MAX_BODY = 600 * 1024


def metadata() -> dict:
    return {
        "team_name": os.environ.get("TEAM_NAME", "Ishaan Gupta"),
        "team_members": [m.strip() for m in os.environ.get("TEAM_MEMBERS", "Ishaan Gupta").split(",")],
        "model": f"{GEMINI.describe()} + deterministic grounded templates",
        "approach": ("Rule-based planner picks one signal per merchant; fact builder derives grounded facts from the 4 contexts; "
                     "category-specific templates guarantee a valid message; Gemini rewrites for compulsion; a validator rejects any "
                     "ungrounded number/quote/URL/taboo. Rule-first reply engine for auto-replies, opt-outs, intent handoff."),
        "contact_email": os.environ.get("CONTACT_EMAIL", "ishaangupta_23me134@dtu.ac.in"),
        "version": VERSION,
        "submitted_at": SUBMITTED_AT,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "VeraBot/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("ACCESS_LOG", "1") == "1":
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------------ io
    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.rfile.read(n)
            return None, "payload too large"
        raw = self.rfile.read(n) if n else b""
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return None, f"invalid json: {e}"
        if not isinstance(obj, dict):
            return None, "body must be a JSON object"
        return obj, None

    # ------------------------------------------------------------------ routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/healthz", "/healthz", ""):
            return self._json(200, {"status": "ok", "uptime_seconds": int(time.time() - STORE.started),
                                    "contexts_loaded": STORE.counts()})
        if path == "/v1/metadata":
            return self._json(200, metadata())
        if path == "/v1/debug/llm":
            return self._json(200, {"enabled": GEMINI.enabled, "available": GEMINI.available(), "model": GEMINI.model,
                                    "mode": GEMINI.mode, "stats": GEMINI.stats})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        body, err = self._body()
        if err:
            return self._json(400, {"accepted": False, "reason": "malformed", "details": err})
        try:
            if path == "/v1/context":
                return self._context(body)
            if path == "/v1/tick":
                avail = body.get("available_triggers") or []
                if not isinstance(avail, list):
                    avail = []
                return self._json(200, tick(body.get("now"), [str(a) for a in avail]))
            if path == "/v1/reply":
                return self._json(200, handle_reply(body))
            if path == "/v1/teardown":
                STORE.reset()
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})
        except Exception as e:  # never 500 without a JSON body
            traceback.print_exc()
            if path == "/v1/tick":
                return self._json(200, {"actions": []})
            if path == "/v1/reply":
                return self._json(200, {"action": "wait", "wait_seconds": 600, "rationale": f"internal error, backing off: {type(e).__name__}"})
            return self._json(500, {"error": type(e).__name__})

    def _context(self, b: dict):
        scope, cid, ver, payload = b.get("scope"), b.get("context_id"), b.get("version"), b.get("payload")
        if scope not in SCOPES:
            return self._json(400, {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(SCOPES)}"})
        if not isinstance(cid, str) or not cid:
            return self._json(400, {"accepted": False, "reason": "invalid_context_id", "details": "context_id required"})
        if isinstance(ver, bool) or not isinstance(ver, (int, float)):
            return self._json(400, {"accepted": False, "reason": "invalid_version", "details": "version must be an integer"})
        if not isinstance(payload, dict):
            return self._json(400, {"accepted": False, "reason": "invalid_payload", "details": "payload must be an object"})
        code, resp = STORE.put(scope, cid, int(ver), payload)
        if code == 200 and scope == "trigger":
            try:
                prewarm(cid, b.get("delivered_at"))
            except Exception:
                traceback.print_exc()
        return self._json(code, resp)


def main():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"Vera bot listening on :{port}  llm={GEMINI.describe()}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
