"""Deterministic, grounded message templates — one per trigger family.

Each builder returns a Draft built only from facts in Ctx. The Gemini layer
may rewrite a Draft, but the Draft alone is always a valid, sendable message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import Ctx, METRIC_WORD
from .util import (IST, seg, fmt_date, fmt_time, parse_dt, days_between, pct, abs_pct, inr, num, humanize,
                   clean_ws, REGIONAL_GREETING)


@dataclass
class Draft:
    body: str
    cta: str = "binary_yes_no"
    send_as: str = "vera"
    rationale: str = ""
    template_name: str = ""
    template_params: list = field(default_factory=list)
    next_action: str = ""          # what a YES commits Vera to do
    anchor: str = ""               # the single fact that drives the message
    skip: bool = False             # planner should not send
    skip_reason: str = ""


def ask(c: Ctx, en: str, hi: str | None = None) -> str:
    """Final CTA sentence; Hinglish variant for merchants whose languages include 'hi'
    (unless the category voice is English-primary, e.g. gyms)."""
    eng_primary = str(c.voice.get("code_mix", "")).startswith("english_primary")
    return hi if (hi and c.hinglish and not eng_primary) else en


def _finish(c: Ctx, d: Draft, anchor_line: str = "") -> Draft:
    d.body = clean_ws(pretty_dates(d.body))
    d.template_name = d.template_name or f"vera_{c.kind}_v1"
    if not d.template_params:
        head = c.sal if d.send_as == "vera" else (c.cust_name or "there")
        d.template_params = [head, anchor_line or d.anchor, d.next_action or ""]
    return d


# ====================================================================== merchant-facing

def t_research(c: Ctx) -> Draft:
    p = c.tp
    item = c.digest_item(p.get("top_item_id") or p.get("digest_item_id")) or \
        c.digest_of(("research",)) or c.digest_of(("trend", "tech"))
    if not item:
        return t_generic(c)
    src = item.get("source", "")
    title = item.get("title", "")
    summary = _first_sentence(item.get("summary", ""))
    n = c.nouns
    if item.get("kind") not in ("research",) and item.get("actionable"):
        act = _dot(_first_sentence(item.get("actionable", "")))
        body = (f"{c.sal}, worth a look from {src}: {title}. {_dot(summary)} Suggested move: {act} "
                + ask(c, "Want me to set it up for you? Reply YES.", "Main aapke liye set up kar doon? Reply YES."))
        return _finish(c, Draft(body=body, cta="binary_yes_no",
                                rationale=f"Category digest item ({item.get('kind')}) with a concrete, sourced action; effort externalised.",
                                next_action=f"set up: {act}", anchor=f"{title} ({src})"))
    rel = ""
    seg = str(item.get("patient_segment", ""))
    if seg and "high_risk" in seg and c.agg.get("high_risk_adult_count"):
        rel = f"Directly relevant to the {num(c.agg['high_risk_adult_count'])} high-risk adults in your {n['people']} list."
    elif item.get("trial_n"):
        rel = f"Solid sample — {num(item['trial_n'])} {n['people']} in the trial."
    elif c.agg.get("total_unique_ytd"):
        rel = f"Useful framing for your {num(c.agg['total_unique_ytd'])} {n['people']} this year."
    body = (f"{c.sal}, new in {src}: {title}. {summary} {rel} "
            + ask(c, "Want me to send the 2-min summary + a patient-friendly WhatsApp you can forward? Reply YES.",
                  "2-min summary + ek patient-friendly WhatsApp draft bhej doon? Reply YES."))
    if c.slug != "dentists":
        body = body.replace("patient-friendly", "customer-friendly")
    return _finish(c, Draft(
        body=body, cta="binary_yes_no",
        rationale=f"Research digest ({src}) chosen as the anchor; tied to merchant's own {n['people']} base; reciprocity CTA (I do the work).",
        next_action=f"send the summary of '{title}' and draft a {n['one']}-education WhatsApp",
        anchor=f"{title} ({src})"))


def t_compliance(c: Ctx) -> Draft:
    p = c.tp
    item = c.digest_item(p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id")) or \
        c.digest_of(("compliance",))
    if not item:
        return t_generic(c)
    deadline = p.get("deadline_iso") or _date_in(item.get("title", ""))
    dl = fmt_date(deadline) if deadline else None
    days = days_between(c.now, parse_dt(deadline)) if deadline else None
    when = ""
    if dl and days is not None and days >= 0:
        c.derived.append(days)
        when = f"Effective {dl} — {days} days from now."
    elif dl:
        when = f"Effective {dl}."
    src_txt = item.get("source", "the regulator")
    sd = re.search(r"\d{4}-\d{2}-\d{2}", src_txt)
    if sd and parse_dt(sd.group(0)) and parse_dt(sd.group(0)) > c.now:
        src_txt = src_txt.replace(sd.group(0), "").strip()
    title = re.sub(r"\s*effective \d{4}-\d{2}-\d{2}", "", item.get("title", "")) if when else item.get("title", "")
    body = (f"{c.sal}, compliance heads-up ({src_txt}): {title}. "
            f"{item.get('summary', '')} {when} "
            + ask(c, "Want a 5-point audit checklist for your setup so you're covered before the date? Reply YES.",
                  "Aapke setup ke liye 5-point audit checklist bhej doon, taaki deadline se pehle sab clear ho? Reply YES."))
    return _finish(c, Draft(
        body=body, cta="binary_yes_no",
        rationale="Regulation change with a hard deadline — loss-aversion anchor; low-effort checklist offer.",
        next_action="send a 5-point compliance audit checklist", anchor=item.get("title", "")))


def t_cde(c: Ctx) -> Draft:
    p = c.tp
    item = c.digest_item(p.get("digest_item_id") or p.get("top_item_id")) or c.digest_of(("cde",))
    if not item:
        return t_generic(c)
    date = item.get("date") or p.get("date")
    when = " ".join(x for x in (fmt_date(date, True), fmt_time(date)) if x)
    credits = p.get("credits") or item.get("credits")
    fee = item.get("actionable") if "free" in str(item.get("actionable", "")).lower() else humanize(p.get("fee", ""))
    bits = [x for x in (when, f"{credits} CDE credits" if credits else "", fee) if x]
    body = (f"{c.sal}, {item.get('title', '')} — {', '.join(bits)}. {' '.join(_sentences(item.get('summary', ''))[:2])} "
            + ask(c, "Want me to save your seat and remind you an hour before? Reply YES.",
                  "Seat reserve karke 1 ghanta pehle reminder bhej doon? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Continuing-education event with date + credits; effort externalised (I register/remind).",
                            next_action="reserve a seat and set a reminder", anchor=item.get("title", "")))


def t_supply_alert(c: Ctx) -> Draft:
    p = c.tp
    item = c.digest_item(p.get("alert_id") or p.get("top_item_id")) or c.digest_of(("alert", "supply"))
    mol = p.get("molecule") or ""
    batches = p.get("affected_batches") or []
    mfr = p.get("manufacturer")
    parts = []
    if mol or batches:
        parts.append(f"voluntary recall on {len(batches) or 'some'} {mol} batch{'es' if len(batches) != 1 else ''}"
                     + (f" ({', '.join(batches)})" if batches else "") + (f" by {mfr}" if mfr else ""))
    summary = _first_sentence((item or {}).get("summary", ""), skip_if=("numbers in alert",))
    if not summary and item:
        summary = _sentences(item.get("summary", ""))[1:2][0] if len(_sentences(item.get("summary", ""))) > 1 else ""
    cnt = c.agg.get("chronic_rx_count")
    who = (f"With {num(cnt)} chronic-Rx customers on your books, anyone dispensed these batches should hear from you first."
           if cnt else "Customers who bought these batches should be informed for replacement.")
    src = (item or {}).get("source")
    lead = "; ".join(parts) or (item or {}).get("title", "supply alert")
    body = (f"{c.sal}, urgent — {lead}{' (' + src + ')' if src else ''}. {summary} {who} "
            + ask(c, "Want me to draft the customer WhatsApp + the replacement-pickup steps? Reply YES.",
                  "Customer WhatsApp aur replacement-pickup steps draft kar doon? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Urgency-5 safety/supply alert: batch-level specificity + merchant's chronic-Rx base; complete-workflow offer.",
                            next_action="draft the customer recall note and replacement workflow", anchor=lead))


def t_category_seasonal(c: Ctx) -> Draft:
    p = c.tp
    trends = p.get("trends") or []
    shown = []
    for t in trends[:4]:
        m = re.match(r"(.+?)_demand_([+-]?\d+)", str(t))
        if m:
            name = humanize(m.group(1)).replace("cold cough", "cold & cough")
            v = int(m.group(2))
            shown.append(f"{name} {'+' if v > 0 else '−'}{abs(v)}%")
    item = c.digest_of(("seasonal",))
    beat = c.seasonal_now()
    if not shown and item:
        shown = [item.get("title", "")]
    if not shown and beat:
        shown = [beat.get("note", "")]
    if not shown:
        return t_generic(c)
    src = f" ({item['source']})" if item and item.get("source") else ""
    action = _dot(_first_sentence(item.get("actionable", ""))) if item else ""
    rep = c.agg.get("repeat_customer_pct")
    aud = f" for your repeat customers ({pct(rep)} of your base)" if rep else ""
    body = (f"{c.sal}, the {humanize(p.get('season', 'seasonal')).replace('2026', '').strip()} shift is showing up: "
            f"{', '.join(shown)}{src}. {action} "
            + ask(c, f"Want me to draft a 'season essentials' Google post + WhatsApp{aud}? Reply YES.",
                  f"Ek 'season essentials' Google post + WhatsApp{aud} draft kar doon? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Seasonal demand shift with concrete % moves; shelf action + ready-made content offer.",
                            next_action="draft a season-essentials Google post and WhatsApp", anchor=", ".join(shown)))


def t_perf_dip(c: Ctx) -> Draft:
    p = c.tp
    metric = p.get("metric")
    dv = p.get("delta_pct")
    if metric is None or dv is None:
        wd = c.worst_delta()
        if wd and wd[1] <= -0.1:
            metric, dv = wd
    diag, act_en, act_hi, nxt = c.fix_lever()
    if metric is None or dv is None:
        hook = c.own_hook()
        lead = f"{c.sal}, quick look at {c.mname}: {hook[0] if hook else diag}."
        if hook and hook[1] != "ctr_high_no_offer" and diag not in hook[0]:
            lead += f" What I'd fix first: {diag}."
    else:
        mw = METRIC_WORD.get(metric, humanize(metric))
        base = p.get("vs_baseline")
        cur = c.perf.get(metric)
        pc0 = c.peer.get("avg_calls_30d")
        show_peer = metric == "calls" and pc0 and cur is not None and cur < pc0
        extra = "" if show_peer else (f" (baseline {num(base)})" if base else (f" ({num(cur)} in the last 30 days)" if cur is not None else ""))
        lead = f"{c.sal}, your {mw} are down {abs_pct(dv)} this week{extra}."
        if show_peer:
            lead += f" That's {num(cur)} calls in 30 days vs about {num(pc0)} for similar {c.nouns['pros']}."
        lead += f" The quickest fix I can see: {diag}."
    body = lead + " " + ask(c, f"Shall I {act_en}? Reply YES.", f"Main {act_hi}? Reply YES.")
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale=f"Performance dip; picked one fixable lever from merchant state ({nxt}) instead of listing every metric.",
                            next_action=nxt, anchor=lead))


def t_perf_spike(c: Ctx) -> Draft:
    p = c.tp
    metric, dv = p.get("metric"), p.get("delta_pct")
    if metric is None or dv is None:
        bd = c.best_delta()
        if bd:
            metric, dv = bd
    if metric is None or dv is None:
        return t_generic(c)
    if p.get("placeholder") and abs(dv) < 0.1:
        diag, act_en, act_hi, nxt = c.fix_lever()
        mw = METRIC_WORD.get(metric, humanize(metric))
        body = (f"{c.sal}, {mw} ticked up {abs_pct(dv)} this week at {c.mname} — small, but a sign people are looking. "
                f"To turn lookers into visits: {diag}. " + ask(c, f"Shall I {act_en}? Reply YES.", f"Main {act_hi}? Reply YES."))
        return _finish(c, Draft(body=body, cta="binary_yes_no", rationale="Small uptick: acknowledged honestly, paired with the one fix that converts it.",
                                next_action=nxt, anchor=f"{mw} +{abs_pct(dv)}"))
    mw = METRIC_WORD.get(metric, humanize(metric))
    driver = p.get("likely_driver")
    drv = f" — most likely from your {humanize(driver)}" if driver else ""
    base = p.get("vs_baseline")
    b = f" (baseline {num(base)})" if base else ""
    dwords = [w for w in humanize(driver or "").split() if len(w) > 3 and w not in ("post", "posts")]
    offer = next((o for o in c.active_offers() if any(w in o.lower() for w in dwords)), None) if dwords else c.best_offer()
    prop = c.last_vera_proposal() if dwords else None
    if dwords and not offer and prop and any(w in prop.lower() for w in dwords):
        spec = next((s_ for s_ in _sentences(prop) if re.search(r"\d", s_) and "?" not in s_), None)
        spec_txt = re.sub(r"^.*?suggest\s*", "", spec or "", flags=re.I).rstrip(".")
        ride = (f"Convert it while it's hot: open enrolment for the {' '.join(dwords)} plan we discussed ({spec_txt}) with a seat cap."
                if spec else "")
        cta_txt = ask(c, "Want me to post the enrolment call today? Reply YES.", "Aaj hi enrolment post kar doon? Reply YES.")
        nxt = f"post the {' '.join(dwords)} enrolment call"
    elif offer:
        ride = f"Best time to push \"{offer}\" while interest is up."
        cta_txt = ask(c, "Want me to draft a follow-up post around it? Reply YES.", "Uske around ek follow-up post draft kar doon? Reply YES.")
        nxt = f"draft a follow-up post around {offer}"
    else:
        cat_off = c.catalog_offer()
        ride = f"You have no live offer to catch this traffic — \"{cat_off}\" would be an easy one to add." if cat_off else "Worth catching this traffic with an offer."
        cta_txt = ask(c, "Want me to put it live today? Reply YES.", "Main ise aaj hi live kar doon? Reply YES.")
        nxt = f"put the offer \"{cat_off}\" live" if cat_off else "set up an offer"
    body = f"{c.sal}, good news: {mw} are up {abs_pct(dv)} this week{b}{drv}. {ride} " + cta_txt
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Positive spike: converted the attention with the offer/program that actually caused it.",
                            next_action=nxt, anchor=f"{mw} +{abs_pct(dv)}"))


def t_seasonal_dip(c: Ctx) -> Draft:
    p = c.tp
    metric, dv = p.get("metric", "views"), p.get("delta_pct")
    beat = c.seasonal_now()
    item = c.digest_of(("seasonal",))
    members = c.agg.get("total_active_members")
    mw = METRIC_WORD.get(metric, humanize(metric))
    note = humanize(p.get("season_note", "")) if p.get("season_note") else (beat or {}).get("note", "")
    lead = f"{c.sal}, your {mw} are down {abs_pct(dv)} this week" if dv is not None else f"{c.sal}, a quick seasonal read"
    body = (f"{lead} — and that's expected: {note}. "
            + (f"{_first_sentence(item.get('summary', ''))} " if item else "")
            + (f"Right move now is retention, not ad spend — keep your {num(members)} members showing up. " if members
               else "Right move now is retention, not ad spend. ")
            + ask(c, "Want me to draft a 4-week attendance challenge for your members? Reply YES.",
                  "Members ke liye 4-week attendance challenge draft kar doon? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Seasonal dip reframed as normal (anxiety pre-emption) with retention action on the member base.",
                            next_action="draft a 4-week member attendance challenge", anchor=lead))


def t_milestone(c: Ctx) -> Draft:
    p = c.tp
    metric = p.get("metric")
    now_v, goal = p.get("value_now"), p.get("milestone_value")
    if metric and now_v is not None and goal is not None:
        mw = METRIC_WORD.get(metric, humanize(metric))
        gap = goal - now_v if isinstance(goal, (int, float)) and isinstance(now_v, (int, float)) else None
        if gap is not None:
            c.derived.append(gap)
        peer = c.peer.get("avg_review_count") if metric == "review_count" else None
        pl = f" (similar {c.nouns['pros']} average {num(peer)})" if peer else ""
        pos = c.positive_review()
        seed = (f" Your {humanize(pos.get('theme'))} fans are the ones to ask — {num(pos.get('occurrences_30d'))} reviews this month already mention it."
                if pos and pos.get("occurrences_30d") else "")
        lead = (f"{c.sal}, you're at {num(now_v)} {mw}{pl} — just {num(gap)} away from {num(goal)}." if gap and gap > 0
                else f"{c.sal}, you just crossed {num(goal)} {mw}{pl}.")
        body = lead + seed + " " + ask(c, f"Want me to draft a one-line review request you can send your regulars today? Reply YES.",
                                       f"Regulars ke liye ek one-line review request draft kar doon? Reply YES.")
        return _finish(c, Draft(body=body, cta="binary_yes_no",
                                rationale="Milestone within reach — goal-gradient effect; one tiny action closes the gap.",
                                next_action="draft a one-line review request message", anchor=lead))
    hook = c.own_hook()
    if hook and hook[1] in ("up",):
        body = (f"{c.sal}, nice week for {c.mname}: {hook[0]}. "
                + ask(c, f"Want me to turn it into a 'thank you, {c.locality}' post while the momentum is there? Reply YES.",
                      f"Ise ek 'thank you, {c.locality}' post mein badal doon? Reply YES."))
        return _finish(c, Draft(body=body, cta="binary_yes_no", rationale="Milestone trigger without payload: celebrated a verified improvement.",
                                next_action="draft a thank-you Google post", anchor=hook[0]))
    hook = hook or (None, None)
    diag, act_en, act_hi, nxt = c.fix_lever()
    body = (f"{c.sal}, before we chase the next milestone for {c.mname}, one gap to close: "
            f"{hook[0] + ', and ' if hook[0] else ''}{diag}. "
            + ask(c, f"Shall I {act_en}? Reply YES.", f"Main {act_hi}? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Milestone trigger without payload and no strong numbers to celebrate: honest gap + one fix.",
                            next_action=nxt, anchor=hook[0] or diag))


def t_renewal(c: Ctx) -> Draft:
    p = c.tp
    days = p.get("days_remaining", c.sub.get("days_remaining"))
    plan = p.get("plan") or c.sub.get("plan") or "your"
    amt = p.get("renewal_amount")
    v, calls, dirs = c.perf.get("views"), c.perf.get("calls"), c.perf.get("directions")
    value = ""
    if v is not None:
        value = f"Last 30 days it brought you {num(v)} profile views, {num(calls or 0)} calls and {num(dirs or 0)} direction requests. "
    status = str(c.sub.get("status", "")).lower()
    if status == "expired" or (isinstance(days, (int, float)) and days <= 0):
        return t_winback_merchant(c)
    if isinstance(days, (int, float)) and days > 30 and not amt:
        diag, act_en, act_hi, nxt = c.fix_lever()
        body = (f"{c.sal}, quick account check: your {plan} plan is active for another {num(days)} days. {value}"
                f"To get more out of it: {diag}. " + ask(c, f"Shall I {act_en}? Reply YES.", f"Main {act_hi}? Reply YES."))
        return _finish(c, Draft(body=body, cta="binary_yes_no",
                                rationale="Renewal is not actually close; used the touchpoint to show value and fix one lever instead of pushing a premature renewal.",
                                next_action=nxt, anchor=f"{days} days left"))
    label = "trial ends" if str(plan).lower() == "trial" else "plan renews"
    when = f"in {num(days)} days" if isinstance(days, (int, float)) else "soon"
    body = (f"{c.sal}, your magicpin {plan + ' ' if label == 'plan renews' else ''}{label} {when}{' (' + inr(amt) + ')' if amt else ''}. {value}"
            + ask(c, "Want me to renew it so the listing doesn't drop off mid-month? Reply YES." if label == "plan renews"
                  else "Want me to move you to the Pro plan so none of this stops? Reply YES.",
                  "Renew kar doon taaki listing beech mein band na ho? Reply YES." if label == "plan renews"
                  else "Pro plan pe shift kar doon taaki yeh sab chalta rahe? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Renewal due: show value delivered (own numbers) + loss aversion of going dark.",
                            next_action="process the plan renewal", anchor=f"renews {when}"))


def t_winback_merchant(c: Ctx) -> Draft:
    p = c.tp
    dse = p.get("days_since_expiry", c.sub.get("days_since_expiry"))
    dip = p.get("perf_dip_pct")
    lapsed = p.get("lapsed_customers_added_since_expiry")
    bits = []
    if dip is not None:
        bits.append(f"profile traffic is down {abs_pct(dip)}")
    if lapsed:
        bits.append(f"{num(lapsed)} more of your {c.nouns['people']} have gone inactive")
    off = c.catalog_offer()
    body = (f"{c.sal}, it's been {num(dse)} days since your magicpin plan lapsed" if dse else f"{c.sal}, your magicpin plan has lapsed")
    body += (f" — since then {' and '.join(bits)}. " if bits else ". ")
    body += (f"If you restart the plan, I'd put \"{off}\" live on day one so there's a reason to tap. " if off else "")
    body += ask(c, "Want me to restart your magicpin plan with that offer? Reply YES.",
                "Magicpin plan us offer ke saath restart kar doon? Reply YES.")
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Lapsed merchant: quantify what the gap has cost, then a single reactivation step.",
                            next_action=f"reactivate the listing with {off}" if off else "reactivate the listing",
                            anchor=f"{dse} days lapsed"))


def t_dormant(c: Ctx) -> Draft:
    p = c.tp
    if c.expired():
        return t_winback_merchant(c)
    ds = p.get("days_since_last_merchant_message")
    hook = c.own_hook()
    tr = c.matched_trend()
    gap_txt = f"it's been {num(ds)} days since we spoke — " if ds else ""
    parts = []
    if hook:
        parts.append(hook[0] + ".")
    if tr and (not hook or hook[1] in ("base", "up", "views_low")):
        parts.append(f"Meanwhile \"{Ctx.trend_topic(tr)}\" searches are up {pct(tr['delta_yoy'])} year-on-year.")
    diag, act_en, act_hi, nxt = c.fix_lever()
    body = (f"{c.sal}, {gap_txt}one thing worth 30 seconds for {c.mname}: {' '.join(parts) or diag + '.'} "
            + ask(c, f"Shall I {act_en}? Reply YES.", f"Main {act_hi}? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Dormant merchant: re-open with their own numbers (not a reminder) and one concrete fix.",
                            next_action=nxt, anchor=parts[0] if parts else diag))


def t_festival(c: Ctx) -> Draft:
    p = c.tp
    fest, date, du = p.get("festival"), p.get("date"), p.get("days_until")
    beat = c.next_beat()
    now_item = c.digest_of(("seasonal",))
    hook = c.own_hook()
    if fest:
        far = isinstance(du, (int, float)) and du > 60
        lead = f"{c.sal}, {fest} is on {fmt_date(date)}" + (f" — {num(du)} days out" if du else "") + "."
        if far:
            fest_beat = next((b for b in c.category.get("seasonal_beats") or [] if any(k in str(b.get("note", "")).lower() for k in ("festival", "wedding", "diwali"))), None)
            mid = (f" Too early for discounts, not for pre-bookings: in {fest_beat['month_range']} {fest_beat['note'].split('—')[-1].strip()}." if fest_beat
                   else " Too early for discounts, not for pre-bookings.")
        else:
            mid = " This is the window to put a festive offer live."
        bridal = c.catalog_offer(["bridal"]) if c.slug == "salons" else None
        hookline = ""
        if bridal and bridal not in c.active_offers():
            hookline = f" You don't have a bridal offer live yet — \"{bridal}\" is the natural anchor."
        body = lead + mid + hookline + " " + ask(c, "Want me to set up the pre-booking post now so it's ready? Reply YES.",
                                                 "Pre-booking post abhi set up kar doon? Reply YES.")
        return _finish(c, Draft(body=body, cta="binary_yes_no",
                                rationale="Festival trigger: judged the timing (early vs now) and picked the offer that actually fits the season.",
                                next_action="set up a festive pre-booking post", anchor=lead))
    # placeholder festival: seasonal judgement on the merchant's own numbers
    parts = [f"{c.sal}, planning note for {c.mname}:"]
    if now_item:
        parts.append(_dot(_first_sentence(now_item.get("summary", "")) or now_item.get("title", "")))
    if beat:
        parts.append(f"Next big window: {beat.get('month_range')} ({beat.get('note', '').split('—')[0].strip()}).")
    if hook:
        parts.append(f"Right now: {hook[0]}.")
    act = _dot(_first_sentence(now_item.get("actionable", ""))) if now_item and now_item.get("actionable") else ""
    if act:
        parts.append(f"Suggested move: {act}")
    body = " ".join(parts) + " " + ask(c, "Want me to draft the plan + first post for it? Reply YES.", "Plan + pehla post draft kar doon? Reply YES.")
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Festival trigger without payload: used this category's seasonal evidence + merchant's own numbers to recommend timing.",
                            next_action="draft the seasonal plan and first post", anchor=beat.get("month_range") if beat else "season"))


def t_ipl(c: Ctx) -> Draft:
    p = c.tp
    match, venue, t = p.get("match"), p.get("venue"), p.get("match_time_iso")
    when_dt = parse_dt(t)
    weeknight = p.get("is_weeknight")
    item = c.digest_of(("seasonal",), ("ipl",))
    dayname = when_dt.astimezone(IST).strftime("%A") if when_dt else None
    offers_today = c.offers_for(when=when_dt.astimezone(IST) if when_dt else None)
    restricted = [o for o in c.active_offers() if o not in offers_today]
    late = next((r for r in c.merchant.get("review_themes") or [] if "deliver" in str(r.get("theme", "")) and r.get("sentiment") == "neg"), None)
    lead = f"{c.sal}, {match or 'IPL match'}{' at ' + venue if venue else ''} tonight{', ' + fmt_time(t) if fmt_time(t) else ''}{' (' + dayname + ')' if dayname else ''}."
    insight = ""
    if item and weeknight is False:
        m = re.search(r"down (\d+)%", item.get("summary", ""))
        insight = (f" Heads-up from {item.get('source', 'order data')}: Saturday IPL home games cut dine-in covers {m.group(1)}% (people watch at home), and tonight is a weekend game too."
                   if m else " Weekend games tend to keep people at home.") + " Tonight is a delivery night, not a dine-in promo night."
        if offers_today:
            insight += f" Lead with \"{offers_today[0]}\"."
        elif restricted:
            insight += f" Your \"{restricted[0]}\" doesn't run today — worth a one-night weekend extension for the match."
        if late:
            insight += f" One caution: {num(late.get('occurrences_30d'))} recent reviews flag late delivery, so pad the promised time."
    elif item and weeknight:
        m = re.search(r"\+(\d+)% covers", item.get("summary", ""))
        insight = (f" Weeknight matches have driven +{m.group(1)}% covers ({item.get('source', '')})." if m else " Weeknight matches pull covers.")
        if offers_today:
            insight += f" Good night to push \"{offers_today[0]}\" for dine-in."
    body = lead + insight + " " + ask(c, "Want me to draft tonight's delivery banner + an Insta story? Ready in 10 min — reply YES.",
                                      "Aaj raat ka delivery banner + Insta story draft kar doon? 10 min mein ready — reply YES.")
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="IPL match today; checked day-of-week against order data and offer validity, flagged delivery-review risk.",
                            next_action="draft tonight's delivery banner and Instagram story", anchor=lead))


def t_review_theme(c: Ctx) -> Draft:
    p = c.tp
    theme, occ, quote, trend = p.get("theme"), p.get("occurrences_30d"), p.get("common_quote"), p.get("trend")
    if not theme:
        r = c.negative_review()
        if r:
            theme, occ, quote = r.get("theme"), r.get("occurrences_30d"), r.get("common_quote")
    if not theme:
        v = c.perf.get("views")
        body = (f"{c.sal}, a new theme is showing up in {poss(c.mname)} recent Google reviews."
                + (f" With {num(v)} people viewing your profile a month, those reviews are what they read first." if v else "")
                + " " + ask(c, "Want me to pull them together with calm draft replies you can approve? Reply YES.",
                            "Unhe ek jagah la kar shaant draft replies bana doon, aap approve kar dena? Reply YES."))
        return _finish(c, Draft(body=body, cta="binary_yes_no",
                                rationale="Review-theme trigger without theme detail: flagged honestly, no invented quotes/counts; reply-drafting offer.",
                                next_action="compile the recent reviews and draft replies", anchor="new review theme"))
    q = f" One says: \"{quote}\"." if quote else ""
    pos = c.positive_review()
    counter = (f" The good news: {num(pos.get('occurrences_30d'))} reviews praise {humanize(pos.get('theme'))} — worth leaning on in the replies."
               if pos and pos.get("occurrences_30d") else "")
    body = (f"{c.sal}, {num(occ) + ' ' if occ else ''}reviews in the last 30 days mention {humanize(theme)}"
            f"{' and the trend is ' + humanize(trend) if trend else ''}.{q}{counter} "
            + ask(c, "Want me to draft calm public replies to these + one fix you can announce? Reply YES.",
                  "In reviews ke liye shaant public replies + ek fix announcement draft kar doon? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Emerging review theme: verbatim quote as proof, protect rating with ready replies.",
                            next_action="draft public replies to the reviews and a fix announcement",
                            anchor=f"{occ} reviews on {humanize(theme)}"))


def t_competitor(c: Ctx) -> Draft:
    p = c.tp
    name, dist, their, opened = p.get("competitor_name"), p.get("distance_km"), p.get("their_offer"), p.get("opened_date")
    mine = c.best_offer()
    pos = c.positive_review()
    rp = c.agg.get("repeat_customer_pct") or c.agg.get("retention_6mo_pct") or c.agg.get("retention_3mo_pct")
    if pos and pos.get("common_quote"):
        edge = f"Don't race them on price — your reviews already say \"{pos['common_quote']}\" ({num(pos.get('occurrences_30d'))} mentions this month). "
    elif rp:
        edge = f"Your moat is loyalty: {pct(rp)} of your {c.nouns['people']} already come back. Don't race on price — remind people why they return. "
    else:
        edge = "Compete on trust and service, not a price race. "
    if name:
        lead = (f"{c.sal}, {name} opened {num(dist)} km from you" if dist else f"{c.sal}, {name} opened near you")
        lead += f" on {fmt_date(opened)}" if opened else ""
        lead += f", leading with \"{their}\"" if their else ""
        lead += f" (vs your \"{mine}\")." if (their and mine) else "."
    else:
        gap = c.ctr_gap()
        lead = f"{c.sal}, a new {c.nouns['pro']} listing has gone live near you."
        if gap:
            lead += f" Your click-through is {gap[0]} vs {gap[1]} for similar {c.nouns['pros']}, so first impressions matter more now."
    body = lead + " " + edge + ask(c, "Want me to draft a Google post that puts that front and centre? Reply YES.",
                                   "Ise highlight karta ek Google post draft kar doon? Reply YES.")
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="New competitor: loss-aversion with a differentiation play grounded in the merchant's own reviews/offer.",
                            next_action="draft a differentiation Google post", anchor=lead))


def t_curious_ask(c: Ctx) -> Draft:
    tr = c.matched_trend()
    pos = c.positive_review()
    guess = ""
    if pos and pos.get("occurrences_30d") and pos.get("common_quote"):
        guess = f" My guess from your reviews: {num(pos['occurrences_30d'])} this month praise your {humanize(pos.get('theme')).split()[0]}s — one says \"{pos['common_quote']}\"."
    elif tr:
        guess = f" My guess: {Ctx.trend_topic(tr)} — searches for it are up {pct(tr['delta_yoy'])} year-on-year."
    thread = c.open_thread()
    tail = ""
    what = {"restaurants": "dish", "pharmacies": "product", "gyms": "class", "dentists": "treatment"}.get(c.slug, "service")
    body = (f"Hi {c.owner or c.mname}! Quick one — which {what} have people asked for most at {c.mname} this week?{guess} "
            + ask(c, "Tell me and I'll turn it into a Google post + a ready reply for price questions — 5 minutes of your time.",
                  "Bas bata dijiye — main use Google post + price queries ke liye ready reply bana dungi, aapke bas 5 minute.")
            + tail)
    return _finish(c, Draft(body=body, cta="open_ended",
                            rationale="Curious-ask cadence: asking-the-merchant lever with an evidence-based guess from their own reviews/matched trend; reciprocity.",
                            next_action="turn the answer into a Google post and a price-reply template", anchor="weekly ask"))


def t_planning(c: Ctx) -> Draft:
    p = c.tp
    topic = humanize(p.get("intent_topic") or "the plan")
    last = p.get("merchant_last_message") or c.open_thread()
    prop = c.last_vera_proposal()
    spec = None
    if prop:
        for sent in _sentences(prop):
            if re.search(r"\d", sent) and not sent.strip().endswith("?"):
                spec = sent.strip()
    base_offer = next((o for o in c.active_offers() if any(w in o.lower() for w in topic.split() if len(w) > 3)), None)
    lines = []
    if spec and re.search(r"\bsuggest", spec, re.I) and "?" not in spec:
        clean = re.sub(r"^(great idea[^.]*\.\s*)?suggest(ed)?\s*", "", spec, flags=re.I).rstrip(".")
        lines.append(f"{c.sal}, here's the {topic} as a ready plan: {clean}.")
        lines.append(f"• Launch: a Google post + WhatsApp to your {c.nouns['people']} list, with a seat cap to create urgency")
        lines.append("• Booking: enquiries come straight to your WhatsApp")
        cta = ask(c, "I've drafted both — reply YES and I'll send them for your approval.",
                  "Dono draft ready hain — YES reply karein, main approval ke liye bhej deti hoon.")
    else:
        proof = f" (your {base_offer or 'current offer'} is the anchor)"
        if prop:
            m = re.search(r"(\d[\d,]*\s*(orders|covers|bookings|members)[^.—]*)", prop)
            if m:
                proof = f" — you already do {m.group(1).strip()}, so there's proven demand"
        anchor_offer = base_offer or c.best_offer()
        lines.append(f"{c.sal}, here's a first cut of the {topic}{proof}:")
        if anchor_offer:
            lines.append(f"• Price off your existing \"{anchor_offer}\", with a volume rate for bigger orders")
        lines.append(f"• Built for repeat orders from {c.locality or 'nearby'} offices/groups — one fixed order cut-off, one delivery window")
        lines.append("• Launch: a Google post + a WhatsApp note you can forward to office admins")
        cta = ask(c, "Want me to write the post + WhatsApp note now? Reply YES.", "Post + WhatsApp note abhi likh doon? Reply YES.")
    body = "\n".join(lines) + "\n" + cta
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale=f"Merchant already asked ({last!r}) — no more qualifying; handed over a concrete plan built on our earlier proposal/their own numbers, one next step.",
                            next_action=f"send the launch post and WhatsApp copy for the {topic}", anchor=topic))


def t_gbp_unverified(c: Ctx) -> Draft:
    p = c.tp
    upl = p.get("estimated_uplift_pct")
    path = humanize(p.get("verification_path", "")).replace(" or ", " or a ")
    body = (f"{c.sal}, {poss(c.mname)} Google profile is still unverified"
            + (f" — verified listings see roughly {pct(upl)} more visibility" if upl else "")
            + ". " + (f"Verification is by {path}; I can start it in about 5 min. " if path else "I can start it in about 5 min. ")
            + ask(c, "Shall I kick it off? Reply YES.", "Shuru kar doon? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Unverified profile: clear uplift estimate + effort externalised.",
                            next_action="start Google profile verification", anchor="unverified profile"))


def t_generic(c: Ctx) -> Draft:
    """Unknown kind or empty payload: lead with the single strongest merchant fact."""
    diag, act_en, act_hi, nxt = c.fix_lever()
    hook = c.own_hook()
    lead = f"{hook[0]}, and {diag}" if hook and diag not in hook[0] else (hook[0] if hook else diag)
    body = (f"{c.sal}, quick look at {c.mname}: {lead}. "
            + ask(c, f"Shall I {act_en}? Reply YES.", f"Main {act_hi}? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale=f"Trigger '{c.kind}' had no usable payload; anchored on the most fixable merchant signal instead of inventing detail.",
                            next_action=nxt, anchor=diag))


# ====================================================================== customer-facing

def _greet(c: Ctx) -> str:
    name = c.cust_parent or c.cust_name
    lang = c.cust_lang
    if lang == "hi" or c.senior:
        return f"Namaste {name + ' ji' if name else ''}".strip()
    if lang.endswith("-en") and lang[:2] in REGIONAL_GREETING:
        return f"{REGIONAL_GREETING[lang[:2]]} {name}".strip()
    return f"Hi {name}".strip()


def _from(c: Ctx) -> str:
    o, m = c.owner, c.mname
    if o and o.lower() in m.lower():
        return f"{m} here"
    if c.slug == "dentists" and o:
        return f"Dr. {o} at {m} here"
    return f"{o} from {m} here" if o else f"{m} here"


def _hi(c: Ctx) -> bool:
    return c.cust_lang in ("hi", "hinglish")


def _slots(c: Ctx) -> list[str]:
    p = c.tp
    s = p.get("available_slots") or p.get("next_session_options") or []
    out = []
    for x in s:
        if not isinstance(x, dict):
            continue
        iso = x.get("iso")
        if iso and parse_dt(iso):
            lbl = fmt_date(iso, True) + (", " + fmt_time(iso) if fmt_time(iso) and "T" in iso else "")
        else:
            lbl = x.get("label")
        if lbl:
            out.append(lbl)
    return out[:2]


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _pref_slot(c: Ctx) -> str | None:
    v = c.prefs.get("preferred_slots")
    if not v:
        return None
    words = humanize(v).split()
    return " ".join(w.capitalize() if w in _DAYS else w for w in words)


def _slot_cta(c: Ctx, slots: list[str]) -> tuple[str, str]:
    if len(slots) >= 2:
        if _hi(c):
            return (f"{slots[0]} ya {slots[1]} — reply 1 ya 2, ya apna time bata dijiye.", "multi_choice_slot")
        return (f"{slots[0]} or {slots[1]} — reply 1 or 2, or tell us a time that suits you.", "multi_choice_slot")
    if len(slots) == 1:
        return ((f"{slots[0]} ka slot hold kar dein? Reply YES." if _hi(c) else f"Shall we hold {slots[0]} for you? Reply YES."),
                "binary_yes_no")
    pref = _pref_slot(c)
    if pref:
        return ((f"Is hafte {pref} ka slot hold kar dein? Reply YES." if _hi(c)
                 else f"Shall we hold a {pref} slot for you this week? Reply YES."), "binary_yes_no")
    return (("Slot book kar dein? Reply YES." if _hi(c) else "Shall we book you in? Reply YES."), "binary_yes_no")


def _cust_draft(c: Ctx, body: str, cta: str, rationale: str, anchor: str) -> Draft:
    return _finish(c, Draft(body=body, cta=cta, send_as="merchant_on_behalf", rationale=rationale,
                            next_action="confirm the booking", anchor=anchor,
                            template_name=f"merchant_{c.kind}_v1",
                            template_params=[c.cust_parent or c.cust_name, c.mname, anchor]))


def _six_months(c: Ctx) -> str | None:
    lv = parse_dt(c.rel.get("last_visit"))
    if not lv:
        return None
    from datetime import timedelta
    return (lv + timedelta(days=182)).strftime("%B")


RECALL_DAYS = {"dentists": 150, "salons": 25, "gyms": 21, "pharmacies": 25, "restaurants": 14}
EMOJI = {"salons": " 🙂", "gyms": " 🙂", "restaurants": " 🙂", "dentists": "", "pharmacies": ""}


def t_recall(c: Ctx) -> Draft:
    p = c.tp
    svc = humanize(p.get("service_due") or "").replace("6 month", "6-month")
    days = c.days_since_last_visit()
    last = fmt_date(p.get("last_service_date") or c.rel.get("last_visit"))
    due_claim = bool(p.get("service_due") or p.get("due_date")) or (days is not None and days >= RECALL_DAYS.get(c.slug, 30))
    if not svc:
        svc = c.nouns["visit"]
    offs = c.offers_for("existing", prefer=[w for w in svc.split() if len(w) > 3] + ["clean", "check"])
    off = offs[0] if offs else None
    slots = _slots(c)
    cta_line, cta = _slot_cta(c, slots)
    who = f"{c.cust_name}'s" if c.cust_parent else "your"
    months = c.months_since_last_visit()
    if not due_claim and c.slug == "dentists" and _six_months(c):
        cta_line, cta = (("Abhi pre-book kar dein? Reply YES." if _hi(c) else "Shall we pre-book it? Reply YES."), "binary_yes_no")
    if c.slug == "gyms" and not p.get("service_due"):
        gym_core = (f"{last} ke baad aapko miss kiya!" if _hi(c) else f"We've missed you since {last} — your spot is still here.") if last else ""
    else:
        gym_core = ""
    if _hi(c):
        if gym_core:
            core = gym_core
        elif due_claim:
            core = f"{'Aapka' if who == 'your' else who} {svc} due hai" + (f" — last visit {last} ko tha" if last else "") + "."
        else:
            core = f"{last + ' wali visit ke baad ' if last else ''}ek chhota sa follow-up."
        body = f"{_greet(c)}, {_from(c)}{EMOJI.get(c.slug, '')}. {core} " + (f"{off} abhi chal raha hai. " if off else "") + cta_line
    else:
        if gym_core:
            core = gym_core
        elif due_claim:
            since = f"It's been about {months} months since {who} last visit, so " if months and months >= 2 else ""
            core = f"{since}{who} {svc} is due" + (f" (last visit {last})" if last and not since else "") + "."
        else:
            nm6 = _six_months(c) if c.slug == "dentists" else None
            core = (f"A quick follow-up on {who} visit{' on ' + last if last else ''} — {who} next 6-monthly check-up falls around {nm6}, and we can pre-book it now."
                    if nm6 else f"A quick follow-up on {who} visit{' on ' + last if last else ''}.")
        body = f"{_greet(c)}, {_from(c)}{EMOJI.get(c.slug, '')}. {core[0].upper() + core[1:]} " + (f"{off} is on right now. " if off else "") + cta_line
    return _cust_draft(c, body, cta, "Customer recall on behalf of merchant: real last-visit date, slots/offer only if they exist, 'due' claimed only when the interval supports it; language preference honoured.",
                       f"{svc}")


def t_appointment(c: Ctx) -> Draft:
    p = c.tp
    when = p.get("appointment_iso") or p.get("slot_iso")
    tlabel = " ".join(x for x in (fmt_date(when, True), fmt_time(when)) if x) if when else None
    svc = humanize(p.get("service") or "") or None
    appt = c.nouns["appt"]
    if _hi(c):
        body = (f"{_greet(c)}, {_from(c)}. Reminder: aapka {appt} {tlabel or 'kal'} hai"
                + (f" ({svc})" if svc else "") + ". Reply YES to confirm, ya naya time bata dijiye.")
    else:
        body = (f"{_greet(c)}, {_from(c)}. A quick reminder that your {appt} is {tlabel or 'tomorrow'}"
                + (f" for {svc}" if svc else "") + ". Reply YES to confirm, or tell us if you need a different time.")
    return _cust_draft(c, body, "binary_yes_no", "Appointment reminder — reduces no-shows; one-tap confirm, reschedule path open.", "appointment tomorrow")


def t_refill(c: Ctx) -> Draft:
    p = c.tp
    if c.slug and c.slug != "pharmacies" and not p.get("molecule_list"):
        d = t_recall(c)
        d.rationale = (f"Refill-type trigger on a {c.nouns['pro']} (not a pharmacy): framed as a routine "
                       f"{c.nouns['visit']} follow-up instead of a medicine refill. " + d.rationale)
        return d
    mols = p.get("molecule_list") or []
    runout = fmt_date(p.get("stock_runs_out_iso"))
    saved = p.get("delivery_address_saved")
    via = "via" in str(c.prefs.get("channel", ""))
    nm = c.cust_name
    senior_off = next((o for o in c.active_offers() if "senior" in o.lower()), None) if c.senior else None
    deliv = next((o for o in c.active_offers() if "deliver" in o.lower()), None)
    whose_hi = f"{nm} ji ki" if (via or c.senior) and nm else "Aapki"
    whose_en = f"{nm}'s" if via and nm else "Your"
    if _hi(c):
        greet = "Namaste" if via else _greet(c)
        body = (f"{greet} 🙏 {c.mname} se refill reminder: "
                + (f"{whose_hi} {len(mols)} regular medicines ({', '.join(mols)}) " if mols else f"{whose_hi} regular medicines ")
                + (f"{runout} tak khatam ho jayengi. " if runout else "refill ke liye due hain. ")
                + "Same dose, same pack ready rakhenge. "
                + (f"{senior_off} lagega" + (f", aur {deliv.lower()}. " if deliv else ". ") if senior_off else (f"{deliv}. " if deliv else ""))
                + ("Saved address pe bhej dein? " if saved else "")
                + "Reply CONFIRM, ya dose mein koi badlav ho to bata dijiye.")
    else:
        greet = "Hello" if via else _greet(c)
        body = (f"{greet}, {c.mname} here. "
                + (f"{whose_en} {len(mols)} regular medicines ({', '.join(mols)}) " if mols else f"{whose_en} regular medicines ")
                + (f"run out on {runout}. " if runout else "are due for a refill. ")
                + "We can keep the same dose and pack ready. "
                + (f"{senior_off} applies" + (f", plus {deliv}. " if deliv else ". ") if senior_off else (f"{deliv}. " if deliv else ""))
                + ("Deliver to the saved address? " if saved else "")
                + "Reply CONFIRM, or tell us if anything in the prescription changed.")
    d = _cust_draft(c, body, "binary_confirm_cancel", "Chronic refill: exact molecules + run-out date; existing senior/delivery offers; addressed correctly when the chat goes via a family member; dosage-change safety valve.",
                    f"refill due {runout}")
    d.next_action = "dispatch the refill"
    return d


def t_lapsed(c: Ctx) -> Draft:
    p = c.tp
    days = p.get("days_since_last_visit") or c.days_since_last_visit()
    focus = humanize(p.get("previous_focus") or c.prefs.get("training_focus") or c.prefs.get("health_focus") or "")
    months_in = p.get("previous_membership_months")
    offs = c.offers_for("existing", prefer=["free", "analysis", "check", "combo"])
    if c.slug == "gyms" and c.rel.get("visits_total", 0) and not offs:
        offs = c.offers_for("any", prefer=["trial", "free"])
    off = offs[0] if offs else None
    weeks = round(days / 7) if isinstance(days, (int, float)) and days >= 14 else None
    if weeks:
        c.derived.append(weeks)
    gap = f"about {weeks} weeks" if weeks else (f"{days} days" if days else "a while")
    pref = _pref_slot(c)
    slots = _slots(c)
    if c.slug == "pharmacies":
        deliv = next((o for o in c.active_offers() if "deliver" in o.lower()), None)
        if _hi(c):
            body = (f"{_greet(c)}, {_from(c)}. Kaafi time se aapka order nahi aaya — sab theek? "
                    + (f"{deliv} available hai. " if deliv else "")
                    + "Kuch chahiye to bas list yahin bhej dijiye — hum ready rakhenge.")
        else:
            body = (f"{_greet(c)}, {_from(c)}. It's been {gap} since your last order — hope all's well. "
                    + (f"{deliv} is available. " if deliv else "")
                    + "Need anything? Just send your list here and we'll keep it ready.")
        return _cust_draft(c, body, "open_ended", "Pharmacy re-engagement: no guilt, no invented offers; easy reorder path.", f"{gap} since last order")
    if c.slug == "dentists":
        last = fmt_date(c.rel.get("last_visit"))
        nxt_m = _six_months(c)
        body = (f"{_greet(c)}, {_from(c)}. Following up on your visit{' on ' + last if last else ''} — "
                + (f"your next 6-monthly check-up falls around {nxt_m}; we can pre-book it now so it's one less thing to remember. " if (nxt_m and not _hi(c))
                   else (f"aapka next 6-monthly check-up {nxt_m} ke aas-paas hai — abhi pre-book kar lein? " if nxt_m else "next check-up pre-book kar lein? "))
                + (f"{off}. " if off else "")
                + (f"Shall we hold a {pref} slot? Reply YES." if pref and not _hi(c) else ("Slot hold kar dein? Reply YES." if _hi(c) else "Shall we hold a slot? Reply YES.")))
        return _cust_draft(c, body, "binary_yes_no", "Clinical follow-up: no lapse/guilt framing; care-first reason and a single booking step.", "follow-up")
    streak = f" You'd built a solid {num(months_in)}-month run" + (f" toward your {focus} goal" if focus else "") + " — easy to pick back up." if months_in else ""
    em = EMOJI.get(c.slug, "")
    if _hi(c):
        body = (f"{_greet(c)}, {_from(c)}{em}. {gap[0].upper() + gap[1:]} ho gaye — wapas aana bilkul easy hai."
                + (f" Aapke {focus} goal ke liye" if focus else "") + (f" {off} available hai." if off else "")
                + (f" {slots[0]} ka slot hold kar dein?" if slots else (f" {pref} slot hold kar dein?" if pref else " Ek slot hold kar dein?"))
                + " Reply YES — koi pressure nahi.")
    else:
        body = (f"{_greet(c)}, {_from(c)}{em}. It's been {gap}.{streak}"
                + (f" You can restart with our {off} offer." if off else "")
                + (f" Shall we hold {slots[0]}?" if slots else (f" Shall we hold a {pref} slot this week?" if pref else " Shall we hold a slot this week?"))
                + " Reply YES — no pressure, no auto-charge.")
    return _cust_draft(c, body, "binary_yes_no", "Lapsed customer win-back: warm, no guilt, past goal/streak from their data, an offer suited to returning customers, one easy yes.", f"{gap} since last visit")


def t_trial_followup(c: Ctx) -> Draft:
    p = c.tp
    td = fmt_date(p.get("trial_date") or c.rel.get("last_visit"))
    trial = c.nouns["trial"]
    slots = _slots(c)
    who = f"{c.cust_name}'s" if c.cust_parent else "your"
    offs = c.offers_for("any", prefer=["month", "trial", "first"])
    off = offs[0] if offs else None
    nxt = {"gyms": "next class", "salons": "next appointment", "dentists": "follow-up visit"}.get(c.slug, "next visit")
    em = EMOJI.get(c.slug, "")
    if _hi(c):
        body = (f"{_greet(c)}, {_from(c)}{em}. {td + ' ko ' if td else ''}{'aapka' if who == 'your' else who} {trial} hua tha — umeed hai accha laga! "
                + (f"Aage continue karne ke liye {off}. " if off else "")
                + (f"{nxt[0].upper() + nxt[1:]} {slots[0]} — hold kar dein? Reply YES." if slots else f"{nxt[0].upper() + nxt[1:]} hold kar dein? Reply YES."))
    else:
        body = (f"{_greet(c)}, {_from(c)}{em}. Hope {who} {trial}{' on ' + td if td else ''} went well! "
                + (f"To keep going, {off} is available. " if off else "")
                + (f"The {nxt} is {slots[0]} — shall we hold the spot? Reply YES." if slots else f"Shall we book the {nxt}? Reply YES."))
    return _cust_draft(c, body, "binary_yes_no", "Trial follow-up: momentum from the trial date + a concrete next step.", "trial follow-up")


def t_bridal(c: Ctx) -> Draft:
    p = c.tp
    wd = p.get("wedding_date") or c.prefs.get("wedding_date")
    dtw = p.get("days_to_wedding")
    if dtw is None and wd:
        dtw = days_between(c.now, parse_dt(wd))
        if dtw is not None:
            c.derived.append(dtw)
    step = humanize(p.get("next_step_window_open") or "")
    m = re.search(r"\s*(\d+)\s*day\b", step)
    if m:
        step = f"{m.group(1)}-day " + (step[:m.start()] + step[m.end():]).strip()
    trial = fmt_date(p.get("trial_completed"))
    off = c.best_offer(["bridal", "spa", "facial"])
    body = (f"{_greet(c)} 💍 {_from(c)}. "
            + (f"{num(dtw)} days to your wedding on {fmt_date(wd)} — " if dtw and wd else "")
            + (f"right window to begin the {step}" if step else "a good time to plan your prep")
            + (f", building on your trial from {trial}" if trial else "") + ". "
            + (f"{off} is on right now. " if off else "")
            + (f"Shall we block a {_pref_slot(c)} slot for the first session? Reply YES." if _pref_slot(c)
               else "Shall we block your first session? Reply YES."))
    return _cust_draft(c, body, "binary_yes_no", "Bridal follow-up: wedding-date countdown + next prep step, preference-honouring single CTA.", "bridal prep window")


def t_customer_to_merchant(c: Ctx) -> Draft:
    """Customer-scoped trigger but no customer context: brief the merchant with the trigger's facts, never invent the rest."""
    p = c.tp
    n = c.nouns
    nm = c.cust_name
    who = nm or f"one of your {n['people']}"
    pron = "them"
    slots = _slots(c)
    kind = c.kind
    off = c.best_offer()
    if kind == "recall_due":
        svc = humanize(p.get("service_due") or "") or n["visit"]
        last = fmt_date(p.get("last_service_date"))
        fact = f"{who}'s {svc} is due" + (f" (last visit {last})" if last else "") + "."
        offer = off
    elif kind == "chronic_refill_due":
        mols = p.get("molecule_list") or []
        ro = fmt_date(p.get("stock_runs_out_iso"))
        fact = (f"a chronic-Rx customer's refill ({', '.join(mols)})" if mols else f"{who}'s regular refill") + \
               (f" runs out on {ro}" if ro else " is due") + (" — delivery address is already saved." if p.get("delivery_address_saved") else ".")
        offer = c.best_offer(["delivery", "senior", "refill"])
    elif kind == "wedding_package_followup":
        wd, dtw, step = fmt_date(p.get("wedding_date")), p.get("days_to_wedding"), humanize(p.get("next_step_window_open") or "")
        m = re.search(r"\s*(\d+)\s*day\b", step)
        if m:
            step = f"{m.group(1)}-day " + (step[:m.start()] + step[m.end():]).strip()
        fact = (f"{who}'s wedding is on {wd}" + (f" ({num(dtw)} days out)" if dtw else "") if wd else f"{who} is in the bridal prep window") + \
               (f" and the {step} window is open now" if step else "") + \
               (f" — trial done {fmt_date(p.get('trial_completed'))}." if p.get("trial_completed") else ".")
        offer = c.best_offer(["bridal", "spa", "facial"])
    elif kind in ("customer_lapsed_hard", "customer_lapsed_soft", "winback_eligible"):
        d, focus = p.get("days_since_last_visit"), humanize(p.get("previous_focus") or "")
        months = p.get("previous_membership_months")
        fact = (f"{who} hasn't visited in {num(d)} days" if d else f"{who} has gone quiet") + \
               (f" — was {num(months)} months in with a {focus} goal." if months and focus else (f" — goal was {focus}." if focus else "."))
        offer = c.best_offer(["trial", "free", "first"])
    elif kind == "trial_followup":
        td = fmt_date(p.get("trial_date"))
        fact = f"{who}'s {n['trial']}" + (f" was on {td}" if td else " is done") + "."
        offer = off
    elif kind == "appointment_tomorrow":
        fact = f"{who} has a {n['appt']} tomorrow."
        offer = None
    else:
        fact = f"{who}: {humanize(kind)}."
        offer = off
    extra = f" Next open slot{'s' if len(slots) > 1 else ''}: {' / '.join(slots)}." if slots else ""
    offer_txt = f" with your \"{offer}\"" if offer else ""
    body = (f"{c.sal}, {fact}{extra} "
            + ask(c, f"Want me to send {nm or pron} a {'reminder' if kind != 'wedding_package_followup' else 'follow-up'} from your number{offer_txt}? Reply YES.",
                  f"Aapke number se {nm or 'unhe'} reminder bhej doon{offer_txt}? Reply YES."))
    return _finish(c, Draft(body=body, cta="binary_yes_no",
                            rationale="Customer-scoped trigger arrived without the customer's profile: briefed the merchant with the trigger's own facts and asked before messaging the customer (no guessed details, consent unknown).",
                            next_action=f"send {nm or 'the customer'} a reminder on the merchant's behalf", anchor=fact))


