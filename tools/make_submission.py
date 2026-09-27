#!/usr/bin/env python3
"""Generate submission.jsonl for the 30 canonical test pairs (challenge brief §7.2).

  python tools/make_submission.py --data <expanded_dir> [--out submission.jsonl] [--now 2026-04-26T10:30:00Z] [--no-llm]

Uses the same compose() as the live bot. With GEMINI_API_KEY set (env or .env) the Gemini
rewrite layer is used; otherwise the deterministic templates are emitted.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
env = os.path.join(ROOT, ".env")
if os.path.exists(env):
    for line in open(env):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

from vera.compose import compose            # noqa: E402
from vera.util import parse_dt              # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="expanded dataset dir (from generate_dataset.py)")
    ap.add_argument("--out", default=os.path.join(ROOT, "submission.jsonl"))
    ap.add_argument("--now", default="2026-04-26T10:30:00Z")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    d = a.data
    pairs = json.load(open(os.path.join(d, "test_pairs.json")))["pairs"]
    ld = lambda sub, i: json.load(open(os.path.join(d, sub, f"{i}.json"))) if i and os.path.exists(os.path.join(d, sub, f"{i}.json")) else None
    now = parse_dt(a.now)
    n_llm = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for p in pairs:
            trg = ld("triggers", p["trigger_id"])
            mer = ld("merchants", p["merchant_id"])
            cus = ld("customers", p.get("customer_id"))
            cat = ld("categories", mer["category_slug"])
            t = time.time()
            out = compose(cat, mer, trg, cus, now=now, use_llm=not a.no_llm)
            row = {"test_id": p["test_id"], **out}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{p['test_id']} [{trg['kind']}] {time.time()-t:.1f}s\n  {out['body']}\n")
    print(f"wrote {len(pairs)} rows to {a.out}")


if __name__ == "__main__":
    main()
