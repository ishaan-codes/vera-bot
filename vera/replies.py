"""/v1/reply: rules decide the move (send / wait / end); templates or Gemini write the words."""
from __future__ import annotations

import json
import re

from .compose import VOICE_NOTES
from .facts import Ctx
from .llm import GEMINI, LLMError, parse_json
from .store import STORE
from .util import is_hinglish, norm_msg, parse_dt, utcnow, clean_ws, first_name
from .validate import check

# ---------------------------------------------------------------------- classifiers

AUTO_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)", r"our team will (get back|respond|contact|reach)",
    r"will (get back|respond|revert) (to you )?(shortly|soon|asap)", r"we('ll| will) get back", r"automated (assistant|reply|message)",
    r"auto[- ]?reply", r"out of (the )?office", r"currently (unavailable|away|closed)", r"business hours",
    r"aapki jaankari ke liye", r"team tak pahuncha", r"hum jald hi", r"sampark karne ke liye dhanyavaad",
    r"this is an automated", r"do not reply", r"we are closed", r"visit us at",
]
OPTOUT_PATTERNS = [
    r"\bstop\b", r"unsubscribe", r"not interested", r"don'?t (message|text|contact|send)", r"do not (message|text|contact)",
    r"stop (messaging|sending|texting)", r"\bspam\b", r"leave me alone", r"remove (me|my number)", r"block",
    r"band karo", r"mat bhejo", r"message mat", r"nahi chahiye", r"interest nahi", r"pareshan mat",
]
HOSTILE_PATTERNS = [r"\buseless\b", r"\bstupid\b", r"\bidiot", r"\bbakwas\b", r"\bfraud\b", r"\bscam\b", r"\bnonsense\b",
                    r"\bwaste of time\b", r"\bbothering\b", r"\bannoying\b", r"\bf+u+c*k", r"\bshut up\b", r"\bpathetic\b"]
COMMIT_PATTERNS = [
    r"\blet'?s do it\b", r"\blets do it\b", r"\bgo ahead\b", r"\bdo it\b", r"\bproceed\b", r"\bconfirm(ed)?\b",
    r"^\s*(yes|yes please|yep|yeah|yup|sure|ok|okay|ok sure|haan|haa|ha ji|haan ji|ji haan|done|chalo|theek hai|thik hai)\b",
    r"\bkar do\b", r"\bkardo\b", r"\bkar dijiye\b", r"\bkaro\b", r"\bshuru karo\b", r"\bi want to (join|start|do)",
    r"\bjudna hai\b", r"\bsign me up\b", r"\bsounds good\b", r"\bplease (do|send|go)\b", r"\bsend (it|me)\b", r"\bbook it\b",
    r"^\s*[12]\s*$", r"\bwhat'?s next\b", r"\bwhats next\b",
]
LATER_PATTERNS = [r"\blater\b", r"\bbusy\b", r"\bnot now\b", r"\bbaad mein\b", r"\babhi nahi\b", r"\bkal\b", r"\btomorrow\b",
                  r"\bin a meeting\b", r"\bcall you back\b", r"\bnext week\b", r"\bafter (diwali|lunch|\d)"]
DECLINE_PATTERNS = [r"^\s*(no|nope|nah|nahi|nahin|na)\s*[.!]*\s*$", r"\bno thanks\b", r"\bno thank you\b", r"\bnot needed\b",
                    r"\bzarurat nahi\b", r"\bnot required\b"]
OFFTOPIC_PATTERNS = [r"\bgst\b", r"\bincome tax\b", r"\bitr\b", r"\btax (return|filing)\b", r"\bloan\b", r"\bvisa\b",
                     r"\bpassport\b", r"\bcricket score\b", r"\belectricity bill\b", r"\baadhaar\b", r"\bpan card\b",
                     r"\blegal notice\b", r"\blandlord\b", r"\bstock (market|tips)\b", r"\bcrypto\b", r"\bmy (son|daughter)'?s (homework|exam)\b"]


def _any(patterns, text) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def classify(msg: str, prev_msgs: list[str]) -> str:
    t = (msg or "").strip()
    low = t.lower()
    n = norm_msg(t)
    if not n:
        return "empty"
    if _any(AUTO_PATTERNS, low) or (prev_msgs and n and prev_msgs.count(n) >= 1 and len(n) > 25):
        return "auto_reply"
    if _any(OPTOUT_PATTERNS, low):
        return "opt_out"
    if _any(HOSTILE_PATTERNS, low):
        return "hostile"
    if _any(OFFTOPIC_PATTERNS, low):
        return "off_topic"
    if _any(COMMIT_PATTERNS, low):
        return "commit"
    if _any(DECLINE_PATTERNS, low):
        return "decline"
    if _any(LATER_PATTERNS, low):
        return "later"
    if "?" in t or re.match(r"^(what|how|why|when|which|who|kya|kaise|kitna|kab|kyun|can|is|are|does|will)\b", low):
        return "question"
    return "engaged"


