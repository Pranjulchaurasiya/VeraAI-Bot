"""
agent.py — 3-step agentic pipeline for Vera Bot
Step 1: Signal Ranker     (llama-3.1-8b-instant, ~1s)
Step 2: Composer          (llama-3.3-70b-versatile, ~5s)
Step 3: Critic            (llama-3.1-8b-instant, ~1s)
Total: ~7s | Budget: 25s | Buffer: ~18s
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

logger = logging.getLogger(__name__)

# ── Groq client (lazy, thread-safe) ──────────────────────────────
_client = None
_client_lock = threading.Lock()


def get_client() -> Groq:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                api_key = os.environ.get("GROQ_API_KEY", "")
                if not api_key:
                    raise RuntimeError("GROQ_API_KEY not set")
                _client = Groq(api_key=api_key)
    return _client


MODEL_FAST = "llama-3.1-8b-instant"
MODEL_STRONG = "llama-3.3-70b-versatile"
PIPELINE_HARD_LIMIT = 22  # skip Step 3 if exceeded


# ── Core Groq call ────────────────────────────────────────────────

def call_groq(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_FAST,
    temperature: float = 0.3,
    max_tokens: int = 600,
) -> dict:
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
    return json.loads(response.choices[0].message.content)


def call_groq_with_retry(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_FAST,
    fallback_model: str = MODEL_FAST,
    temperature: float = 0.3,
    max_tokens: int = 600,
) -> dict:
    try:
        return call_groq(system_prompt, user_prompt, model, temperature, max_tokens)
    except Exception as e:
        logger.warning(f"Groq ({model}) failed: {e} — retrying with {fallback_model}")
        try:
            return call_groq(system_prompt, user_prompt, fallback_model, temperature, max_tokens)
        except Exception as e2:
            logger.error(f"Groq retry ({fallback_model}) failed: {e2}")
            raise


# ── Step 1: Signal Rank ───────────────────────────────────────────

def step1_signal_rank(merged_context: dict, tick_history: list) -> dict:
    user_prompt = signal_ranker_user(merged_context, tick_history)
    try:
        result = call_groq_with_retry(
            SIGNAL_RANKER_SYSTEM, user_prompt,
            model=MODEL_FAST, fallback_model=MODEL_FAST,
            max_tokens=400,
        )
    except Exception as e:
        logger.error(f"Step1 failed: {e}")
        result = {}

    result.setdefault("top_signal", "general_opportunity")
    result.setdefault("key_number", "N/A")
    result.setdefault("key_fact", "merchant on magicpin")
    result.setdefault("reasoning", "Opportunity to engage merchant now")
    result.setdefault("is_perfect_storm", False)
    result.setdefault("storm_narrative", "")
    result.setdefault("escalation_needed", False)
    result.setdefault("send_as", "vera")
    result.setdefault("is_customer_facing", False)
    return result


# ── Step 2: Compose ───────────────────────────────────────────────

def step2_compose(
    step1_output: dict,
    merchant_payload: dict,
    trigger_payload: dict,
    category_payload: dict,
    category: str,
    tick_history: list,
    customer_payload: dict | None = None,
) -> dict:
    user_prompt = composer_user(
        step1_output=step1_output,
        merchant_payload=merchant_payload,
        trigger_payload=trigger_payload,
        category_payload=category_payload,
        category=category,
        tick_history=tick_history,
        customer_payload=customer_payload,
    )

    t0 = time.time()
    result = None

    try:
        result = call_groq_with_retry(
            COMPOSER_SYSTEM, user_prompt,
            model=MODEL_STRONG, fallback_model=MODEL_FAST,
            temperature=0.4, max_tokens=700,
        )
    except Exception as e1:
        logger.warning(f"Step2 retries failed: {e1} — trying 8b direct")
        try:
            result = call_groq(COMPOSER_SYSTEM, user_prompt, model=MODEL_FAST, temperature=0.4, max_tokens=700)
        except Exception as e2:
            logger.error(f"Step2 complete failure: {e2}")
            result = None

    elapsed = time.time() - t0
    if elapsed > 12:
        logger.warning(f"Step2 took {elapsed:.1f}s")

    if result is None:
        result = {}

    # Determine send_as from step1 or customer context
    default_send_as = "merchant_on_behalf" if (customer_payload or step1_output.get("is_customer_facing")) else "vera"
    result.setdefault("message", "Vera is here to help grow your business!")
    result.setdefault("cta", "open_ended")
    result.setdefault("send_as", default_send_as)
    result.setdefault("template_name", f"vera_{trigger_payload.get('kind', 'generic')}_v1")
    result.setdefault("template_params", [])
    result.setdefault("rationale", "Signal-based outreach")
    return result


# ── Step 3: Critic ────────────────────────────────────────────────

def step3_critic(step2_output: dict, original_context: dict) -> dict:
    user_prompt = critic_user(step2_output, original_context)
    try:
        result = call_groq_with_retry(
            CRITIC_SYSTEM, user_prompt,
            model=MODEL_FAST, fallback_model=MODEL_FAST,
            max_tokens=700,
        )
    except Exception as e:
        logger.error(f"Step3 failed: {e} — returning Step2")
        return step2_output

    result.setdefault("message", step2_output.get("message", ""))
    result.setdefault("cta", step2_output.get("cta", ""))
    result.setdefault("send_as", step2_output.get("send_as", "vera"))
    result.setdefault("template_name", step2_output.get("template_name", ""))
    result.setdefault("template_params", step2_output.get("template_params", []))
    result.setdefault("rationale", step2_output.get("rationale", ""))
    return result


# ── Full Pipeline ─────────────────────────────────────────────────

def run_pipeline(
    merchant_id: str,
    trigger_id: str,
    merchant_payload: dict,
    trigger_payload: dict,
    category_payload: dict,
    customer_payload: dict | None,
    category: str,
    tick_history: list,
) -> dict:
    pipeline_start = time.time()

    merged_context = {
        "merchant_id": merchant_id,
        "trigger_id": trigger_id,
        "category": category,
        "merchant": merchant_payload,
        "trigger": trigger_payload,
        "customer": customer_payload or {},
        "category_context": {
            "peer_stats": category_payload.get("peer_stats", {}),
            "seasonal_beats": category_payload.get("seasonal_beats", []),
            "trend_signals": category_payload.get("trend_signals", [])[:3],
            "voice_tone": category_payload.get("voice", {}).get("tone", ""),
        },
        "tick_count": len(tick_history),
    }

    # ── Step 1 ───────────────────────────────────────────────────
    t1 = time.time()
    step1_result = step1_signal_rank(merged_context, tick_history)
    logger.info(
        f"[{merchant_id}] Step1 {time.time()-t1:.2f}s | "
        f"signal={step1_result.get('top_signal')} | "
        f"storm={step1_result.get('is_perfect_storm')} | "
        f"escalate={step1_result.get('escalation_needed')}"
    )

    # ── Step 2 ───────────────────────────────────────────────────
    t2 = time.time()
    try:
        step2_result = step2_compose(
            step1_output=step1_result,
            merchant_payload=merchant_payload,
            trigger_payload=trigger_payload,
            category_payload=category_payload,
            category=category,
            tick_history=tick_history,
            customer_payload=customer_payload,
        )
        logger.info(f"[{merchant_id}] Step2 {time.time()-t2:.2f}s")
    except Exception as e:
        logger.error(f"[{merchant_id}] Step2 exception: {e}")
        owner = (merchant_payload.get("identity") or {}).get("owner_first_name", "")
        step2_result = {
            "message": f"{owner + ', ' if owner else ''}there's a strong opportunity for your {category} business right now. Want me to help you act on it?",
            "cta": "open_ended",
            "send_as": "vera",
            "template_name": "vera_generic_v1",
            "template_params": [],
            "rationale": "Fallback — Groq unavailable",
        }

    # ── Time check ───────────────────────────────────────────────
    elapsed = time.time() - pipeline_start
    if elapsed > PIPELINE_HARD_LIMIT:
        logger.warning(f"[{merchant_id}] Pipeline {elapsed:.1f}s > limit — skipping Step3")
        return step2_result

    # ── Step 3 ───────────────────────────────────────────────────
    t3 = time.time()
    final_result = step3_critic(step2_result, merged_context)
    logger.info(
        f"[{merchant_id}] Step3 {time.time()-t3:.2f}s | "
        f"passed={final_result.get('passed', '?')} | "
        f"total={time.time()-pipeline_start:.2f}s"
    )
    return final_result


# ── Reply Pipeline (70b) ──────────────────────────────────────────

def run_reply_pipeline(
    reply_text: str,
    conversation_history: list,
    original_message: str,
    original_cta: str,
    category: str,
    merchant_payload: dict,
    tick_history: list,
    turn_number: int = 1,
    trigger_payload: dict | None = None,
    category_payload: dict | None = None,
) -> dict:
    user_prompt = reply_composer_user(
        reply_text=reply_text,
        conversation_history=conversation_history,
        original_message=original_message,
        original_cta=original_cta,
        category=category,
        merchant_payload=merchant_payload,
        tick_history=tick_history,
        turn_number=turn_number,
        trigger_payload=trigger_payload,
        category_payload=category_payload,
    )

    try:
        result = call_groq_with_retry(
            REPLY_COMPOSER_SYSTEM, user_prompt,
            model=MODEL_STRONG, fallback_model=MODEL_FAST,
            temperature=0.3, max_tokens=400,
        )
    except Exception as e:
        logger.error(f"Reply pipeline failed: {e}")
        result = {
            "message": "Thanks for your reply! Let me know how I can help.",
            "cta": "",
            "send_as": "vera",
            "action": "send",
            "wait_seconds": 0,
            "rationale": "Fallback reply",
            "suppress": False,
            "case_detected": "unknown",
            "mark_acted": False,
        }

    result.setdefault("action", "send")
    result.setdefault("wait_seconds", 0)
    result.setdefault("suppress", False)
    result.setdefault("case_detected", "unknown")
    result.setdefault("send_as", "vera")
    result.setdefault("mark_acted", False)
    return result
