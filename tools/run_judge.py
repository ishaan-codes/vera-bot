#!/usr/bin/env python3
"""Run magicpin's official judge_simulator.py against your bot, with Gemini as the judge LLM,
without editing the simulator file.

  python tools/run_judge.py --bot http://localhost:8080 --sim ../pack/judge_simulator.py --scenario all
  python tools/run_judge.py --bot https://<your-app>.onrender.com --scenario full_evaluation

Scenarios: warmup, phase2_short, auto_reply_hell, intent_transition, hostile, all, full_evaluation.
Tip: restart the bot (or POST /v1/teardown) between runs — re-pushing the same context version returns 409 by design.
"""
import argparse
import importlib.util
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
env = os.path.join(ROOT, ".env")
if os.path.exists(env):
    for line in open(env):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

from vera.llm import Gemini  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="http://localhost:8080")
    ap.add_argument("--sim", default=os.path.join(os.path.dirname(ROOT), "pack", "judge_simulator.py"))
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--judge-model", default=os.environ.get("JUDGE_MODEL", "gemini-2.5-flash"))
    ap.add_argument("--no-teardown", action="store_true")
    a = ap.parse_args()

    spec = importlib.util.spec_from_file_location("judge_simulator", a.sim)
    js = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(js)
    js.BOT_URL = a.bot.rstrip("/")

    judge_llm = Gemini()
    judge_llm.models = [a.judge_model]
    judge_llm.thinking_ok = True

    class GeminiJudge(js.LLMProvider):
        def complete(self, prompt, system=None):
            return judge_llm.generate(system or "", prompt, timeout=45, json_mode=False, max_tokens=1500)

        def name(self):
            return f"Gemini judge ({judge_llm.model})"

    if not a.no_teardown:
        try:
            urllib.request.urlopen(urllib.request.Request(js.BOT_URL + "/v1/teardown", data=b"{}", method="POST",
                                                          headers={"Content-Type": "application/json"}), timeout=10)
        except Exception as e:
            print("teardown failed (ok if bot is older):", e)
    j = js.JudgeSimulator(GeminiJudge())
    j.client = js.BotClient(js.BOT_URL)
    ok = j.run(a.scenario)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
