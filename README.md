# Vera Bot v2 — magicpin India AI Challenge 2026

**Author:** Pranjul Chaurasiya | research.pranjul@gmail.com  
**Version:** 2.0.0  
**Deadline:** May 2, 2026 11:59 PM IST

---

## What This Is

A production-ready stateful Python Flask API bot powering **Vera** — magicpin's AI growth assistant for Indian merchants.

The judge runs 60 simulated minutes, calling `/v1/tick` every 5 minutes (12+ ticks), injecting fresh context mid-test, and replaying hostile/off-topic replies against top bots.

---

## Architecture: 3-Step Agentic Pipeline

```
Every /v1/tick:

Step 1 — Signal Ranker + Synthesizer   (llama-3.1-8b-instant, ~1s)
         ↓ Picks strongest signal
         ↓ Detects "perfect storm" (2+ signals converging)
         ↓ Flags escalation if merchant ignored last N ticks

Step 2 — Composer with Escalation      (llama-3.3-70b-versatile, ~5s)
         ↓ Writes category-tuned, merchant-specific message
         ↓ Escalates urgency if merchant has been ignoring
         ↓ Leads with storm narrative if perfect storm detected

Step 3 — Critic & Self-Correct         (llama-3.1-8b-instant, ~1s)
         ↓ Scores 5 dimensions (0-10 each)
         ↓ Rewrites any dimension scoring < 7
         ↓ Returns original if all ≥ 7

Total: ~7s | Judge timeout: 30s | Buffer: ~23s ✅

Every /v1/reply:
         → llama-3.3-70b-versatile (70b for replay test quality)
         → Detects all 5 reply cases
         → Updates tick history (mark_acted on YES)
```

---

## Key Upgrades in v2

### 1. Tick Memory + Escalation
Every tick is recorded in `tick_history[merchant_id]`. The pipeline knows:
- How many ticks this merchant has received
- Which signals were used
- Whether the merchant acted (said YES)
- How many consecutive ticks were ignored

If ignored 1 tick → adds time constraint ("by tonight", "next 2 hours")  
If ignored 2+ ticks → completely changes angle, references missed opportunity cost

### 2. Signal Synthesis (Perfect Storm)
When 2+ signals converge simultaneously (e.g. `order_dip + festival + search_spike`), Step 1 detects the combination and Step 2 leads with the combined narrative:
> "IPL tonight + 37% order dip + biryani searches up 2.3x = act NOW"

### 3. 70b Model for Replies
The replay test is the tiebreaker for top 10. Replies now use `llama-3.3-70b-versatile` instead of 8b — significantly better case detection and response quality.

### 4. Reliable Tick→Merchant Registry
`register_tick(tick_id, merchant_id, ...)` is called on every tick. `/v1/reply` uses `lookup_tick(tick_id)` for O(1) merchant lookup instead of scanning the cache.

### 5. mark_acted()
When the reply pipeline detects a YES, it calls `mark_tick_acted(merchant_id, tick_id)`. This prevents the escalation logic from treating an acted-on tick as ignored.

---

## Why Groq

| Step | Model | Time |
|------|-------|------|
| Signal Rank | llama-3.1-8b-instant | ~1s |
| Compose | llama-3.3-70b-versatile | ~5s |
| Critic | llama-3.1-8b-instant | ~1s |
| Reply | llama-3.3-70b-versatile | ~5s |
| **Tick total** | | **~7s** |
| **Reply total** | | **~5s** |

`response_format={"type": "json_object"}` forces clean JSON — zero parsing failures.

---

## Category Tone Strategy

| Category | Tone | Memes |
|----------|------|-------|
| Restaurant | Hinglish meme, appetite-driven | ✅ |
| Salon | Aspirational, Hinglish ok, trend-aware | ✅ |
| Gym | Motivational, streak-based | ✅ |
| Dentist | Clinical, trust-first, professional | ❌ |
| Pharmacy | Utility-first, compliance-aware | ❌ |

**Message structure (casual):** FACT first → meme hook → CTA  
**Message structure (medical):** Precise number → professional context → formal CTA

---

## Adaptive Injection Handling

1. `/v1/context` stores context atomically by `scope:context_id` key
2. Higher version = atomic replace. Same/lower = no-op (idempotent)
3. **Every `/v1/tick` reads fresh from `context_store`** — never caches composed messages
4. New context mid-test = better messages from the next tick onward 🔥

---

## Replay Test Strategy

| Case | Detection | Response |
|------|-----------|----------|
| YES | "yes", "go ahead", "haan", "kar do" | Confirm action + next concrete step. Mark acted. |
| NO | "no", "nahi", "not now", "baad mein" | Acknowledge + offer completely different angle |
| Off-topic | Unrelated question | Brief answer + gentle redirect to CTA |
| Hostile | "stop messaging", "band kar", "annoying" | Apologize + set suppression. Never message again today. |
| Handoff | "talk to manager", "check with owner" | Acknowledge + set send_as=handoff |

---

## Fallback Chain

```
Groq API key missing → RuntimeError → caught → cached message or hardcoded generic
Step 1 fails → hardcoded signal dict → pipeline continues
Step 2 fails → retry 70b → retry 8b → direct 8b → static fallback message
Pipeline > 22s → skip Step 3, return Step 2 output
Step 3 fails → return Step 2 output unchanged
Entire pipeline throws → cached last message (correct tick_id) → hardcoded generic
Outer route try/except → NEVER returns 500
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/healthz` | Health check |
| GET | `/v1/metadata` | Bot metadata |
| POST | `/v1/context` | Store merchant/trigger/customer context |
| POST | `/v1/tick` | Run 3-step pipeline, return message |
| POST | `/v1/reply` | Handle merchant reply (70b), return follow-up |

---

## Local Run

```bash
# 1. Enter directory
cd vera-bot

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your Groq API key
cp .env.example .env
# Edit .env: GROQ_API_KEY=gsk_...

# 4. Load env and start
set -a && source .env && set +a   # Linux/Mac
# OR on Windows: set GROQ_API_KEY=gsk_...

python app.py
# OR production:
gunicorn app:app --workers 4 --timeout 60

# 5. Run judge simulator (separate terminal)
python judge_simulator.py
```

Get a free Groq API key: https://console.groq.com

---

## Deploy to Railway.app

1. Push `vera-bot/` to GitHub
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Set env var: `GROQ_API_KEY=gsk_...`
4. Railway reads `Procfile` + `runtime.txt` automatically
5. Public HTTPS URL in ~2 minutes

---

## Tradeoffs

| Decision | Tradeoff |
|----------|----------|
| Groq over OpenAI | 7s vs 15s+ — speed wins for 30s timeout |
| 70b for compose + replies | Best quality where it matters most |
| 8b for signal rank + critic | Fast bookends, quality not critical |
| In-memory state | Zero latency, zero infra — resets on restart (fine for 3-day test) |
| `temperature=0.3` | Deterministic enough, not robotic |
| Tick memory | Enables escalation — key differentiator vs stateless bots |

---

## Pre-submission Checklist

- [ ] `GROQ_API_KEY` set in Railway env vars
- [ ] `python judge_simulator.py` — 56/56 checks pass ✅
- [ ] Response time < 30s ✅
- [ ] JSON schema matches exactly ✅
- [ ] Public HTTPS URL live ✅
- [ ] Bot stays live for 3 days ✅