# ====================================================================== dispatch

MERCHANT_BUILDERS = {
    "research_digest": t_research, "category_research_digest_release": t_research,
    "category_trend_movement": t_dormant, "trend": t_dormant,
    "regulation_change": t_compliance, "compliance_alert": t_compliance,
    "cde_opportunity": t_cde,
    "supply_alert": t_supply_alert,
    "category_seasonal": t_category_seasonal, "weather_heatwave": t_category_seasonal,
    "perf_dip": t_perf_dip,
    "perf_spike": t_perf_spike,
    "seasonal_perf_dip": t_seasonal_dip,
    "milestone_reached": t_milestone,
    "renewal_due": t_renewal,
    "winback_eligible": t_winback_merchant,
    "dormant_with_vera": t_dormant,
    "festival_upcoming": t_festival,
    "ipl_match_today": t_ipl,
    "review_theme_emerged": t_review_theme,
    "competitor_opened": t_competitor,
    "curious_ask_due": t_curious_ask, "scheduled_recurring": t_curious_ask,
    "active_planning_intent": t_planning,
    "gbp_unverified": t_gbp_unverified,
}

CUSTOMER_BUILDERS = {
    "recall_due": t_recall,
    "appointment_tomorrow": t_appointment,
    "chronic_refill_due": t_refill,
    "customer_lapsed_hard": t_lapsed, "customer_lapsed_soft": t_lapsed, "winback_eligible": t_lapsed,
    "trial_followup": t_trial_followup,
    "wedding_package_followup": t_bridal,
}


