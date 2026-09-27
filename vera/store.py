"""Thread-safe, versioned, in-memory context + conversation store."""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from .util import iso_now

SCOPES = ("category", "merchant", "customer", "trigger")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.started = time.time()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict] = {}
            # conversation_id -> state dict
            self.conversations: dict[str, dict] = {}
            # merchant-level memory
            self.sent_suppression: set[str] = set()
            self.opted_out: dict[str, str] = {}          # merchant_id -> iso time
            self.auto_reply_count: dict[str, int] = defaultdict(int)
            self.last_merchant_msg: dict[str, str] = {}
            self.backoff_until: dict[str, float] = {}     # merchant/customer key -> epoch seconds (sim)
            self.bodies_sent: dict[str, set] = defaultdict(set)  # recipient key -> bodies
            self.ack_seq = 0

    # ------------------------------------------------------------------ contexts
    def put(self, scope: str, cid: str, version: int, payload: dict):
        """Returns (status_code, response_dict)."""
        with self.lock:
            key = (scope, cid)
            cur = self.contexts.get(key)
            if cur and cur["version"] >= version:
                return 409, {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
            self.contexts[key] = {"version": version, "payload": payload}
            self.ack_seq += 1
            return 200, {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": iso_now()}

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        with self.lock:
            rec = self.contexts.get((scope, cid))
            return rec["payload"] if rec else None

    def version(self, scope: str, cid: str | None) -> int:
        if not cid:
            return 0
        with self.lock:
            rec = self.contexts.get((scope, cid))
            return rec["version"] if rec else 0

    def counts(self) -> dict:
        out = {s: 0 for s in SCOPES}
        with self.lock:
            for (scope, _cid) in self.contexts:
                out[scope] = out.get(scope, 0) + 1
        return out

    def find_trigger(self, tid: str) -> dict | None:
        trg = self.get("trigger", tid)
        if trg is not None:
            trg = dict(trg)
            trg.setdefault("id", tid)
        return trg

    def bundle(self, trigger: dict):
        """Resolve (category, merchant, customer) for a trigger."""
        mid = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
        merchant = self.get("merchant", mid)
        category = None
        if merchant:
            category = self.get("category", merchant.get("category_slug"))
        if category is None:
            slug = (trigger.get("payload") or {}).get("category")
            category = self.get("category", slug) if slug else None
        cid = trigger.get("customer_id") or (trigger.get("payload") or {}).get("customer_id")
        customer = self.get("customer", cid)
        return category, merchant, customer

    def versions_key(self, trigger: dict) -> str:
        mid = trigger.get("merchant_id")
        merchant = self.get("merchant", mid) or {}
        slug = merchant.get("category_slug")
        return "|".join(str(x) for x in (
            trigger.get("id"), self.version("trigger", trigger.get("id")),
            mid, self.version("merchant", mid),
            slug, self.version("category", slug),
            trigger.get("customer_id"), self.version("customer", trigger.get("customer_id")),
        ))


STORE = Store()