# ---------------------------------------------------------------------- state helpers

def _conv(conversation_id: str, merchant_id: str | None, customer_id: str | None, from_role: str) -> dict:
    with STORE.lock:
        st = STORE.conversations.get(conversation_id)
        if st is None:
            st = {"merchant_id": merchant_id, "customer_id": customer_id, "trigger_id": None, "kind": None,
                  "send_as": "merchant_on_behalf" if from_role == "customer" else "vera",
                  "next_action": None, "anchor": None, "sent": [], "turns": [], "status": "open",
                  "auto_replies": 0, "unknown": True}
            STORE.conversations[conversation_id] = st
        if merchant_id and not st.get("merchant_id"):
            st["merchant_id"] = merchant_id
        return st


def _ctx_for(st: dict, now) -> Ctx:
    mer = STORE.get("merchant", st.get("merchant_id")) or {}
    cat = STORE.get("category", mer.get("category_slug")) or {}
    trg = STORE.find_trigger(st.get("trigger_id")) if st.get("trigger_id") else None
    cus = STORE.get("customer", st.get("customer_id")) if st.get("customer_id") else None
    return Ctx(cat, mer, trg or {"kind": st.get("kind") or "conversation", "payload": {}}, cus, now)


def _default_next_action(c: Ctx) -> str:
    hist = c.merchant.get("conversation_history") or []
    for h in reversed(hist):
        if h.get("from") == "merchant" and h.get("engagement") in ("intent_action", "merchant_replied") and h.get("body"):
            return f"act on your last request (\"{h['body']}\")"
    diag, act_en, _hi, nxt = c.fix_lever()
    return nxt


# ---------------------------------------------------------------------- reply templates

def _t_commit(c: Ctx, st: dict, hi: bool) -> str:
    nxt = st.get("next_action") or _default_next_action(c)
    art = _artifact(c, st)
    if st.get("send_as") == "merchant_on_behalf":
        return ("Done ✅ aapka slot confirm ho gaya. Koi badlav ho to yahin bata dijiye." if hi
                else "Done ✅ you're confirmed. If anything changes, just reply here.")
    if hi:
        return clean_ws(f"Done — {nxt} pe kaam shuru. {art} Next step: reply CONFIRM aur main ise live kar dungi.")
    return clean_ws(f"Done — I'm on it: {nxt}. {art} Next step: reply CONFIRM and I'll put it live.")


def _artifact(c: Ctx, st: dict) -> str:
    """A small, concrete, grounded draft so the merchant sees immediate progress."""
    kind = st.get("kind") or ""
    off = c.best_offer() or c.catalog_offer()
    loc = c.locality
    if kind in ("research_digest",) and c.trigger:
        item = c.digest_item((c.tp or {}).get("top_item_id")) or c.digest_of(("research",))
        if item:
            return f"Here's the patient note draft: \"{item.get('title')} — ask us at your next visit whether this applies to you.\""
    if kind in ("supply_alert",):
        return "Here's the customer note draft: \"A batch of one of your medicines is being replaced by the manufacturer — please bring your strip in and we'll swap it free.\""
    if off:
        return f"Here's the draft post: \"{off} at {c.mname}{', ' + loc if loc else ''} — call or walk in to book.\""
    return f"Draft ready for {c.mname}."


def _t_offtopic(c: Ctx, st: dict, hi: bool) -> str:
    back = st.get("next_action") or _default_next_action(c)
    if hi:
        return f"Yeh mere scope se bahar hai — iske liye aapke CA/expert best rahenge. Wapas apne kaam pe: {back} — kar doon? Reply YES."
    return f"That's outside what I can help with — your CA or a specialist is the right person for it. Back to {c.mname}: shall I {back}? Reply YES."


def _t_question(c: Ctx, st: dict, msg: str, hi: bool) -> str:
    anchor = st.get("anchor") or ""
    nxt = st.get("next_action") or _default_next_action(c)
    low = msg.lower()
    if re.search(r"\b(price|cost|charge|fee|kitna|paisa|kitne)\b", low):
        plan, amt = c.sub.get("plan"), (c.tp or {}).get("renewal_amount")
        off = c.best_offer()
        if amt:
            return f"The {plan} plan renewal is ₹{amt:,}. Shall I go ahead and {nxt}? Reply YES."
        if off:
            return f"Your live offer is \"{off}\" — no extra cost from my side to {nxt}. Shall I go ahead? Reply YES."
    base = f"Good question. The short version: {anchor}." if anchor else "Good question."
    if hi:
        return f"{base} Sabse aasaan next step: main {nxt}. Kar doon? Reply YES."
    return f"{base} Easiest next step is for me to {nxt}. Shall I go ahead? Reply YES."


