"""Small shared helpers: dates, numbers, language, text."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_dt(value) -> datetime | None:
    """Parse ISO date/datetime strings (with or without tz, 'Z' suffix)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        if len(s) == 10:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=IST)
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def fmt_date(value, with_weekday: bool = False) -> str | None:
    """'2026-11-12' -> '12 Nov' (or 'Thu 12 Nov')."""
    dt = parse_dt(value) if not isinstance(value, datetime) else value
    if not dt:
        return None
    dt = dt.astimezone(IST)
    s = f"{dt.day} {dt.strftime('%b')}"
    return f"{dt.strftime('%a')} {s}" if with_weekday else s


def fmt_time(value) -> str | None:
    dt = parse_dt(value)
    if not dt:
        return None
    dt = dt.astimezone(IST)
    h = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d}{ampm}" if dt.minute else f"{h}{ampm}"


def days_between(a: datetime | None, b: datetime | None) -> int | None:
    if not a or not b:
        return None
    return (b.date() - a.date()).days


def inr(amount) -> str:
    """4999 -> '₹4,999' (Indian grouping)."""
    try:
        n = int(round(float(amount)))
    except (TypeError, ValueError):
        return str(amount)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def num(n) -> str:
    """Plain number with thousands separators (Indian style)."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return str(n)
    if v.is_integer():
        return inr(v)[1:] if v >= 0 else "-" + inr(-v)[1:]
    return f"{v:g}"


def pct(fraction, signed: bool = False) -> str:
    """0.215 -> '21.5%'; 0.2 -> '20%'. Accepts already-percent values > 1.5 as-is."""
    try:
        v = float(fraction)
    except (TypeError, ValueError):
        return str(fraction)
    p = v * 100 if abs(v) <= 1.5 else v
    p = round(p, 1)
    s = f"{p:g}%" if not p.is_integer() else f"{int(p)}%"
    if signed and p > 0:
        s = "+" + s
    return s


def abs_pct(fraction) -> str:
    try:
        return pct(abs(float(fraction)))
    except (TypeError, ValueError):
        return str(fraction)


def humanize(slug) -> str:
    """'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    if not isinstance(slug, str):
        return str(slug)
    return re.sub(r"[_\-]+", " ", slug).strip()


HONORIFICS = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "shri", "smt", "smt."}


def first_name(full) -> str:
    """'Mr. Sharma' -> 'Sharma'; 'Dr. Rajan' -> 'Rajan'; 'Aanya (parent: Sneha)' -> 'Aanya'."""
    if not full:
        return ""
    s = re.sub(r"\(.*?\)", "", str(full)).strip()
    parts = [w for w in s.split() if w.lower() not in HONORIFICS]
    return parts[0] if parts else ""


def seg(text) -> str:
    """'office_25-45' -> 'office 25-45'; keeps hyphens."""
    return re.sub(r"_+", " ", str(text or "")).strip()


def parent_name(full) -> str | None:
    m = re.search(r"parent:\s*([A-Za-z]+)", str(full or ""))
    return m.group(1) if m else None


def name_from_id(cid: str | None) -> str | None:
    """'c_001_priya_for_m001' -> 'Priya' (only used when customer context is missing)."""
    if not cid:
        return None
    m = re.match(r"c_\d+_([a-z]+)", cid)
    if not m or m.group(1) in ("anonymous", "walkin", "grandfather", "grandmother", "father", "mother", "son",
                                "daughter", "customer", "patient", "member", "guest", "parent"):
        return None
    return m.group(1).capitalize()


# --------------------------------------------------------------------------- language

HINGLISH_MARKERS = {
    "hai", "hain", "kya", "nahi", "nahin", "haan", "han", "karo", "kar", "karna", "mujhe", "aap",
    "aapka", "aapke", "ji", "chahiye", "bhai", "theek", "thik", "accha", "acha", "abhi", "baad",
    "mein", "kaise", "kitna", "kyun", "bolo", "batao", "bhejo", "hoga", "raha", "rahi", "sab",
    "kuch", "mat", "band", "shukriya", "dhanyavad", "chalega", "jaldi", "zaroor", "humein", "hum",
}


def is_hinglish(text: str) -> bool:
    if not text:
        return False
    if re.search(r"[ऀ-ॿ]", text):
        return True
    words = re.findall(r"[a-zA-Z]+", text.lower())
    hits = sum(1 for w in words if w in HINGLISH_MARKERS)
    return hits >= 2 or (hits >= 1 and len(words) <= 4)


def merchant_prefers_hinglish(merchant: dict) -> bool:
    langs = [str(l).lower() for l in (merchant.get("identity", {}) or {}).get("languages", []) or []]
    return "hi" in langs


def customer_lang(customer: dict | None) -> str:
    """Return one of: 'en', 'hinglish', 'hi', or '<xx>-en' for regional mixes."""
    if not customer:
        return "en"
    pref = str((customer.get("identity") or {}).get("language_pref") or "en").lower()
    if pref in ("hi",):
        return "hi"
    if pref.startswith("hi"):
        return "hinglish"
    m = re.match(r"(te|ta|kn|mr|bn|gu|ml|pa)", pref)
    if m:
        return m.group(1) + "-en"
    return "en"


REGIONAL_GREETING = {"te": "Namaskaram", "ta": "Vanakkam", "kn": "Namaskara", "mr": "Namaskar",
                     "bn": "Nomoskar", "gu": "Kem cho", "ml": "Namaskaram", "pa": "Sat Sri Akal"}


def clean_ws(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r" +([,.!?])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def norm_msg(text: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", "", (text or "").lower()).strip()
