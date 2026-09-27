"""Output checks. Anything the LLM writes must pass these or the template is used."""
from __future__ import annotations

import re

from .facts import Ctx

URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|io|org|net|co)\b(/|\s|$))", re.I)
PREAMBLE_RE = re.compile(r"\b(i hope (you|this)|hope you('| a)re (doing )?well|i am reaching out|i'm reaching out|"
                         r"reaching out to you|this is vera|i am vera|i'm vera|my name is)\b", re.I)
TIME_UNIT_RE = re.compile(r"(\d+)\s*(-\s*)?(min|mins|minute|minutes|sec|seconds|hr|hrs|hour|hours|h)\b", re.I)


_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_CTX_RE = re.compile(rf"\b(\d{{1,2}})\s*{_MON}\b|\b{_MON}\s*(\d{{1,2}})\b|\b(20\d\d)\b", re.I)


def CLOCK_OK(body: str) -> set[str]:
    out = set()
    for m in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", body, re.I):
        out.add(m.group(1))
        if m.group(2):
            out.add(m.group(2))
    return out


def numbers_in(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        t = tok.replace(",", "")
        try:
            v = float(t)
            out.append(f"{v:g}")
        except ValueError:
            pass
    return out


def check(body: str, c: Ctx, baseline: str = "", send_as: str = "vera", cta: str = "") -> list[str]:
    issues: list[str] = []
    if not body or not body.strip():
        return ["empty body"]
    import json as _j
    blob = _j.dumps([c.category, c.merchant, c.trigger, c.customer or {}], ensure_ascii=False).lower()
    scan = body
    # identifiers / ISO dates that appear verbatim in context are grounded as whole tokens
    for tok in set(re.findall(r"\b[\w\-/.:]*\d[\w\-/.:]*\b", body)):
        if re.search(r"[A-Za-z]", tok) or re.match(r"\d{4}-\d{2}-\d{2}", tok):
            if tok.lower().strip(".:") in blob:
                scan = re.sub(r"(?<![\w])" + re.escape(tok) + r"(?![\w])", " ", scan)
    values, dparts = c.grounding()
    allowed = values | {f"{float(x):g}" for x in numbers_in(baseline)}
    small_ok = {str(i) for i in range(0, 11)}
    time_ok = {m.group(1) for m in TIME_UNIT_RE.finditer(body) if int(m.group(1)) <= 60}
    date_ok = set()
    for m in DATE_CTX_RE.finditer(body):
        date_ok |= {g for g in m.groups() if g}
    for n in numbers_in(scan):
        if n in allowed or n in small_ok or n in time_ok:
            continue
        if n in dparts and (n in date_ok or n in CLOCK_OK(body)):
            continue
        issues.append(f"ungrounded number {n}")
    if URL_RE.search(body):
        issues.append("contains a URL")
    low = body.lower()
    for t in c.taboos:
        t0 = re.sub(r"\(.*?\)", "", t).strip().lower()
        if t0 and t0 in low:
            issues.append(f"taboo phrase '{t0}'")
    if PREAMBLE_RE.search(body):
        issues.append("preamble / self-introduction")
    for q in re.findall(r"[\"“]([^\"”]{6,})[\"”]", body):
        if not _grounded_quote(q, c):
            issues.append(f"quoted text not in context: {q[:40]}")
    if cta != "multi_choice_slot" and len(re.findall(r"\breply\b", low)) > 1:
        issues.append("more than one reply instruction")
    if len(body) > 650:
        issues.append("too long")
    name = c.cust_name if send_as == "merchant_on_behalf" else c.owner
    if name and name.lower() not in low and c.mname.lower() not in low:
        issues.append("missing recipient name")
    return issues


def _grounded_quote(q: str, c: Ctx) -> bool:
    import json
    blob = json.dumps([c.category, c.merchant, c.trigger, c.customer or {}], ensure_ascii=False).lower()
    q = q.lower().strip(" .")
    if q in blob:
        return True
    # allow minor formatting differences: check that most words appear
    words = [w for w in re.findall(r"[a-z0-9₹]+", q) if len(w) > 2]
    return bool(words) and sum(1 for w in words if w in blob) / len(words) >= 0.85
