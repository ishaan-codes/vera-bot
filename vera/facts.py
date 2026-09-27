"""Ctx: a read-only view over the 4 contexts with grounded, derived facts.

Everything a message may state must come from here, so the validator can
check the final text against `Ctx.allowed_numbers()`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .util import (num as _n, parse_dt, fmt_date, days_between, pct, abs_pct, humanize, first_name,
                   parent_name, name_from_id, merchant_prefers_hinglish, customer_lang, utcnow)

CAT_NOUNS = {
    "dentists":    {"biz": "clinic", "people": "patients", "one": "patient", "pro": "dentist", "pros": "dental clinics",
                    "visit": "check-up", "appt": "appointment", "trial": "first consultation"},
    "salons":      {"biz": "salon", "people": "clients", "one": "client", "pro": "salon", "pros": "salons",
                    "visit": "next salon visit", "appt": "appointment", "trial": "trial session"},
    "restaurants": {"biz": "restaurant", "people": "customers", "one": "customer", "pro": "restaurant", "pros": "restaurants",
                    "visit": "next visit", "appt": "table reservation", "trial": "first visit"},
    "gyms":        {"biz": "gym", "people": "members", "one": "member", "pro": "gym", "pros": "gyms",
                    "visit": "next session", "appt": "session", "trial": "trial class"},
    "pharmacies":  {"biz": "pharmacy", "people": "customers", "one": "customer", "pro": "pharmacy", "pros": "pharmacies",
                    "visit": "routine BP & sugar check", "appt": "pickup", "trial": "first visit"},
}
DEFAULT_NOUNS = {"biz": "business", "people": "customers", "one": "customer", "pro": "business", "pros": "businesses",
                 "visit": "next visit", "appt": "appointment", "trial": "first visit"}

METRIC_WORD = {"views": "profile views", "calls": "calls", "directions": "direction requests",
               "ctr": "click-through rate", "leads": "leads", "review_count": "reviews",
               "bookings": "bookings", "orders": "orders"}


class Ctx:
    def __init__(self, category: dict | None, merchant: dict | None, trigger: dict,
                 customer: dict | None = None, now: datetime | None = None):
        self.category = category or {}
        self.merchant = merchant or {}
        self.trigger = trigger or {}
        self.customer = customer
        self.now = now or utcnow()
        self.derived: list = []           # numbers we computed (allowed in text)

    # ------------------------------------------------------------------ basics
    @property
    def kind(self) -> str:
        return str(self.trigger.get("kind") or "generic")

    @property
    def tp(self) -> dict:
        p = self.trigger.get("payload") or {}
        return p if isinstance(p, dict) else {}

    @property
    def placeholder(self) -> bool:
        p = self.tp
        return bool(p.get("placeholder")) or not {k for k in p if k not in ("placeholder", "metric_or_topic", "category")}

    @property
    def slug(self) -> str:
        return self.category.get("slug") or self.merchant.get("category_slug") or ""

    @property
    def nouns(self) -> dict:
        return CAT_NOUNS.get(self.slug, DEFAULT_NOUNS)

    @property
    def ident(self) -> dict:
        return self.merchant.get("identity") or {}

    @property
    def mname(self) -> str:
        return self.ident.get("name") or "your business"

    @property
    def owner(self) -> str:
        return first_name(self.ident.get("owner_first_name") or "")

    @property
    def sal(self) -> str:
        """Merchant-facing salutation."""
        o = self.owner
        if self.slug == "dentists":
            return f"Dr. {o}" if o else "Doctor"
        return o or f"{self.mname} team"

    @property
    def locality(self) -> str:
        return self.ident.get("locality") or self.ident.get("city") or ""

    @property
    def city(self) -> str:
        return self.ident.get("city") or ""

    @property
    def hinglish(self) -> bool:
        return merchant_prefers_hinglish(self.merchant)

    @property
    def perf(self) -> dict:
        return self.merchant.get("performance") or {}

    @property
    def delta(self) -> dict:
        return self.perf.get("delta_7d") or {}

    @property
    def peer(self) -> dict:
        return self.category.get("peer_stats") or {}

    @property
    def agg(self) -> dict:
        return self.merchant.get("customer_aggregate") or {}

    @property
    def signals(self) -> list[str]:
        return [str(s) for s in (self.merchant.get("signals") or [])]

    def has_signal(self, prefix: str) -> str | None:
        for s in self.signals:
            if s.startswith(prefix):
                return s
        return None

    @property
    def sub(self) -> dict:
        return self.merchant.get("subscription") or {}

    @property
    def voice(self) -> dict:
        return self.category.get("voice") or {}

    @property
    def taboos(self) -> list[str]:
        return [str(t) for t in (self.voice.get("vocab_taboo") or self.voice.get("taboos") or [])]

    # ------------------------------------------------------------------ offers
    def active_offers(self) -> list[str]:
        return [o.get("title") for o in (self.merchant.get("offers") or [])
                if o.get("title") and str(o.get("status", "active")).lower() == "active"]

    def best_offer(self, prefer: list[str] | None = None) -> str | None:
        offers = self.active_offers()
        for kw in prefer or []:
            for t in offers:
                if kw.lower() in t.lower():
                    return t
        return offers[0] if offers else None

    def catalog_offer(self, prefer: list[str] | None = None, service_price_only: bool = True) -> str | None:
        cat = self.category.get("offer_catalog") or []
        items = [o for o in cat if o.get("title")]
        for kw in prefer or []:
            for o in items:
                if kw.lower() in o["title"].lower():
                    return o["title"]
        # match the merchant's own identity (e.g. 'Pizza Spot' -> pizza offer), else a safe category default
        name_words = [w for w in re.findall(r"[a-z]+", self.mname.lower()) if len(w) > 3]
        for o in items:
            if any(w in o["title"].lower() for w in name_words):
                return o["title"]
        safe = {"restaurants": "delivery"}.get(self.slug)
        if safe:
            for o in items:
                if safe in o["title"].lower():
                    return o["title"]
        for o in items:
            if not service_price_only or o.get("type") in ("service_at_price", None) and "@" in o["title"]:
                return o["title"]
        return items[0]["title"] if items else None

    # ------------------------------------------------------------------ digest
    def digest(self) -> list[dict]:
        return [d for d in (self.category.get("digest") or []) if isinstance(d, dict)]

    def digest_item(self, item_id) -> dict | None:
        for d in self.digest():
            if d.get("id") == item_id:
                return d
        return None

    def digest_of(self, kinds: tuple, keywords: tuple = ()) -> dict | None:
        items = [d for d in self.digest() if d.get("kind") in kinds]
        for kw in keywords:
            for d in items:
                if kw.lower() in json.dumps(d).lower():
                    return d
        return items[-1] if items else None   # newest injected items are appended last

    def seasonal_now(self) -> dict | None:
        """Seasonal beat whose month_range covers `now` (IST)."""
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        m = self.now.month
        for b in self.category.get("seasonal_beats") or []:
            rng = str(b.get("month_range", ""))
            parts = re.findall(r"[A-Z][a-z]{2}", rng)
            if not parts or parts[0] not in months:
                continue
            a = months.index(parts[0]) + 1
            z = months.index(parts[-1]) + 1 if parts[-1] in months else a
            inside = (a <= m <= z) if a <= z else (m >= a or m <= z)
            if inside:
                return b
        return None

    def next_beat(self, prefer: tuple = ("festival", "wedding", "diwali", "season")) -> dict | None:
        """The next seasonal beat starting after `now` (wraps the year); prefers festive notes."""
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        cands = []
        for b in self.category.get("seasonal_beats") or []:
            parts = re.findall(r"[A-Z][a-z]{2}", str(b.get("month_range", "")))
            if not parts or parts[0] not in months:
                continue
            start = months.index(parts[0]) + 1
            ahead = (start - self.now.month) % 12
            fest = any(k in str(b.get("note", "")).lower() for k in prefer)
            cands.append((0 if fest else 1, ahead, b))
        cands.sort(key=lambda x: (x[0], x[1]))
        return cands[0][2] if cands else None

    def top_trend(self) -> dict | None:
        tr = [t for t in (self.category.get("trend_signals") or []) if isinstance(t.get("delta_yoy"), (int, float))]
        return max(tr, key=lambda t: t["delta_yoy"]) if tr else None

    # ------------------------------------------------------------------ performance judgement
    def ctr_gap(self) -> tuple[str, str] | None:
        """(merchant ctr, peer ctr) as strings when merchant is below peer."""
        c, p = self.perf.get("ctr"), self.peer.get("avg_ctr")
        if isinstance(c, (int, float)) and isinstance(p, (int, float)) and c < p:
            return pct(c), pct(p)
        return None

    def worst_delta(self):
        best = None
        for k, v in self.delta.items():
            if isinstance(v, (int, float)) and v < 0 and (best is None or v < best[1]):
                best = (k.replace("_pct", ""), v)
        return best

    def best_delta(self):
        best = None
        for k, v in self.delta.items():
            if isinstance(v, (int, float)) and v > 0 and (best is None or v > best[1]):
                best = (k.replace("_pct", ""), v)
        return best

    def stale_days(self) -> str | None:
        s = self.has_signal("stale_posts")
        m = re.search(r"(\d+)", s or "")
        return m.group(1) if m else None

    def positive_review(self) -> dict | None:
        for r in self.merchant.get("review_themes") or []:
            if r.get("sentiment") == "pos":
                return r
        return None

    def negative_review(self) -> dict | None:
        neg = [r for r in self.merchant.get("review_themes") or [] if r.get("sentiment") == "neg"]
        return max(neg, key=lambda r: r.get("occurrences_30d", 0)) if neg else None

    def fix_lever(self):
        """Pick the single most fixable issue from merchant state. Returns (diagnosis, action_en, action_hi, next_action)."""
        n = self.nouns
        if not self.active_offers():
            off = self.catalog_offer()
            if off:
                return (f"you have no active offer live right now — {n['people']} searching nearby see nothing to act on",
                        f"put \"{off}\" live on your profile today", f"\"{off}\" aaj hi live kar doon",
                        f"set up the offer \"{off}\" on the listing")
        sd = self.stale_days()
        if sd:
            return (f"your last Google post was {sd} days ago",
                    "draft 3 fresh Google posts for you to approve", "3 naye Google posts draft kar doon",
                    "draft 3 Google posts")
        if self.has_signal("unverified"):
            return ("your Google profile is still unverified, which caps how often it shows up",
                    "start the verification for you", "verification start kar doon",
                    "start Google profile verification")
        gap = self.ctr_gap()
        if gap:
            return (f"your click-through is {gap[0]} vs {gap[1]} for similar {n['pros']} — people see you but don't tap",
                    "rewrite your listing headline + top photo order", "listing headline aur photos theek kar doon",
                    "rewrite the listing headline and reorder photos")
        off = self.best_offer()
        return (f"\"{off}\" is your only hook right now" if off else "the listing has no fresh hook",
                "draft a fresh post around it" if off else "draft a fresh post", "ek fresh post draft kar doon",
                "draft a fresh Google post")

    # ------------------------------------------------------------------ smarter selection
    NEW_USER_WORDS = ("first month", "trial", "free consultation", "new member", "first visit", "demo", "intro")
    DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    def offer_ok_on(self, title: str, when) -> bool:
        """False if the offer is day-restricted, e.g. '(Tue-Thu)', and `when` falls outside it."""
        m = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*[-–]\s*(mon|tue|wed|thu|fri|sat|sun)", title.lower())
        if not m or not when:
            return True
        a, z = self.DAY_NAMES.index(m.group(1)), self.DAY_NAMES.index(m.group(2))
        d = when.weekday()
        return a <= d <= z if a <= z else (d >= a or d <= z)

    def offers_for(self, audience: str = "any", when=None, prefer: list | None = None) -> list[str]:
        out = []
        for t in self.active_offers():
            low = t.lower()
            if audience == "existing" and any(w in low for w in self.NEW_USER_WORDS):
                continue
            if when is not None and not self.offer_ok_on(t, when):
                continue
            out.append(t)
        if prefer:
            pref = [t for t in out if any(k.lower() in t.lower() for k in prefer)]
            out = pref + [t for t in out if t not in pref]
        return out

    def matched_trend(self) -> dict | None:
        """Trend signal most relevant to THIS merchant (offer/name/review words), else the strongest."""
        trends = [t for t in (self.category.get("trend_signals") or []) if isinstance(t.get("delta_yoy"), (int, float))]
        if not trends:
            return None
        words = " ".join(self.active_offers() + [self.mname] +
                         [str(r.get("theme", "")) + " " + str(r.get("common_quote", "")) for r in self.merchant.get("review_themes") or []]).lower()
        def score(t):
            q = [w for w in re.findall(r"[a-z]+", str(t.get("query", "")).lower()) if w not in ("near", "me", "price", "cost", "offer", "delhi", "mumbai") and len(w) > 3]
            return (sum(1 for w in q if w in words), t["delta_yoy"])
        best = max(trends, key=score)
        return best

    @staticmethod
    def trend_topic(t: dict) -> str:
        q = str(t.get("query", ""))
        q = re.sub(r"\b(near me|price|cost|delhi|mumbai|bangalore|chennai|hyderabad|pune)\b", "", q, flags=re.I)
        return re.sub(r"\s+", " ", q).strip()

    def open_thread(self) -> str | None:
        """Merchant's last message if Vera hasn't answered it yet."""
        hist = self.merchant.get("conversation_history") or []
        if hist and hist[-1].get("from") == "merchant" and hist[-1].get("body"):
            return hist[-1]["body"]
        return None

    def last_vera_proposal(self) -> str | None:
        for h in reversed(self.merchant.get("conversation_history") or []):
            if h.get("from") == "vera" and re.search(r"\d", h.get("body", "")):
                return h["body"]
        return None

    def expired(self) -> bool:
        return str(self.sub.get("status", "")).lower() == "expired"

    def own_hook(self):
        """Strongest verifiable fact about the merchant's own numbers: (sentence, kind)."""
        n = self.nouns
        wd = self.worst_delta()
        if wd and wd[1] <= -0.1:
            k, v = wd
            cur = self.perf.get(k)
            return (f"your {METRIC_WORD.get(k, k)} are down {pct(abs(v))} this week" + (f" ({_n(cur)} in the last 30 days)" if cur is not None else ""), "dip")
        c, p = self.perf.get("ctr"), self.peer.get("avg_ctr")
        if isinstance(c, (int, float)) and isinstance(p, (int, float)) and abs(c - p) >= 0.005:
            if c < p:
                return (f"your profile click-through is {pct(c)} vs {pct(p)} for similar {n['pros']} — people see you but don't tap", "ctr_low")
            if not self.active_offers():
                return (f"your click-through is {pct(c)} vs {pct(p)} for similar {n['pros']} — people are tapping, but there's no offer waiting for them", "ctr_high_no_offer")
        v, pv = self.perf.get("views"), self.peer.get("avg_views_30d")
        if isinstance(v, (int, float)) and isinstance(pv, (int, float)) and v < pv * 0.7:
            return (f"your profile got {_n(v)} views in 30 days vs about {_n(pv)} for similar {n['pros']}", "views_low")
        bd = self.best_delta()
        if bd and bd[1] >= 0.1:
            return (f"your {METRIC_WORD.get(bd[0], bd[0])} are up {pct(bd[1])} this week", "up")
        if v:
            return (f"{self.mname} got {_n(v)} profile views and {_n(self.perf.get('calls') or 0)} calls in the last 30 days", "base")
        return None

    # ------------------------------------------------------------------ customer
    @property
    def cust_ident(self) -> dict:
        return (self.customer or {}).get("identity") or {}

    @property
    def cust_name(self) -> str:
        raw = self.cust_ident.get("name") or name_from_id(self.trigger.get("customer_id")) or ""
        if raw.startswith("("):
            return ""
        return first_name(raw)

    @property
    def cust_parent(self) -> str | None:
        return parent_name(self.cust_ident.get("name"))

    @property
    def cust_lang(self) -> str:
        return customer_lang(self.customer)

    @property
    def senior(self) -> bool:
        ci = self.cust_ident
        return bool(ci.get("senior_citizen")) or str(ci.get("age_band", "")).startswith(("60", "65", "70", "75"))

    @property
    def rel(self) -> dict:
        return (self.customer or {}).get("relationship") or {}

    @property
    def prefs(self) -> dict:
        return (self.customer or {}).get("preferences") or {}

    def months_since_last_visit(self) -> int | None:
        d = days_between(parse_dt(self.rel.get("last_visit")), self.now)
        if d is None or d < 0:
            return None
        m = round(d / 30.4)
        self.derived.append(m)
        return m

    def days_since_last_visit(self) -> int | None:
        d = days_between(parse_dt(self.rel.get("last_visit")), self.now)
        if d is None or d < 0:
            return None
        self.derived.append(d)
        return d

    def consent_ok(self) -> bool:
        if not self.customer:
            return True
        c = self.customer.get("consent") or {}
        p = self.prefs
        if p.get("reminder_opt_in") is False and not c.get("scope"):
            return False
        if not c.get("opted_in_at") and not c.get("scope"):
            return False
        return True

    # ------------------------------------------------------------------ grounding
    def allowed_numbers(self) -> set[str]:
        """Numeric tokens stated as values in the contexts (dates/IDs excluded), plus derived ones."""
        return self.grounding()[0]

    def grounding(self) -> tuple[set[str], set[str]]:
        """(value numbers, date-part numbers). Date parts are only valid when written as dates."""
        objs = [self.category, self.merchant, self.trigger, self.customer or {}]
        blob = json.dumps(objs, ensure_ascii=False)
        date_re = r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:[+-]\d{2}:\d{2}|Z)?)?"
        dates = re.findall(date_re, blob)
        text = re.sub(date_re, " ", blob)
        text = re.sub(r"\b[A-Za-z]+[_\-]?[A-Za-z]*\d[\w\-]*\b", " ", text)       # ids like m_001_x, AT2024-1102, W17
        text = re.sub(r"\b\w*_\w*\b", " ", text)                                # snake_case keys/ids
        found: set[str] = set()
        for tok in re.findall(r"\d[\d,]*\.?\d*", text):
            found |= _num_variants(tok)
        for v in _walk_numbers(objs):
            found |= _num_variants(str(v))
            if -1.5 <= v <= 1.5:
                for p in (v * 100, abs(v * 100)):
                    found |= _num_variants(f"{round(p, 1):g}")
                    found |= _num_variants(str(int(round(p))))
        for v in self.derived:
            found |= _num_variants(str(v))
        dparts: set[str] = set()
        from .util import IST
        for s in dates:
            dt = parse_dt(s)
            if dt:
                d = dt.astimezone(IST)
                dparts |= {str(d.day), str(d.year), str(d.hour % 12 or 12), f"{d.minute:02d}"}
        return found, dparts


def _num_variants(tok: str) -> set[str]:
    t = tok.replace(",", "").rstrip(".")
    out = {t}
    try:
        v = float(t)
        out.add(f"{v:g}")
        if v.is_integer():
            out.add(str(int(v)))
    except ValueError:
        pass
    return out


def _walk_numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_numbers(v)
