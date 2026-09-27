#!/usr/bin/env python3
"""Local end-to-end harness (stdlib only). Mimics the judge's lifecycle against a running bot.

  python tools/local_harness.py --bot http://localhost:8080 --data <expanded_dir> [--out harness_out.jsonl]

Checks: endpoint contract (idempotency 409, bad scope 400, malformed 400), warmup counts,
a 12-tick test window with mid-test injections, action schema, latency, anti-repetition,
reply scenarios (auto-reply, intent, hostile, off-topic, question, later, customer slot pick),
and a 10 rps burst.
"""
import argparse
import glob
import json
import os
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

REQ_ACTION = ["conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
              "template_params", "body", "cta", "suppression_key", "rationale"]
FAILS = []


def call(bot, method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(bot + path, data=data, method=method, headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), (time.time() - t) * 1000
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), (time.time() - t) * 1000
        except Exception:
            return e.code, None, (time.time() - t) * 1000


def expect(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load(data_dir):
    rd = lambda sub: [json.load(open(f)) for f in sorted(glob.glob(os.path.join(data_dir, sub, "*.json")))]
    return rd("categories"), rd("merchants"), rd("customers"), rd("triggers")


def push(bot, scope, cid, payload, version=1):
    return call(bot, "POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version,
                                             "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="http://localhost:8080")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="harness_out.jsonl")
    a = ap.parse_args()
    bot = a.bot.rstrip("/")
    cats, mers, cuss, trgs = load(a.data)
    out = open(a.out, "w")

    print("== contract")
    call(bot, "POST", "/v1/teardown", {})
    s, b, _ = call(bot, "GET", "/v1/healthz"); expect(s == 200 and b.get("status") == "ok", "healthz 200")
    s, b, _ = call(bot, "GET", "/v1/metadata"); expect(s == 200 and b.get("team_name"), "metadata 200")
    s, b, _ = call(bot, "POST", "/v1/context", {"scope": "nope", "context_id": "x", "version": 1, "payload": {}})
    expect(s == 400 and b.get("reason") == "invalid_scope", "bad scope -> 400 invalid_scope")
    req = urllib.request.Request(bot + "/v1/context", data=b"{not json", method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    expect(code == 400, "malformed json -> 400")

    print("== warmup")
    t = time.time()
    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(lambda x: push(bot, *x), [("category", c["slug"], c) for c in cats] +
                          [("merchant", m["merchant_id"], m) for m in mers] +
                          [("customer", c["customer_id"], c) for c in cuss]))
    expect(all(r[0] == 200 for r in res), f"{len(res)} base contexts accepted ({time.time()-t:.1f}s)")
    s, b, _ = push(bot, "merchant", mers[0]["merchant_id"], mers[0], 1)
    expect(s == 409 and b.get("reason") == "stale_version", "same version re-push -> 409 stale_version")
    s, b, _ = call(bot, "GET", "/v1/healthz")
    expect(b["contexts_loaded"] == {"category": len(cats), "merchant": len(mers), "customer": len(cuss), "trigger": 0},
           f"healthz counts {b['contexts_loaded']}")

    print("== test window (12 ticks)")
    order = trgs[:]
    waves = [order[i::12] for i in range(12)]
    all_actions, lat = [], []
    bodies_by_conv = {}
    cat_by_slug = {c["slug"]: c for c in cats}
    mer_by_id = {m["merchant_id"]: m for m in mers}
    for i, wave in enumerate(waves):
        now = f"2026-04-26T{10 + (i * 5) // 60:02d}:{(i * 5) % 60:02d}:00Z"
        for tr in wave:
            push(bot, "trigger", tr["id"], tr)
        if i == 4:   # mid-test injection: new digest item + perf shift
            cat = json.loads(json.dumps(cat_by_slug["dentists"]))
            cat["digest"].append({"id": "d_INJ_dci_sterilization", "kind": "compliance",
                                  "title": "DCI mandates Class B autoclave logs from 2026-07-01",
                                  "source": "DCI circular 2026-04-20",
                                  "summary": "Every sterilization cycle must be logged with cycle number and indicator result. Inspections begin July.",
                                  "actionable": "Start a cycle log this week"})
            s, b, _ = push(bot, "category", "dentists", cat, 2); expect(s == 200, "digest injection v2 accepted")
            m = json.loads(json.dumps(mer_by_id["m_002_bharat_dentist_mumbai"]))
            m["performance"]["calls"] = 2; m["performance"]["delta_7d"]["calls_pct"] = -0.7
            s, b, _ = push(bot, "merchant", m["merchant_id"], m, 2); expect(s == 200, "perf injection v2 accepted")
            inj = {"id": "trg_INJ_compliance_sterilization", "scope": "merchant", "kind": "regulation_change",
                   "source": "external", "merchant_id": "m_002_bharat_dentist_mumbai", "customer_id": None,
                   "payload": {"category": "dentists", "top_item_id": "d_INJ_dci_sterilization", "deadline_iso": "2026-07-01"},
                   "urgency": 4, "suppression_key": "compliance:sterilization:2026", "expires_at": "2026-07-01T00:00:00Z"}
            push(bot, "trigger", inj["id"], inj)
            wave = wave + [inj]
        ids = [w["id"] for w in wave]
        s, b, ms = call(bot, "POST", "/v1/tick", {"now": now, "available_triggers": ids})
        lat.append(ms)
        acts = (b or {}).get("actions", [])
        expect(s == 200 and len(acts) <= 20, f"tick {i+1}: {len(acts)}/{len(ids)} actions in {ms:.0f}ms")
        for act in acts:
            missing = [k for k in REQ_ACTION if k not in act]
            if missing:
                expect(False, f"action missing {missing}")
            if not act.get("body"):
                expect(False, "empty body")
            prev = bodies_by_conv.setdefault(act["conversation_id"], set())
            if act["body"] in prev:
                expect(False, "repeated body in conversation")
            prev.add(act["body"])
            all_actions.append(act)
            out.write(json.dumps(act, ensure_ascii=False) + "\n")
        if i == 4:
            inj_act = [x for x in acts if x["trigger_id"] == "trg_INJ_compliance_sterilization"]
            expect(bool(inj_act) and "autoclave" in inj_act[0]["body"].lower(), "injected digest item used in next send")
    # re-tick everything: suppression keys must prevent duplicates
    s, b, _ = call(bot, "POST", "/v1/tick", {"now": "2026-04-26T11:05:00Z", "available_triggers": [t["id"] for t in trgs]})
    dup = [x for x in b.get("actions", []) if x["suppression_key"] in {y["suppression_key"] for y in all_actions}]
    expect(not dup, f"no re-send of already-sent suppression keys ({len(b.get('actions', []))} deferred actions sent on re-tick)")
    all_actions += b.get("actions", [])
    for act in b.get("actions", []):
        out.write(json.dumps(act, ensure_ascii=False) + "\n")
    print(f"  total actions {len(all_actions)}; max tick latency {max(lat):.0f}ms")
    cust = [x for x in all_actions if x["send_as"] == "merchant_on_behalf"]
    print(f"  customer-facing: {len(cust)}  merchant-facing: {len(all_actions) - len(cust)}")

    print("== replies")
    conv = next(x for x in all_actions if x["merchant_id"] == "m_001_drmeera_dentist_delhi" and x["send_as"] == "vera")
    cid = conv["conversation_id"]
    s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": cid, "merchant_id": conv["merchant_id"], "from_role": "merchant",
                                              "message": "Interesting. How long will it take?", "received_at": "2026-04-26T11:10:00Z", "turn_number": 2})
    expect(s == 200 and b["action"] == "send", f"question -> send: {b.get('body', '')[:120]}")
    s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": cid, "merchant_id": conv["merchant_id"], "from_role": "merchant",
                                              "message": "Btw can you also help me with my GST filing this month?", "turn_number": 3})
    expect(b["action"] == "send" and ("ca" in b["body"].lower() or "outside" in b["body"].lower()), f"GST curveball -> polite redirect: {b.get('body', '')[:120]}")
    s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": cid, "merchant_id": conv["merchant_id"], "from_role": "merchant",
                                              "message": "Ok let's do it", "turn_number": 4})
    bad = any(w in b.get("body", "").lower() for w in ["would you", "do you", "can you tell", "what if", "how about"])
    expect(b["action"] == "send" and not bad, f"commit -> action mode: {b.get('body', '')[:140]}")
    mid2 = "m_003_studio11_salon_hyderabad"
    for i in range(1, 5):
        s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": f"conv_ar_{i}", "merchant_id": mid2, "from_role": "merchant",
                                                  "message": "Thank you for contacting Studio11! Our team will respond shortly.", "turn_number": i + 1})
        print(f"  auto-reply turn {i}: {b['action']}")
        if b["action"] == "end":
            break
    expect(b["action"] == "end", "auto-reply loop ends by turn 3-4")
    s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": "conv_h", "merchant_id": mid2, "from_role": "merchant",
                                              "message": "Why are you bothering me. This is useless.", "turn_number": 2})
    expect(b["action"] in ("end", "send") and (b["action"] == "end" or "sorry" in b["body"].lower() or "maaf" in b["body"].lower()),
           f"hostile -> {b['action']}")
    s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": "conv_h", "merchant_id": mid2, "from_role": "merchant",
                                              "message": "Stop sending these.", "turn_number": 3})
    expect(b["action"] == "end", "explicit stop -> end")
    s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": "conv_l", "merchant_id": "m_005_pizzajunction_restaurant_delhi",
                                              "from_role": "merchant", "message": "busy right now, baad mein baat karte hain", "turn_number": 2})
    expect(b["action"] == "wait", f"'later' -> wait {b.get('wait_seconds')}")
    c_act = next((x for x in cust if x.get("cta") == "multi_choice_slot"), cust[0] if cust else None)
    if c_act:
        s, b, _ = call(bot, "POST", "/v1/reply", {"conversation_id": c_act["conversation_id"], "merchant_id": c_act["merchant_id"],
                                                  "customer_id": c_act["customer_id"], "from_role": "customer", "message": "2", "turn_number": 2})
        expect(b["action"] == "send", f"customer slot pick -> {b.get('body', '')[:100]}")

    print("== burst 10 rps")
    t = time.time()
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: call(bot, "GET", "/v1/healthz")[0], range(50)))
    expect(all(c == 200 for c in codes), f"50 healthz in {time.time()-t:.2f}s")
    out.close()
    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + '; '.join(FAILS)}\nactions written to {a.out}")


if __name__ == "__main__":
    main()
