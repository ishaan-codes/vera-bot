"""/v1/tick: decide which triggers deserve a message right now, compose them within budget."""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait

from .compose import compose_ctx
from .facts import Ctx
from .store import STORE
from .templates import build_draft
from .util import parse_dt, utcnow

TICK_BUDGET = float(os.environ.get("TICK_BUDGET_SECONDS", "11"))
MAX_ACTIONS = 20
PER_MERCHANT = int(os.environ.get("MAX_PER_MERCHANT_PER_TICK", "2"))
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("LLM_CONCURRENCY", "6")))


def _short(mid: str) -> str:
    m = re.match(r"(m_\d+_[a-z0-9]+)", mid or "")
    return m.group(1) if m else (mid or "m")


def conversation_id(trg: dict) -> str:
    tid = trg.get("id", "")
    num = re.search(r"trg_(\d+)", tid)
    suffix = num.group(1) if num else re.sub(r"[^a-z0-9]+", "", tid.lower())[-10:]
    who = trg.get("customer_id")
    base = f"conv_{_short(trg.get('merchant_id', ''))}_{trg.get('kind', 'msg')}"
    if who:
        c = re.match(r"(c_\d+_[a-z]+)", who)
        base += "_" + (c.group(1) if c else who)
    base += f"_{suffix}"
    cid = base
    i = 2
    while cid in STORE.conversations:
        cid = f"{base}_{i}"
        i += 1
    return cid


def _eligible(trg: dict, now_epoch: float) -> tuple[bool, str]:
    mid = trg.get("merchant_id")
    if not mid or STORE.get("merchant", mid) is None:
        return False, "unknown merchant"
    sk = trg.get("suppression_key")
    if sk and sk in STORE.sent_suppression:
        return False, "already sent (suppression key)"
    if mid in STORE.opted_out:
        return False, "merchant opted out"
    is_customer = bool(trg.get("customer_id")) or trg.get("scope") == "customer"
    if not is_customer and STORE.backoff_until.get(mid, 0) > now_epoch:
        return False, "merchant backoff"
    return True, ""


def tick(now_iso: str | None, available: list[str]) -> dict:
    t0 = time.time()
    now = parse_dt(now_iso) or utcnow()
    now_epoch = now.timestamp()
    cands = []
    for tid in available or []:
        trg = STORE.find_trigger(tid)
        if not trg:
            continue
        ok, _why = _eligible(trg, now_epoch)
        if ok:
            cands.append(trg)
    # one merchant-facing message per merchant per tick (highest urgency); customer-facing one per customer
    cands.sort(key=lambda t: (-int(t.get("urgency") or 0), available.index(t["id"]) if t.get("id") in available else 0))
    chosen, per_m, seen_c = [], {}, set()
    for t in cands:
        to_customer = (bool(t.get("customer_id")) or t.get("scope") == "customer") and \
            STORE.get("customer", t.get("customer_id")) is not None
        if to_customer:
            k = t.get("customer_id")
            if k in seen_c:
                continue
            seen_c.add(k)
        else:
            mid = t["merchant_id"]
            if per_m.get(mid, 0) >= PER_MERCHANT:
                continue      # deferred: still in available_triggers next tick if the judge keeps it active
            per_m[mid] = per_m.get(mid, 0) + 1
        chosen.append(t)
        if len(chosen) >= MAX_ACTIONS:
            break

    jobs = []
    for t in chosen:
        cat, mer, cus = STORE.bundle(t)
        ctx = Ctx(cat, mer, t, cus, now)
        base = build_draft(Ctx(cat, mer, t, cus, now))      # instant, deterministic fallback
        vk = STORE.versions_key(t)
        fut = POOL.submit(compose_ctx, ctx, True, max(2.0, TICK_BUDGET - 1.5), vk)
        jobs.append((t, base, fut))
    remaining = TICK_BUDGET - (time.time() - t0)
    if jobs:
        wait([j[2] for j in jobs], timeout=max(0.5, remaining))

    actions = []
    for t, base, fut in jobs:
        d = fut.result() if fut.done() and not fut.exception() else base
        if d.skip or not d.body:
            continue
        mid, cid = t["merchant_id"], t.get("customer_id")
        conv = conversation_id(t)
        recipient = cid or mid
        if d.body in STORE.bodies_sent[recipient]:
            continue   # never send the same body twice to the same person
        sk = t.get("suppression_key") or f"{t.get('kind')}:{mid}:{t['id']}"
        action = {
            "conversation_id": conv,
            "merchant_id": mid,
            "customer_id": cid,
            "send_as": d.send_as,
            "trigger_id": t["id"],
            "template_name": d.template_name,
            "template_params": [str(x) for x in d.template_params],
            "body": d.body,
            "cta": d.cta,
            "suppression_key": sk,
            "rationale": d.rationale,
        }
        with STORE.lock:
            STORE.sent_suppression.add(sk)
            STORE.bodies_sent[recipient].add(d.body)
            STORE.conversations[conv] = {
                "merchant_id": mid, "customer_id": cid, "trigger_id": t["id"], "kind": t.get("kind"),
                "send_as": d.send_as, "next_action": d.next_action, "anchor": d.anchor,
                "sent": [d.body], "turns": [{"from": "bot", "body": d.body}], "status": "open",
                "auto_replies": 0, "created": now.isoformat(),
            }
        actions.append(action)
    return {"actions": actions}


def prewarm(trigger_id: str, delivered_at: str | None):
    """Compose in the background when a trigger arrives, so /v1/tick is instant."""
    trg = STORE.find_trigger(trigger_id)
    if not trg:
        return
    cat, mer, cus = STORE.bundle(trg)
    if not mer:
        return
    now = parse_dt(delivered_at) or utcnow()
    POOL.submit(compose_ctx, Ctx(cat, mer, trg, cus, now), True, 12.0, STORE.versions_key(trg))
