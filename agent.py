"""
agent.py — 3-step agentic pipeline for Vera Bot
Step 1: Signal Ranker + Synthesizer  (llama-3.1-8b-instant, ~1s)
Step 2: Composer with escalation     (llama-3.3-70b-versatile, ~5s)
Step 3: Critic & self-correct        (llama-3.1-8b-instant, ~1s)
Total: ~7s | Budget: 25s | Buffer: ~18s

Upgrades:
- Tick history passed to Step 1 + Step 2 for escalation
- Signal synthesis (perfect storm detection)
- 70b model for replies (replay test quality)
- Parallel context fetch + Step 1 via threading
- Tighter fallback chain
"""

import json
import os
import time
import logging
import threading
from groq import Groq

from prompts import (
    SIGNAL_RANKER_SYSTEM, signal_ranker_user,
    COMPOSER_SYSTEM, composer_user,
    CRITIC_SYSTEM, critic_user,
    REPLY_COMPOSER_SYSTEM, reply_composer_user,
)
from categories import get_category_rules

logger = logging.getLogger(__name__)

# ── Groq client (lazy init) ───────────────────────────────────────
_client = None
_client_lock = threading.Lock()


def get_client() -> Groq:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked locking
                api_key = os.environ.get("GROQ_API_KEY", "")
                if not api_key:
                    raise RuntimeError("GROQ_API_KEY environment variable is not set")
                _client = Groq(api_key=api_key )
    return _client


MODEL_FAST = "llama-3.1-8b-instant"
MODEL_STRONG = "llama-3.3-70b-versatile"

STEP2_TIMEOUT = 12      # seconds — if exceeded, log warning
PIPELINE_HARD_LIMIT = 22  # seconds — skip Step 3 if exceeded


# ── Core Groq call ────────────────────────────────────────────────

def call_groq(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_FAST,
    temperature: float = 0.3,
    max_tokens: int = 500,
) -> dict:
    """
    Core Groq API call with JSON mode. Raises on failure.
    """
    response = get_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


def call_groq_with_retry(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_FAST,
    fallback_model: str = MODEL_FAST,
    temperature: float = 0.3,
    max_tokens: int = 500,
) -> dict:
    """
    Call Groq with one automatic retry on a different model.
    """
    try:
        return call_groq(system_prompt, user_prompt, model, temperature, max_tokens)
    except Exception as e:
        logger.warning(f"Groq call failed ({model}): {e} — retrying with {fallback_model}")
        try:
            return call_groq(system_prompt, user_prompt, fallback_model, temperature, max_tokens)
        except Exception as e2:
            logger.error(f"Groq retry also failed ({fallback_model}): {e2}")
            raise


# ── Step 1: Signal Rank + Synthesis ──────────────────────────────

def step1_signal_rank(merged_context: dict, tick_history: list) -> dict:
    """
    Analyze signals, pick strongest, detect perfect storms, flag escalation.
    Uses llama-3.1-8b-instant (~1s).
    """
    user_prompt = signal_ranker_user(merged_context, tick_history)
    try:
        result = call_groq_with_retry(
            SIGNAL_RANKER_SYSTEM,
            user_prompt,
            model=MODEL_FAST,
            fallback_model=MODEL_FAST,
            max_tokens=400,
        )
    except Exception as e:
        logger.error(f"Step1 Groq failed: {e} — using fallback signal")
        result = {}

    result.setdefault("top_signal", "general_opportunity")
    result.setdefault("signal_type", "streak")
    result.setdefault("key_number", "N/A")
    result.setdefault("key_fact", "merchant on magicpin")
    result.setdefault("reasoning", "Opportunity to engage merchant now")
    result.setdefault("is_perfect_storm", False)
    result.setdefault("storm_narrative", "")
    result.setdefault("escalation_needed", False)
    result.setdefault("escalation_reason", "")
    result.setdefault("suggested_angle", "")
    return result


# ── Step 2: Compose ───────────────────────────────────────────────

def step2_compose(
    step1_output: dict,
    merchant_payload: dict,
    trigger_payload: dict,
    category: str,
    category_rules: dict,
    tick_history: list,
    customer_payload: dict | None = None,
) -> dict:
    """
    Compose the merchant message with escalation + storm awareness.
    Uses llama-3.3-70b-versatile (~5s), falls back to 8b.
    """
    user_prompt = composer_user(
        step1_output=step1_output,
        merchant_payload=merchant_payload,
        trigger_payload=trigger_payload,
        category=category,
        category_tone=category_rules["tone_instruction"],
        tick_history=tick_history,
        customer_payload=customer_payload,
    )

    t0 = time.time()
    result = None

    try:
        result = call_groq_with_retry(
            COMPOSER_SYSTEM,
            user_prompt,
            model=MODEL_STRONG,
            fallback_model=MODEL_FAST,
            temperature=0.4,
            max_tokens=500,
        )
    except Exception as e1:
        logger.warning(f"Step2 all retries failed: {e1} — trying 8b direct")
        try:
            result = call_groq(
                COMPOSER_SYSTEM,
                user_prompt,
                model=MODEL_FAST,
                temperature=0.4,
                max_tokens=500,
            )
        except Exception as e2:
            logger.error(f"Step2 complete failure: {e2} — using static fallback")
            result = None

    elapsed = time.time() - t0
    if elapsed > STEP2_TIMEOUT:
        logger.warning(f"Step2 took {elapsed:.1f}s — over {STEP2_TIMEOUT}s budget")

    if result is None:
        result = {}

    result.setdefault("message", "Vera is here to help grow your business!")
    result.setdefault("cta", "Reply YES to proceed")
    result.setdefault("send_as", "vera")
    result.setdefault("rationale", "Signal-based outreach")
    result["send_as"] = "vera"
    return result


