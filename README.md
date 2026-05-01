# Vera Bot — magicpin India AI Challenge 2026

**Author:** Pranjul Chaurasiya | research.pranjul@gmail.com  
**Bot URL:** https://veraai-bot.up.railway.app  
**Version:** 5.0.0

---

## Approach

### 3-Step Agentic Pipeline

Every `/v1/tick` runs a 3-step pipeline:

```
Step 1 — Signal Ranker + Synthesizer   (llama-3.1-8b-instant, ~1s)
         Picks the single strongest signal from all 4 contexts.
         Detects "perfect storms" (2+ signals converging).
         Flags escalation if merchant ignored last N ticks.
         Computes peer comparison for social proof lever.

Step 2 — Composer with Escalation      (llama-3.3-70b-versatile, ~5s)
         Writes category-voice-aware, merchant-specific message.
         Uses all 5 compulsion levers: loss aversion, social proof,
         effort externalization, curiosity, reciprocity.
         Escalates angle if merchant has been ignoring.
         Passes full category digest + peer stats to composer.

Step 3 — Critic & Self-Correct         (llama-3.1-8b-instant, ~1s)
         Scores 5 dimensions (0-10 each).
         Rewrites any dimension scoring < 7.
         Fact-checks all numbers against original context.

Total: ~7s | Judge timeout: 30s | Buffer: ~23s
```

### Why Groq

- `llama-3.1-8b-instant`: ~1s per call — used for signal ranking and critic
- `llama-3.3-70b-versatile`: ~5s per call — used for composition and replies
- `response_format={"type": "json_object"}`: forces clean JSON, zero parsing failures
- Total pipeline: ~7s vs 30s timeout = 23s buffer

### Category Voice Strategy

| Category | Tone | Memes | Key vocab |
|---|---|---|---|
| Dentists | peer_clinical | No | fluoride varnish, caries, IOPA, recall |
| Salons | warm_practical | Yes | balayage, keratin, bridal, hair spa |
| Restaurants | fellow_operator | Yes | covers, AOV, footfall, thali |
| Gyms | coach_to_member | Yes | membership, PT, HIIT, churn |
| Pharmacies | trustworthy_precise | No | molecule, batch, schedule H, chronic Rx |

The category context (voice rules, digest items, peer stats) is passed directly to the composer on every tick — not hardcoded.

### Adaptive Injection Handling

Every `/v1/tick` reads fresh from `context_store`. When the judge injects new context mid-test (new digest items, updated performance, new triggers), the next tick automatically uses the updated data. No caching of composed messages.

### Replay Test Strategy

`/v1/reply` handles all cases:

| Case | Detection | Response |
|---|---|---|
| YES | "yes", "go ahead", "haan", "kar do" | Immediately execute — no qualifying questions |
| NO | "no", "nahi", "not now" | Acknowledge + offer different angle with social proof |
| Off-topic | Unrelated question | Brief answer + redirect to original CTA |
| Hostile | "stop messaging", "band kar" | Apologize + end + suppress |
| Handoff | "talk to manager" | Acknowledge + wait 30 min |
| Auto-reply | Canned WA Business phrases | Turn 2: flag → Turn 3: wait 4h → Turn 4: end |

### Tick Memory + Escalation

Every tick is recorded in `tick_history[merchant_id]`. The pipeline knows:
- How many ticks this merchant has received
- Which signals were used
- Whether the merchant acted (said YES)
- How many consecutive ticks were ignored

If ignored 1 tick → adds time constraint  
If ignored 2+ ticks → completely changes angle, references missed opportunity cost

### Dataset Preloaded on Startup

All 5 categories, 10 merchants, 15 customers, 25 triggers are loaded into memory before the judge calls `/v1/context`. This ensures the bot passes warmup immediately and has base context for all 30 test pairs.

---

## Tradeoffs

| Decision | Tradeoff |
|---|---|
| Groq over OpenAI | 7s vs 15s+ — speed wins for 30s timeout |
| 70b for compose + replies | Best quality where it matters most |
| 8b for signal rank + critic | Fast bookends, quality not critical |
| In-memory state | Zero latency, zero infra — resets on restart (fine for 3-day test) |
| `temperature=0.3` | Deterministic enough, not robotic |
| Tick memory | Enables escalation — key differentiator vs stateless bots |
| Category context passed to composer | Voice-aware composition without hardcoding |

---

## What Additional Context Would Have Helped

1. **Real-time slot availability** — for recall_due triggers, actual open appointment slots would make the message more actionable
2. **Merchant's WhatsApp conversation history** — knowing what Vera said in previous sessions would improve escalation quality
3. **Competitor data** — for competitor_opened triggers, knowing the competitor's actual offer would enable sharper positioning
4. **Customer visit history** — for lapsed customer triggers, knowing the specific services received would personalize the winback message

---

## Local Run

```bash
cd vera-bot
pip install -r requirements.txt
cp .env.example .env  # add GROQ_API_KEY
python app.py
python judge_simulator.py
```

---

## Deployment

Railway.app — auto-deploys from GitHub on every push.  
Public URL: https://veraai-bot.up.railway.app
