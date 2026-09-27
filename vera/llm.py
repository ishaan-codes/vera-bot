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
        self.model_idx = 0
        self.thinking_ok = True
        self.lock = threading.Lock()
        self.fail_streak = 0
        self.open_until = 0.0
        self.stats = {"calls": 0, "ok": 0, "fail": 0, "last_error": None}

    # ------------------------------------------------------------------ status
    @property
    def model(self) -> str:
        return self.models[min(self.model_idx, len(self.models) - 1)] if self.models else "none"

    def available(self) -> bool:
        return self.enabled and time.time() >= self.open_until

    def describe(self) -> str:
        return f"{self.model}" if self.enabled else "no LLM"

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
            detail = e.read().decode(errors="replace")[:400]
            raise LLMError(f"HTTP {e.code}: {detail}") from None
        except Exception as e:  # timeouts, DNS, proxy
            raise LLMError(f"{type(e).__name__}: {e}") from None

    def generate(self, system: str, prompt: str, timeout: float = 9.0, json_mode: bool = True,
                 max_tokens: int = 900) -> str:
        if not self.available():
            raise LLMError("llm unavailable")
        gen = {"temperature": 0, "topP": 1, "topK": 1, "seed": 7, "maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if self.thinking_ok:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": gen}
        deadline = time.time() + timeout
        modes = [self.mode] if self.mode in ("studio", "vertex") else ["studio", "vertex"]
        last = None
        self.stats["calls"] += 1
        for _attempt in range(4):
            remaining = deadline - time.time()
            if remaining < 1.0:
                break
            mode = modes[0]
            try:
                data = self._post(mode, self.model, body, remaining)
                text = _extract_text(data)
                with self.lock:
                    self.fail_streak = 0
                    if self.mode == "auto":
                        self.mode = mode
                self.stats["ok"] += 1
                return text
            except LLMError as e:
                last = str(e)
                low = last.lower()
                if "thinking" in low and self.thinking_ok:
                    self.thinking_ok = False
                    gen.pop("thinkingConfig", None)
                    continue
                if ("http 404" in low or "not found" in low) and self.model_idx < len(self.models) - 1:
                    self.model_idx += 1
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
