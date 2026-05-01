"""
compose.py — Final response assembler for Vera Bot
Builds suppression key + enforces schema on all outgoing responses
"""

import hashlib
from datetime import datetime, timezone
from validator import sanitize_message


def make_suppression_key(merchant_id: str, trigger_id: str) -> str:
    """
    MD5 of merchant_id + trigger_id + UTC date.
    Same merchant+trigger on same day always maps to same key.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{merchant_id}{trigger_id}{today}"
    return hashlib.md5(raw.encode()).hexdigest()


def compose(
    tick_id: str,
    merchant_id: str,
    trigger_id: str,
    pipeline_result: dict,
) -> dict:
    """
    Build the final /v1/tick response.
    Enforces schema, sanitizes message length, adds suppression key.
    """
    message = sanitize_message(pipeline_result.get("message", ""), max_words=120)
    cta = pipeline_result.get("cta", "")
    rationale = pipeline_result.get("rationale", "")
    suppression_key = make_suppression_key(merchant_id, trigger_id)

    # Enrich rationale with scores if critic ran
    scores = pipeline_result.get("scores")
    if scores:
        avg = sum(scores.values()) / len(scores)
        rationale = f"[scores avg={avg:.1f}] {rationale}"

    return {
        "tick_id": tick_id,
        "message": message,
        "cta": cta,
        "send_as": "vera",
        "suppression_key": suppression_key,
        "rationale": rationale,
    }


def compose_reply(
    tick_id: str,
    merchant_id: str,
    trigger_id: str,
    reply_result: dict,
) -> dict:
    """
    Build the final /v1/reply response.
    Same schema as /v1/tick.
    """
    message = sanitize_message(reply_result.get("message", ""), max_words=120)
    cta = reply_result.get("cta", "")
    rationale = reply_result.get("rationale", "")
    suppression_key = make_suppression_key(merchant_id, trigger_id)

    return {
        "tick_id": tick_id,
        "message": message,
        "cta": cta,
        "send_as": reply_result.get("send_as", "vera"),
        "suppression_key": suppression_key,
        "rationale": rationale,
        "case_detected": reply_result.get("case_detected", "unknown"),
    }
