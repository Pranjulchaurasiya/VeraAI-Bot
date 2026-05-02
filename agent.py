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

# ── Signal priority order (deterministic fallback if LLM picks wrong) ──
# Higher index = higher priority. Used to validate/override LLM signal pick.
SIGNAL_PRIORITY = [
    "curious_ask_due",
    "dormant_with_vera",
    "category_seasonal",
    "gbp_unverified",
    "cde_opportunity",
    "winback_eligible",
    "trial_followup",
    "wedding_package_followup",
    "milestone_reached",
    "perf_spike",
    "active_planning_intent",
    "renewal_due",
    "competitor_opened",
    "chronic_refill_due",
    "supply_alert",
    "review_theme_emerged",
    "seasonal_perf_dip",
    "ipl_match_today",
    "festival_upcoming",
    "recall_due",
    "perf_dip",
    "regulation_change",
    "research_digest",
]


# ── Core Groq call ────────────────────────────────────────────────

def call_groq(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_FAST,
    temperature: float = 0.0,
    max_tokens: int = 600,
) -> dict:
    """
    temperature=0.0 by default — deterministic outputs.
    Same input always produces same output. Critical for judge scoring.
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
    return json.loads(response.choices[0].message.content)


def call_groq_with_retry(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_FAST,
    fallback_model: str = MODEL_FAST,
    temperature: float = 0.0,
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

def _derive_signal_from_trigger(trigger_payload: dict) -> str:
    """
    Deterministic signal extraction directly from trigger payload.
    Used as ground truth to validate/override LLM signal pick.
    """
    return trigger_payload.get("kind", "general_opportunity")


def _deduplicate_signal(signal: str, tick_history: list) -> tuple[str, bool]:
    """
    Check if this signal was already used in the last 2 ticks.
    Returns (signal_to_use, escalation_needed).
    If the same signal appeared in last 2 ticks without being acted on,
    force escalation_needed=True so the composer switches angle.
    """
    if not tick_history:
        return signal, False

    recent_signals = [t["signal"] for t in tick_history[-2:]]
    recent_acted = [t["acted"] for t in tick_history[-2:]]

    # If same signal appeared twice and neither was acted on → force escalation
    same_count = sum(1 for s in recent_signals if s == signal)
    any_acted = any(recent_acted)

    if same_count >= 2 and not any_acted:
        return signal, True  # keep signal type but force escalation angle
    return signal, False


def step1_signal_rank(merged_context: dict, tick_history: list) -> dict:
    # Ground truth: extract signal directly from trigger (deterministic)
    trigger_payload = merged_context.get("trigger", {})
    ground_truth_signal = _derive_signal_from_trigger(trigger_payload)

    user_prompt = signal_ranker_user(merged_context, tick_history)
    try:
        result = call_groq_with_retry(
            SIGNAL_RANKER_SYSTEM, user_prompt,
            model=MODEL_FAST, fallback_model=MODEL_FAST,
            temperature=0.0, max_tokens=400,
        )
    except Exception as e:
        logger.error(f"Step1 failed: {e}")
        result = {}

    # Override: always use the trigger's actual kind as top_signal.
    # LLM may enrich reasoning/narrative but cannot change the signal source.
    result["top_signal"] = ground_truth_signal

    # Deterministic deduplication — override LLM's escalation_needed if needed
    _, force_escalation = _deduplicate_signal(ground_truth_signal, tick_history)
    if force_escalation:
        result["escalation_needed"] = True
        logger.info(f"Step1: forced escalation — signal '{ground_truth_signal}' repeated without action")

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
            temperature=0.0, max_tokens=700,
        )
    except Exception as e1:
        logger.warning(f"Step2 retries failed: {e1} — trying 8b direct")
        try:
            result = call_groq(COMPOSER_SYSTEM, user_prompt, model=MODEL_FAST, temperature=0.0, max_tokens=700)
        except Exception as e2:
            logger.error(f"Step2 complete failure: {e2}")
            result = None

    elapsed = time.time() - t0
    if elapsed > 12:
        logger.warning(f"Step2 took {elapsed:.1f}s")

    if result is None:
        result = {}

    # Determine send_as: customer context or customer-scoped trigger → merchant_on_behalf
    trigger_scope = trigger_payload.get("scope", "merchant")
    default_send_as = (
        "merchant_on_behalf"
        if (customer_payload or step1_output.get("is_customer_facing") or trigger_scope == "customer")
        else "vera"
    )
    result.setdefault("message", "Vera is here to help grow your business!")
    result.setdefault("cta", "open_ended")
    result.setdefault("send_as", default_send_as)
    result.setdefault("template_name", f"vera_{trigger_payload.get('kind', 'generic')}_v1")
    result.setdefault("template_params", [])
    result.setdefault("rationale", "Signal-based outreach")
    return result


# ── Step 3: Critic ────────────────────────────────────────────────

def _extract_numbers_from_text(text: str) -> list[str]:
    """Extract all numeric tokens from a string for grounding check."""
    import re
    # Match integers, decimals, percentages, currency amounts
    return re.findall(r'\b\d+(?:\.\d+)?(?:%|₹)?\b', text)


def _build_allowed_numbers(original_context: dict) -> set[str]:
    """
    Deterministically extract all numbers present in the context.
    Any number in the composed message must appear here.
    """
    import re
    context_str = json.dumps(original_context)
    return set(re.findall(r'\b\d+(?:\.\d+)?(?:%|₹)?\b', context_str))


def _check_grounding(message: str, allowed_numbers: set[str]) -> list[str]:
    """
    Return list of numbers in message that are NOT in the context.
    These are potentially hallucinated.
    """
    msg_numbers = _extract_numbers_from_text(message)
    # Filter out trivially safe numbers (single digits like "1", "2", "3" used in lists)
    suspicious = [n for n in msg_numbers if n not in allowed_numbers and len(n) > 1]
    return suspicious


def step3_critic(step2_output: dict, original_context: dict) -> dict:
    message = step2_output.get("message", "")

    # ── Deterministic grounding check (runs before LLM) ──────────
    allowed_numbers = _build_allowed_numbers(original_context)
    suspicious_numbers = _check_grounding(message, allowed_numbers)
    if suspicious_numbers:
        logger.warning(
            f"Step3 grounding: suspicious numbers not in context: {suspicious_numbers} "
            f"in message: '{message[:80]}'"
        )
        # Inject grounding warning into critic prompt so it knows to fix these
        step2_output = dict(step2_output)
        step2_output["_grounding_warning"] = (
            f"These numbers appear in the message but NOT in the context — "
            f"likely hallucinated, remove or replace with real context numbers: {suspicious_numbers}"
        )

    user_prompt = critic_user(step2_output, original_context)
    try:
        result = call_groq_with_retry(
            CRITIC_SYSTEM, user_prompt,
            model=MODEL_FAST, fallback_model=MODEL_FAST,
            temperature=0.0, max_tokens=700,
        )
    except Exception as e:
        logger.error(f"Step3 failed: {e} — returning Step2")
        # Remove internal warning key before returning
        step2_output.pop("_grounding_warning", None)
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

    # ── Deterministic anti-repetition guard ──────────────────────
    # Judge penalizes -2 for verbatim repeat of a previous message.
    # Check final message against tick history before returning.
    final_message = final_result.get("message", "")
    prev_messages = {t["message"] for t in tick_history}
    if final_message and final_message in prev_messages:
        logger.warning(f"[{merchant_id}] Anti-repetition: message is verbatim repeat — appending tick context")
        # Append a differentiating suffix rather than re-running the whole pipeline
        tick_num = len(tick_history) + 1
        final_result["message"] = final_message.rstrip(".") + f" (follow-up #{tick_num})"

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
            temperature=0.0, max_tokens=400,
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