def _t_engaged(c: Ctx, st: dict, hi: bool) -> str:
    nxt = st.get("next_action") or _default_next_action(c)
    if hi:
        return f"Samajh gayi, thanks {c.owner or ''}! Main {nxt} ready rakhti hoon — bas YES bol dijiye."
    return f"Got it, thanks{' ' + c.owner if c.owner else ''}. I can {nxt} right away — just reply YES."


REPLY_SYSTEM = """You are Vera, magicpin's WhatsApp assistant for merchants, continuing a live conversation.
Write the next message only. Rules:
- The decided MOVE is fixed; write words for it. For COMMIT: switch to action immediately — say what you are doing now and show a short concrete draft; never ask qualifying questions (no "would you", "do you", "what if", "how about", "can you tell").
- Use only facts from CONTEXT/CONVERSATION. No invented numbers, prices, dates, names, links.
- Match the merchant's language in their last message (Hinglish in Roman script if they wrote Hinglish, else English).
- No re-introduction, no preamble. 1-3 short sentences (a draft block may add a few lines). End with one clear low-effort next step.
- Stay on mission (merchant growth on magicpin/Google). Politely decline unrelated requests and steer back.
Return JSON only: {"body": "...", "rationale": "..."}"""


def _llm_reply(c: Ctx, st: dict, move: str, msg: str, fallback: str, hi: bool) -> tuple[str, str | None]:
    if not GEMINI.available():
        return fallback, None
    convo = st.get("turns", [])[-8:]
    prompt = (f"MOVE: {move}\nWHAT VERA OFFERED TO DO: {st.get('next_action') or _default_next_action(c)}\n"
              f"LANGUAGE: {'Hinglish' if hi else 'English'}\nCATEGORY VOICE: {VOICE_NOTES.get(c.slug, 'peer, practical')}\n"
              f"FALLBACK (acceptable answer you should improve on): {fallback}\n\n"
              f"CONVERSATION (oldest first): {json.dumps(convo, ensure_ascii=False)}\n"
              f"MERCHANT'S LATEST MESSAGE: {msg}\n\n"
              f"CONTEXT: {json.dumps({'merchant': c.merchant, 'trigger': c.trigger, 'customer': c.customer, 'category': {k: v for k, v in c.category.items() if k != 'patient_content_library'}}, ensure_ascii=False, separators=(',', ':'))}")
    try:
        out = parse_json(GEMINI.generate(REPLY_SYSTEM, prompt, timeout=8.0))
        body = clean_ws(str(out.get("body", "")))
        issues = check(body, c, baseline=fallback + " " + " ".join(t.get("body", "") for t in convo),
                       send_as=st.get("send_as", "vera"), cta="multi_choice_slot")
        issues = [i for i in issues if i != "missing recipient name"]
        if move == "commit" and re.search(r"would you|do you|can you tell|what if|how about", body, re.I):
            issues.append("qualifying question after commitment")
        if issues:
            return fallback, None
        return body, str(out.get("rationale") or "")
    except (LLMError, ValueError, TypeError):
        return fallback, None


# ---------------------------------------------------------------------- main entry

