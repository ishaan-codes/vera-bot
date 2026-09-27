#!/usr/bin/env python3
"""Run this on YOUR laptop (not needed on the server): verifies the Gemini key, finds a
working endpoint + model, and times one real composition.

  python tools/check_gemini.py            # reads GEMINI_API_KEY from env or bot/.env
"""
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

import urllib.request  # noqa: E402

key = os.environ.get("GEMINI_API_KEY", "")
if not key:
    sys.exit("GEMINI_API_KEY not set (put it in bot/.env as GEMINI_API_KEY=...)")

print("1) Listing models visible to this key (AI Studio endpoint)...")
try:
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                 headers={"x-goog-api-key": key})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    names = [m["name"].split("/")[-1] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in names if "flash" in n]
    print("   OK. Flash models:", ", ".join(flash[:12]) or "(none)")
except Exception as e:
    print("   AI Studio listing failed:", str(e)[:200], "\n   (fine if this is a Vertex express key — step 2 tries both)")

from vera.llm import GEMINI  # noqa: E402
print(f"\n2) Calling {GEMINI.models} (mode={GEMINI.mode})...")
t = time.time()
try:
    out = GEMINI.generate("Reply with JSON only.", 'Return {"ok": true, "model": "<your model name>"}', timeout=20)
    print(f"   OK in {time.time()-t:.1f}s -> {out.strip()[:120]}\n   working model={GEMINI.model} mode={GEMINI.mode}")
except Exception as e:
    sys.exit(f"   FAILED: {e}\n   Check the key in Google AI Studio (aistudio.google.com/apikey) or set GEMINI_MODEL.")

print("\n3) Composing one real message (Dr. Meera research digest)...")
from vera.compose import compose  # noqa: E402
pack = os.path.join(os.path.dirname(ROOT), "pack", "dataset")
if not os.path.isdir(pack):
    pack = os.path.join(ROOT, "dataset")
cat = json.load(open(os.path.join(pack, "categories", "dentists.json")))
mer = [m for m in json.load(open(os.path.join(pack, "merchants_seed.json")))["merchants"] if m["merchant_id"].startswith("m_001")][0]
trg = [t for t in json.load(open(os.path.join(pack, "triggers_seed.json")))["triggers"] if t["id"].startswith("trg_001")][0]
t = time.time()
res = compose(cat, mer, trg, None)
print(f"   {time.time()-t:.1f}s  (LLM used: {'yes' if GEMINI.stats['ok'] >= 2 else 'no — template fallback'})\n   BODY: {res['body']}\n   WHY:  {res['rationale']}")
print("\nStats:", GEMINI.stats)
