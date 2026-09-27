# Vera challenge bot — Ishaan Gupta

A merchant-engagement bot for the magicpin AI Challenge. It exposes the five judge endpoints (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) plus `/v1/teardown`. The server is pure Python standard library, so it has **zero dependencies** (`python server.py`).

## Approach

Every message passes through the same five steps:

1. **Planner (rules, no LLM).** On each tick it drops triggers that are already sent (by suppression key), opted-out, or backed off. It sends at most 2 merchant-facing messages per merchant per tick, highest urgency first, and at most one message per customer. It never sends the same body twice to the same recipient.
2. **Fact builder (`vera/facts.py`).** A read-only view over the four contexts. It works out the salutation (Dr./owner name), the language (Hinglish when `hi` is in the merchant's languages, or from the customer's `language_pref`), and peer gaps such as CTR against the category median. It picks the single most fixable lever (no live offer → stale posts → unverified listing → CTR gap). It matches offers to audience and day ("First Month" never goes to a regular; "(Tue-Thu)" never becomes a Sunday hook) and matches trends to what the merchant actually sells. It also picks up open conversation threads.
3. **Deterministic templates (`vera/templates.py`).** One builder per trigger family covers about 25 kinds, with a generic fallback for unseen kinds. Each builder is grounded, category-voiced and has a single CTA. Many expanded triggers carry `{"placeholder": true}` payloads. For those, the builder anchors on the merchant's own numbers and never invents specifics. A customer trigger that arrives without its customer context becomes a merchant-facing brief that asks before messaging the customer.
4. **Gemini rewrite (`vera/compose.py`, `vera/llm.py`).** Gemini runs at temperature 0 with a fixed seed. It sees the four contexts plus the template as a baseline, and is told to sharpen compulsion without adding facts. It works with both AI Studio and Vertex-express keys and has model fallbacks and a circuit breaker. Pre-warming starts when a trigger is pushed, so ticks usually read from cache.
5. **Validator (`vera/validate.py`).** It rejects the rewrite if it contains any number not grounded in the contexts. Date parts only count when written as dates. It also rejects quoted text not found in the data, URLs, category taboo words, preambles or self-introductions, more than one reply instruction, a missing recipient name, or a body over 650 characters. On any failure the grounded template ships, so **output is never empty, never invented, and is identical for identical inputs** (cached by context versions).

**Replies (`vera/replies.py`)** work rule-first:
- **Auto-replies** are counted per sender across conversations: the first gets one flag for the owner, the second waits 24h, the third ends the conversation.
- **Stop or opt-out** ends the conversation and suppresses the sender.
- **Abuse** gets a one-line apology and a 7-day pause.
- **Commitment** ("ok let's do it", "haan karo", "1") switches to action mode immediately with a concrete draft and no qualifying questions.
- **Off-topic asks** (GST, loans…) get a polite decline and a steer back.
- **"Later"** becomes a wait.
- **Questions** get an answer from context plus one next step.

Gemini words the answers, and the same validator checks them.

## Tradeoffs

- Rules decide *what* to do and the LLM only decides *how to say it*. This gives up some creative range in exchange for zero hallucination and hard latency bounds: ticks take about 3ms on templates and are capped at 11s with the LLM.
- Placeholder triggers are answered with honest, merchant-grounded messages instead of invented event details.
- State is in memory, which the brief allows. Keep a single instance running during the test.

## Run and test

```
python server.py                                     # PORT=8080 by default; GEMINI_API_KEY in env or .env
python dataset/generate_dataset.py --seed-dir dataset --out expanded
python tools/local_harness.py --data expanded        # contract + 12-tick window + injections + replies + burst
python tools/check_gemini.py                         # verify key/model, time one composition
python tools/run_judge.py --scenario all             # official judge_simulator.py with Gemini as judge
python tools/make_submission.py --data expanded      # writes submission.jsonl for the 30 test pairs
```

## What would help most

Real open slots and service durations per merchant, and a merchant-level "services offered" list (so trend and offer suggestions never guess). Some weekday labels in the payloads were wrong, so I recompute them from the ISO timestamps. Delivery and dine-in capability flags for restaurants would also help.