def build_draft(c: Ctx) -> Draft:
    kind = c.kind
    is_customer = c.trigger.get("scope") == "customer" or bool(c.trigger.get("customer_id"))
    if is_customer:
        if not c.customer:
            return t_customer_to_merchant(c)
        if not c.consent_ok():
            return Draft(body="", skip=True, skip_reason="customer has not opted in to merchant outreach")
        fn = CUSTOMER_BUILDERS.get(kind, t_recall if "recall" in kind else t_lapsed)
        return fn(c)
    fn = MERCHANT_BUILDERS.get(kind)
    if fn is None:
        if "research" in kind or "digest" in kind:
            fn = t_research
        elif "regulat" in kind or "compliance" in kind:
            fn = t_compliance
        elif "dip" in kind:
            fn = t_perf_dip
        elif "spike" in kind:
            fn = t_perf_spike
        else:
            fn = t_generic
    return fn(c)


# ---------------------------------------------------------------------- text helpers

_ABBR = ("dr.", "mr.", "mrs.", "ms.", "p.", "vs.", "st.", "no.", "approx.", "e.g.", "i.e.")


def _sentences(text: str) -> list[str]:
    raw = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text or "") if x.strip()]
    out: list[str] = []
    for part in raw:
        if out and (out[-1].lower().endswith(_ABBR) or re.search(r"\b[A-Z]\.$", out[-1])):
            out[-1] = out[-1] + " " + part
        else:
            out.append(part)
    return out


def _dot(text: str) -> str:
    t = (text or "").strip()
    return t if not t or t[-1] in ".!?" else t + "."


def _first_sentence(text: str, skip_if: tuple = ()) -> str:
    for s in _sentences(text):
        if not any(k in s.lower() for k in skip_if):
            return s
    return ""


def poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def pretty_dates(text: str) -> str:
    """'circular 2026-11-04' -> 'circular 4 Nov 2026'."""
    def f(m):
        dt = parse_dt(m.group(0))
        return f"{dt.day} {dt.strftime('%b')} {dt.year}" if dt else m.group(0)
    return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", f, text or "")


def _date_in(text: str) -> str | None:
    m = re.search(r"\d{4}-\d{2}-\d{2}", text or "")
    return m.group(0) if m else None