def handle_reply(req: dict) -> dict:
    conv_id = str(req.get("conversation_id") or "conv_unknown")
    mid, cid = req.get("merchant_id"), req.get("customer_id")
    role = str(req.get("from_role") or "merchant")
    msg = str(req.get("message") or "")
    now = parse_dt(req.get("received_at")) or utcnow()
    st = _conv(conv_id, mid, cid, role)
    mid = mid or st.get("merchant_id")
    who_key = (cid or st.get("customer_id")) if role == "customer" else mid
    who_key = who_key or conv_id

    prev = [norm_msg(t["body"]) for t in st["turns"] if t.get("from") != "bot"]
    prev_global = STORE.last_merchant_msg.get(who_key)
    if prev_global:
        prev = prev + [prev_global]
    st["turns"].append({"from": role, "body": msg})
    kind = classify(msg, prev)
    hi = is_hinglish(msg)
    c = _ctx_for(st, now)

    # ---- auto-replies: counted per sender across conversations
    if kind == "auto_reply":
        with STORE.lock:
            STORE.auto_reply_count[who_key] += 1
            n = STORE.auto_reply_count[who_key]
            STORE.last_merchant_msg[who_key] = norm_msg(msg)
        if n >= 3:
            st["status"] = "ended"
            return {"action": "end", "rationale": f"Same canned auto-reply {n}x in a row — no human on the line; closing without spending more turns."}
        if n == 2:
            STORE.backoff_until[who_key] = now.timestamp() + 86400
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Auto-reply again — owner isn't at the phone. Backing off 24h instead of burning turns."}
        nxt = st.get("next_action") or _default_next_action(c)
        body = (f"Lagta hai yeh auto-reply hai 🙂 Jab owner dekhein, bas YES reply kar dein — main {nxt}."
                if hi else
                f"Looks like an auto-reply 🙂 When the owner sees this, a simple YES is all I need to {nxt}.")
        return _send(st, body, "binary_yes_no", "Detected WhatsApp Business auto-reply; one short flag for the owner, no repeat pitch.")

    with STORE.lock:
        STORE.auto_reply_count[who_key] = 0
        STORE.last_merchant_msg[who_key] = norm_msg(msg)

    if kind == "empty":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Empty message; waiting for a real reply."}

    if kind == "opt_out":
        st["status"] = "ended"
        with STORE.lock:
            STORE.opted_out[who_key] = now.isoformat()
        return {"action": "end", "rationale": "Explicit opt-out/stop request — ending and suppressing further proactive sends to this recipient."}

    if kind == "hostile":
        st["status"] = "paused"
        STORE.backoff_until[who_key] = now.timestamp() + 7 * 86400
        body = ("Maaf kijiye, yeh aapko bekaar laga. Main messages rok rahi hoon — jab chahein 'Hi Vera' likh dijiye. 🙏" if hi else
                "Sorry this felt like noise — I'll pause messages. If you ever want help with your listing, just say 'Hi Vera'. 🙏")
        return _send(st, body, "none", "Merchant frustrated; one-line apology + open door, pausing proactive sends for 7 days.")

    if kind == "later":
        secs = 86400 if re.search(r"\b(kal|tomorrow|next week)\b", msg, re.I) else 3 * 3600
        STORE.backoff_until[who_key] = now.timestamp() + secs
        return {"action": "wait", "wait_seconds": secs, "rationale": "Merchant asked for time; backing off instead of pushing."}

    if kind == "decline":
        st["status"] = "ended"
        return {"action": "end", "rationale": "Merchant declined this offer; closing gracefully without a second pitch."}

    if st.get("status") in ("ended",) and kind not in ("commit", "question", "engaged", "off_topic"):
        return {"action": "end", "rationale": "Conversation already closed."}
    st["status"] = "open"

    if kind == "commit":
        fb = _t_commit(c, st, hi)
        body, why = _llm_reply(c, st, "commit", msg, fb, hi) if st.get("send_as") != "merchant_on_behalf" else (fb, None)
        return _send(st, body, "binary_confirm_cancel",
                     why or "Merchant committed — switched straight to action mode with a concrete draft; no more qualifying.")
    if kind == "off_topic":
        fb = _t_offtopic(c, st, hi)
        body, why = _llm_reply(c, st, "decline off-topic request politely, then steer back to the open item", msg, fb, hi)
        return _send(st, body, "binary_yes_no", why or "Out-of-scope ask declined politely; redirected to the open thread.")
    if kind == "question":
        fb = _t_question(c, st, msg, hi)
        body, why = _llm_reply(c, st, "answer the question briefly from context, then one next step", msg, fb, hi)
        return _send(st, body, "binary_yes_no", why or "Answered from context and re-offered the single next step.")
    fb = _t_engaged(c, st, hi)
    body, why = _llm_reply(c, st, "acknowledge and move to the single next step", msg, fb, hi)
    return _send(st, body, "binary_yes_no", why or "Engaged reply; moving to one concrete next step.")


def _send(st: dict, body: str, cta: str, rationale: str) -> dict:
    body = clean_ws(body)
    if body in st["sent"]:
        body = body + (" (Just a YES works.)" if "YES" in body else " 🙂")
        if body in st["sent"]:
            st["status"] = "ended"
            return {"action": "end", "rationale": "Nothing new to say without repeating myself; closing."}
    bot_turns = sum(1 for t in st["turns"] if t.get("from") == "bot")
    if bot_turns >= 6:
        st["status"] = "ended"
        return {"action": "end", "rationale": "Conversation has run long without closure; ending gracefully."}
    st["sent"].append(body)
    st["turns"].append({"from": "bot", "body": body})
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}