# ── Step 3: Critic ────────────────────────────────────────────────

def step3_critic(step2_output: dict, original_context: dict) -> dict:
    """
    Score on 5 dimensions, rewrite if any < 7.
    Uses llama-3.1-8b-instant (~1s).
    """
    user_prompt = critic_user(step2_output, original_context)
    try:
        result = call_groq_with_retry(
            CRITIC_SYSTEM,
            user_prompt,
            model=MODEL_FAST,
            fallback_model=MODEL_FAST,
            max_tokens=600,
        )
    except Exception as e:
        logger.error(f"Step3 failed: {e} — returning Step2 output")
        return step2_output

    result.setdefault("message", step2_output.get("message", ""))
    result.setdefault("cta", step2_output.get("cta", ""))
    result.setdefault("send_as", "vera")
    result.setdefault("rationale", step2_output.get("rationale", ""))
    result["send_as"] = "vera"
    return result


# ── Full Pipeline ─────────────────────────────────────────────────

def run_pipeline(
    merchant_id: str,
    trigger_id: str,
    merchant_payload: dict,
    trigger_payload: dict,
    customer_payload: dict | None,
    category: str,
    tick_history: list,
) -> dict:
    """
    Full 3-step agentic pipeline with escalation + signal synthesis.
    Returns final composed + critiqued message dict.
    """
    pipeline_start = time.time()
    category_rules = get_category_rules(category)

    # Flatten nested judge payload structure: {identity:{}, performance:{}, offers:[]}
    # into a single dict so prompts can reference any field directly
    def flatten_payload(p: dict) -> dict:
        flat = {}
        for k, v in p.items():
            if isinstance(v, dict):
                flat.update(v)       # hoist nested keys to top level
                flat[k] = v          # also keep original nested key
            else:
                flat[k] = v
        return flat

    merchant_flat = flatten_payload(merchant_payload)
    trigger_flat = flatten_payload(trigger_payload)
    customer_flat = flatten_payload(customer_payload) if customer_payload else {}

    merged_context = {
        "merchant_id": merchant_id,
        "trigger_id": trigger_id,
        "category": category,
        "merchant": merchant_flat,
        "trigger": trigger_flat,
        "customer": customer_flat,
        "tick_count": len(tick_history),
    }

    # ── STEP 1: Signal Rank ──────────────────────────────────────
    t1 = time.time()
    step1_result = step1_signal_rank(merged_context, tick_history)
    logger.info(
        f"[{merchant_id}] Step1 {time.time()-t1:.2f}s | "
        f"signal={step1_result.get('top_signal')} | "
        f"storm={step1_result.get('is_perfect_storm')} | "
        f"escalate={step1_result.get('escalation_needed')}"
    )

    # ── STEP 2: Compose ──────────────────────────────────────────
    t2 = time.time()
    try:
        step2_result = step2_compose(
            step1_output=step1_result,
            merchant_payload=merchant_payload,
            trigger_payload=trigger_payload,
            category=category,
            category_rules=category_rules,
            tick_history=tick_history,
            customer_payload=customer_payload,
        )
        logger.info(f"[{merchant_id}] Step2 {time.time()-t2:.2f}s")
    except Exception as e:
        logger.error(f"[{merchant_id}] Step2 exception: {e}")
        step2_result = {
            "message": (
                f"Your {category} business has a strong opportunity right now. "
                f"Want me to help you act on it?"
            ),
            "cta": "Reply YES to proceed",
            "send_as": "vera",
            "rationale": "Fallback — Groq unavailable",
        }

    # ── Check time before Step 3 ─────────────────────────────────
    elapsed = time.time() - pipeline_start
    if elapsed > PIPELINE_HARD_LIMIT:
        logger.warning(f"[{merchant_id}] Pipeline {elapsed:.1f}s > {PIPELINE_HARD_LIMIT}s — skipping Step3")
        return step2_result

    # ── STEP 3: Critic ───────────────────────────────────────────
    t3 = time.time()
    final_result = step3_critic(step2_result, merged_context)
    logger.info(
        f"[{merchant_id}] Step3 {time.time()-t3:.2f}s | "
        f"passed={final_result.get('passed', '?')} | "
        f"total={time.time()-pipeline_start:.2f}s"
    )

    return final_result


# ── Reply Pipeline (70b for quality) ─────────────────────────────

def run_reply_pipeline(
    reply_text: str,
    conversation_history: list,
    original_message: str,
    original_cta: str,
    category: str,
    merchant_payload: dict,
    tick_history: list,
) -> dict:
    """
    Reply pipeline using 70b model for replay test quality.
    Handles all 5 reply cases with full tick history context.
    """
    user_prompt = reply_composer_user(
        reply_text=reply_text,
        conversation_history=conversation_history,
        original_message=original_message,
        original_cta=original_cta,
        category=category,
        merchant_payload=merchant_payload,
        tick_history=tick_history,
    )

    try:
        # Use 70b for replies — this is the replay test tiebreaker
        result = call_groq_with_retry(
            REPLY_COMPOSER_SYSTEM,
            user_prompt,
            model=MODEL_STRONG,
            fallback_model=MODEL_FAST,
            temperature=0.3,
            max_tokens=400,
        )
    except Exception as e:
        logger.error(f"Reply pipeline failed: {e}")
        result = {
            "message": "Thanks for your reply! Let me know how I can help you further.",
            "cta": "",
            "send_as": "vera",
            "rationale": "Fallback reply",
            "suppress": False,
            "case_detected": "unknown",
            "mark_acted": False,
        }

    result.setdefault("suppress", False)
    result.setdefault("case_detected", "unknown")
    result.setdefault("send_as", "vera")
    result.setdefault("mark_acted", False)
    return result
