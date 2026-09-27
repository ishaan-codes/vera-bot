"""compose(category, merchant, trigger, customer?) -> message.

1. Deterministic template (always available, always grounded).
2. Optional Gemini rewrite that sees the same facts + the template as a baseline.
3. Validator: the rewrite is used only if it adds no ungrounded numbers, quotes,
   URLs, taboo words, preambles or extra CTAs. Otherwise the template ships.
Results are cached per (context versions, day) so repeated calls are identical.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from datetime import datetime

from .facts import Ctx
from .llm import GEMINI, LLMError, parse_json
from .templates import Draft, build_draft
from .util import clean_ws
from .validate import check

_CACHE: dict[str, Draft] = {}
_CACHE_LOCK = threading.Lock()

VOICE_NOTES = {
    "dentists": "clinical peer-to-peer (colleague, not salesperson); technical terms welcome; cite sources; no hype, no guarantees",
    "salons": "warm, practical, fellow-professional; concrete service + price",
    "restaurants": "fellow operator, busy and practical; covers, AOV, delivery vs dine-in",
    "gyms": "coach energy, disciplined, evidence-based; no body-shaming, no quick-fix promises",
    "pharmacies": "trustworthy, precise, calm; molecule names and dates exact; never alarmist",
}

SYSTEM = """You write ONE WhatsApp message for magicpin's merchant assistant "Vera".
You receive the full CONTEXT (category, merchant, trigger, optional customer) and a BASELINE message that is already correct and grounded.
Your job: produce a sharper version that a busy Indian merchant (or their customer) would actually reply to.

Hard rules (violations are rejected automatically):
- Use ONLY facts present in CONTEXT or BASELINE. Every number, price, date, name, source, quote and offer must appear there. Never invent research, competitors, customer counts, prices, slots or statistics.
- No URLs. No "I hope you're well" / self-introductions. No hype words or the category's taboo words.
- Exactly one call-to-action, as the LAST sentence, same type as the BASELINE CTA (e.g. ends with "Reply YES." / "Reply CONFIRM" / a slot choice / an open question).
- Start with the recipient's name/salutation exactly as in BASELINE, then immediately WHY NOW (the trigger).
- Choose the ONE most compelling signal; do not list every metric. 2-4 short sentences, ideally under 450 characters.
- Keep the BASELINE's language register: if it uses Hindi-English code-mix (Roman script), keep a natural Hinglish mix; otherwise English.
- Use levers that fit: specificity (a verifiable number/source), loss aversion, social proof from the data, effort externalisation ("I'll draft it"), curiosity. Low-friction ask.
- Do not copy the baseline word-for-word; improve flow and compulsion while keeping every fact correct.

Return JSON only: {"body": "<message>", "rationale": "<one sentence: which signal you chose and why it should get a reply>"}"""


def _trim_category(cat: dict, trigger: dict) -> dict:
    c = {k: v for k, v in cat.items() if k not in ("patient_content_library",)}
    return c


def build_prompt(c: Ctx, d: Draft) -> str:
    ctx = {
        "category": _trim_category(c.category, c.trigger),
        "merchant": c.merchant,
        "trigger": c.trigger,
        "customer": c.customer,
        "today": c.now.strftime("%Y-%m-%d"),
    }
    who = "the merchant's customer (sent from the merchant's number)" if d.send_as == "merchant_on_behalf" else "the merchant (sent as Vera)"
    return (f"RECIPIENT: {who}\nCATEGORY VOICE: {VOICE_NOTES.get(c.slug, 'peer, practical')}\n"
            f"CTA TYPE: {d.cta}\nWHAT 'YES' COMMITS VERA TO: {d.next_action or 'n/a'}\n\n"
            f"BASELINE:\n{d.body}\n\nCONTEXT:\n{json.dumps(ctx, ensure_ascii=False, separators=(',', ':'))}")


def cache_key(c: Ctx, versions_key: str) -> str:
    return f"{versions_key}|{c.now.strftime('%Y-%m-%d')}"


_INFLIGHT: dict[str, threading.Event] = {}


def compose_ctx(c: Ctx, use_llm: bool = True, timeout: float = 9.0, versions_key: str | None = None) -> Draft:
    key = cache_key(c, versions_key) if versions_key else None
    if key:
        # if the same message is already being composed (prewarm vs tick), wait for it instead of a 2nd LLM call
        with _CACHE_LOCK:
            ev = _INFLIGHT.get(key)
            mine = ev is None
            if mine:
                _INFLIGHT[key] = threading.Event()
        if not mine:
            ev.wait(timeout)
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
            if hit is not None:
                return copy.deepcopy(hit)
            return build_draft(c)
        try:
            return _compose_ctx(c, use_llm, timeout, key)
        finally:
            with _CACHE_LOCK:
                _INFLIGHT.pop(key).set()
    return _compose_ctx(c, use_llm, timeout, None)


def _compose_ctx(c: Ctx, use_llm: bool, timeout: float, key: str | None) -> Draft:
    if key:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None and (hit.template_name.endswith("+llm") or not use_llm or not GEMINI.available()):
            return copy.deepcopy(hit)
    d = build_draft(c)
    if d.skip:
        return d
    base_issues = check(d.body, c, baseline=d.body, send_as=d.send_as, cta=d.cta)
    if use_llm and GEMINI.available():
        try:
            raw = GEMINI.generate(SYSTEM, build_prompt(c, d), timeout=timeout)
            out = parse_json(raw)
            body = clean_ws(str(out.get("body", "")))
            issues = check(body, c, baseline=d.body, send_as=d.send_as, cta=d.cta)
            if not issues:
                d.body = body
                if out.get("rationale"):
                    d.rationale = f"{str(out['rationale']).strip()} [{d.rationale}]"
                d.template_name = (d.template_name or "vera") + "+llm"
            else:
                _log(f"llm draft rejected for {c.trigger.get('id')}: {issues[:3]}")
        except (LLMError, ValueError, TypeError) as e:
            _log(f"llm unavailable for {c.trigger.get('id')}: {str(e)[:120]}")
    if base_issues and not d.template_name.endswith("+llm"):
        _log(f"template self-check {c.trigger.get('id')}: {base_issues[:2]}")
    if key:
        with _CACHE_LOCK:
            _CACHE[key] = copy.deepcopy(d)
    return d


def _log(msg: str):
    sys.stderr.write(f"[compose] {msg}\n")


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
            now: datetime | None = None, use_llm: bool = True) -> dict:
    """Challenge-brief §7.1 signature."""
    c = Ctx(category, merchant, trigger, customer, now)
    d = compose_ctx(c, use_llm=use_llm)
    return {
        "body": d.body,
        "cta": d.cta,
        "send_as": d.send_as,
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('kind')}:{(merchant or {}).get('merchant_id')}:{trigger.get('id')}",
        "rationale": d.rationale,
    }
