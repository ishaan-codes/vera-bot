"""Minimal Gemini client (stdlib only).

Supports Google AI Studio keys (generativelanguage.googleapis.com) and Vertex AI
express-mode keys (aiplatform.googleapis.com), auto-detecting which one works.
A small circuit breaker keeps the bot fast if the API is slow or rate-limited:
callers always have a deterministic template to fall back to.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

DEFAULT_MODELS = "gemini-2.5-flash,gemini-flash-latest,gemini-2.0-flash"


class LLMError(Exception):
    pass


class Gemini:
    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.models = [m.strip() for m in os.environ.get("GEMINI_MODEL", DEFAULT_MODELS).split(",") if m.strip()]
        self.mode = os.environ.get("GEMINI_MODE", "auto")  # auto | studio | vertex
        self.enabled = bool(self.key) and os.environ.get("LLM_DISABLED", "") not in ("1", "true")
        self.rpm = max(1, int(os.environ.get("LLM_RPM", "12")))   # client-side budget; raise on a paid key
        self.thinking_ok = True
        self.lock = threading.Lock()
        self.fail_streak = 0
        self.open_until = 0.0
        self.dead: set[str] = set()                 # models that returned 404
        self.cooldown: dict[str, float] = {}        # model -> epoch until usable (429)
        self.sent: list[float] = []                 # request timestamps (sliding 60s window)
        self.last_model = self.models[0] if self.models else "none"
        self.stats = {"calls": 0, "ok": 0, "fail": 0, "rate_limited": 0, "budget_skips": 0, "last_error": None}

    # ------------------------------------------------------------------ status
    @property
    def model(self) -> str:
        m = self._pick()
        return m or self.last_model

    def _pick(self) -> str | None:
        now = time.time()
        for m in self.models:
            if m not in self.dead and self.cooldown.get(m, 0) <= now:
                return m
        return None

    def available(self) -> bool:
        return self.enabled and time.time() >= self.open_until and self._pick() is not None

    def describe(self) -> str:
        return f"{self.last_model}" if self.enabled else "no LLM"

    def _take_slot(self, wait: float) -> bool:
        """Sliding-window RPM limiter. Blocks up to `wait` seconds for a slot."""
        deadline = time.time() + max(0.0, wait)
        while True:
            with self.lock:
                now = time.time()
                self.sent = [t for t in self.sent if now - t < 60]
                if len(self.sent) < self.rpm:
                    self.sent.append(now)
                    return True
                nxt = self.sent[0] + 60 - now
            if time.time() + min(nxt, 0.5) > deadline:
                return False
            time.sleep(min(max(nxt, 0.05), 0.5))

    # ------------------------------------------------------------------ http
    def _url(self, mode: str, model: str) -> tuple[str, dict]:
        if mode == "vertex":
            return (f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent?key={self.key}",
                    {"Content-Type": "application/json"})
        return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                {"Content-Type": "application/json", "x-goog-api-key": self.key})

    def _post(self, mode: str, model: str, body: dict, timeout: float) -> dict:
        url, headers = self._url(mode, model)
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:3000]
            raise LLMError(f"HTTP {e.code}: {detail}") from None
        except Exception as e:  # timeouts, DNS, proxy
            raise LLMError(f"{type(e).__name__}: {e}") from None

    def generate(self, system: str, prompt: str, timeout: float = 9.0, json_mode: bool = True,
                 max_tokens: int = 900) -> str:
        if not self.available():
            raise LLMError("llm unavailable")
        deadline = time.time() + timeout
        # wait for a request slot, but leave enough time for the call itself
        if not self._take_slot(wait=max(0.0, timeout - 5.0)):
            self.stats["budget_skips"] += 1
            raise LLMError("client rate budget exhausted")
        gen = {"temperature": 0, "topP": 1, "topK": 1, "seed": 7, "maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if self.thinking_ok:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": gen}
        modes = [self.mode] if self.mode in ("studio", "vertex") else ["studio", "vertex"]
        last = None
        self.stats["calls"] += 1
        for _attempt in range(6):
            remaining = deadline - time.time()
            model = self._pick()
            if remaining < 1.0 or model is None:
                break
            mode = modes[0]
            try:
                data = self._post(mode, model, body, remaining)
                text = _extract_text(data)
                with self.lock:
                    self.fail_streak = 0
                    self.last_model = model
                    if self.mode == "auto":
                        self.mode = mode
                self.stats["ok"] += 1
                return text
            except LLMError as e:
                last = str(e)
                low = last.lower()
                if "http 429" in low or "resource_exhausted" in low:
                    daily = "perday" in low.replace(" ", "").replace("_", "") or "per day" in low
                    with self.lock:
                        self.cooldown[model] = time.time() + (1800 if daily else 60)
                    self.stats["rate_limited"] += 1
                    continue                      # next model has its own free-tier quota
                if "thinking" in low and self.thinking_ok:
                    self.thinking_ok = False
                    gen.pop("thinkingConfig", None)
                    continue
                if "http 404" in low or ("not found" in low and "model" in low):
                    self.dead.add(model)
                    continue
                if self.mode == "auto" and len(modes) > 1 and any(c in low for c in ("http 401", "http 403", "api key", "http 400")):
                    modes.pop(0)
                    continue
                break
        with self.lock:
            self.fail_streak += 1
            self.stats["fail"] += 1
            self.stats["last_error"] = (last or "timeout")[:300]
            if self.fail_streak >= 3:
                self.open_until = time.time() + 45
                self.fail_streak = 0
            if self._pick() is None and self.cooldown:
                live = [t for m, t in self.cooldown.items() if m not in self.dead]
                if live:
                    self.open_until = max(self.open_until, min(live))
        raise LLMError(last or "timeout")


def _extract_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected response: {json.dumps(data)[:300]}")


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        raise


GEMINI = Gemini()
