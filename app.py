"""
app.py — Vera Bot Flask API  v3.0
magicpin India AI Challenge | Author: Pranjul Chaurasiya

REAL JUDGE SCHEMA (from judge_simulator.py):
  POST /v1/tick   → receives {now, available_triggers:[...]}
                  → returns  {actions:[{trigger_id, merchant_id, customer_id,
                                        body, cta, send_as, rationale}]}
  POST /v1/reply  → receives {conversation_id, merchant_id, customer_id,
                               from_role, message, received_at, turn_number}
                  → returns  {action:"send|end|wait", body, cta, send_as,
                               wait_seconds, rationale}
"""

import uuid
import logging
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(override=True)

from flask import Flask, request, jsonify

from state import (
    store_context, get_context,
    add_conversation_turn, get_last_turns,
    set_suppression, is_suppressed,
    cache_last_message, get_cached_message,
    record_tick, get_merchant_tick_history, get_ignored_streak,
    register_tick, lookup_tick, mark_tick_acted,
)
from agent import run_pipeline, run_reply_pipeline
from compose import make_suppression_key
from validator import build_error_response, sanitize_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

MAX_ACTIONS_PER_TICK = 20


def extract_category(payload: dict) -> str:
    """Extract category from any payload structure (flat or nested)."""
    return (
        payload.get("category")
        or payload.get("type")
        or payload.get("business_type")
        or payload.get("category_slug")
        or (payload.get("identity") or {}).get("category")
        or (payload.get("identity") or {}).get("category_slug")
        or (payload.get("identity") or {}).get("type")
        or "restaurant"
    ).lower()


# ── Health ────────────────────────────────────────────────────────

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "bot": "vera-pranjul",
        "version": "3.0.0",
    }), 200


# ── Metadata ──────────────────────────────────────────────────────

@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "team_name": "Pranjul Chaurasiya",          # judge reads this field
        "name": "Vera Bot by Pranjul",
        "author": "Pranjul Chaurasiya",
        "model": "groq/llama-3.3-70b-versatile + llama-3.1-8b-instant",
        "version": "3.0.0",
        "capabilities": ["context", "tick", "reply"],
        "approach": (
            "3-step agentic: signal_rank+synthesis → compose+escalation → critic | "
            "70b replies | tick memory | perfect storm detection"
        ),
        "avg_response_ms": 7000,
    }), 200


# ── Context ───────────────────────────────────────────────────────

@app.route("/v1/context", methods=["POST"])
def context():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify(build_error_response("Invalid JSON body", "INVALID_JSON")), 400

        required = ["scope", "context_id", "version", "payload"]
        for field in required:
            if field not in data:
                return jsonify(build_error_response(f"Missing field: {field}", "MISSING_FIELD")), 400

        scope = data["scope"]
        context_id = data["context_id"]
        version = data["version"]
        payload = data["payload"]

        valid_scopes = {"merchant", "customer", "trigger", "category"}
        if scope not in valid_scopes:
            return jsonify(build_error_response(
                f"Invalid scope '{scope}'. Must be one of: {valid_scopes}",
                "INVALID_SCOPE"
            )), 400

        if not isinstance(version, int) or version < 0:
            return jsonify(build_error_response(
                "version must be a non-negative integer", "INVALID_VERSION"
            )), 400

        if isinstance(payload, list) and len(payload) > 50:
            payload = payload[-3:]
            logger.warning(f"Truncated oversized payload for {scope}:{context_id}")

        stored = store_context(scope, context_id, version, payload)
        logger.info(f"Context {'stored' if stored else 'no-op'}: {scope}:{context_id} v{version}")

        return jsonify({
            "accepted": True,
            "ack_id": f"ack_{uuid.uuid4()}",
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }), 200

    except Exception as e:
        logger.error(f"/v1/context error: {e}", exc_info=True)
        return jsonify(build_error_response(str(e), "INTERNAL_ERROR")), 500


# ── Tick ──────────────────────────────────────────────────────────
# Real judge schema:
#   Request:  { "now": "ISO", "available_triggers": ["tid1", "tid2", ...] }
#   Response: { "actions": [ { trigger_id, merchant_id, customer_id,
#                               body, cta, send_as, rationale }, ... ] }

@app.route("/v1/tick", methods=["POST"])
def tick():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify(build_error_response("Invalid JSON body", "INVALID_JSON")), 400

        now = data.get("now", datetime.now(timezone.utc).isoformat())
        available_triggers = data.get("available_triggers", [])

        logger.info(f"Tick: {len(available_triggers)} triggers | now={now}")

        if not available_triggers:
            return jsonify({"actions": []}), 200

        # Cap at 20 actions per tick
        triggers_to_process = available_triggers[:MAX_ACTIONS_PER_TICK]
        actions = []

        for trigger_id in triggers_to_process:
            try:
                action = _process_trigger(trigger_id, now)
                if action:
                    actions.append(action)
            except Exception as e:
                logger.error(f"Error processing trigger {trigger_id}: {e}", exc_info=True)
                continue

        logger.info(f"Tick done: {len(actions)} actions produced")
        return jsonify({"actions": actions}), 200

    except Exception as e:
        logger.error(f"/v1/tick error: {e}", exc_info=True)
        return jsonify({"actions": []}), 200  # never crash — return empty actions


def _process_trigger(trigger_id: str, now: str) -> dict | None:
    """Process one trigger and return an action dict, or None to skip."""
    trigger_payload = get_context("trigger", trigger_id) or {}

    # Get merchant_id from trigger payload
    merchant_id = (
        trigger_payload.get("merchant_id")
        or trigger_payload.get("mid")
        or (trigger_payload.get("payload") or {}).get("merchant_id")
        or trigger_id  # fallback
    )

    # Get customer_id from trigger payload (optional)
    customer_id = (
        trigger_payload.get("customer_id")
        or trigger_payload.get("cid")
        or (trigger_payload.get("payload") or {}).get("customer_id")
    )

    merchant_payload = get_context("merchant", merchant_id) or {}
    customer_payload = get_context("customer", customer_id) if customer_id else None
    category = extract_category(merchant_payload)

    # Suppression check
    suppression_key = make_suppression_key(merchant_id, trigger_id)
    if is_suppressed(suppression_key):
        logger.info(f"Suppressed: {merchant_id}+{trigger_id}")
        return None  # skip suppressed merchants

    # Load tick history for escalation
    history = get_merchant_tick_history(merchant_id, last_n=3)

    # Run 3-step pipeline
    try:
        pipeline_result = run_pipeline(
            merchant_id=merchant_id,
            trigger_id=trigger_id,
            merchant_payload=merchant_payload,
            trigger_payload=trigger_payload,
            customer_payload=customer_payload,
            category=category,
            tick_history=history,
        )
    except Exception as e:
        logger.error(f"Pipeline failed for {trigger_id}: {e}", exc_info=True)
        cached = get_cached_message(merchant_id)
        if cached:
            pipeline_result = {
                "message": cached.get("body", cached.get("message", "")),
                "cta": cached.get("cta", ""),
                "rationale": "Cached fallback",
            }
        else:
            pipeline_result = {
                "message": f"Great opportunity for your {category} business right now. Want me to help?",
                "cta": "Reply YES",
                "rationale": "Fallback: pipeline unavailable",
            }

    message = sanitize_message(pipeline_result.get("message", ""), max_words=120)
    cta = pipeline_result.get("cta", "")
    rationale = pipeline_result.get("rationale", "")

    # Add critic scores to rationale if available
    scores = pipeline_result.get("scores")
    if scores:
        avg = sum(scores.values()) / len(scores)
        rationale = f"[scores avg={avg:.1f}] {rationale}"

    # Build action in real judge format
    action = {
        "trigger_id": trigger_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "body": message,
        "cta": cta,
        "send_as": "vera",
        "rationale": rationale,
    }

    # Record in tick history
    tick_id = f"tick_{trigger_id}_{uuid.uuid4().hex[:8]}"
    record_tick(
        merchant_id=merchant_id,
        tick_id=tick_id,
        signal=pipeline_result.get("top_signal", "unknown"),
        signal_type=pipeline_result.get("signal_type", "unknown"),
        message=message,
        cta=cta,
    )
    register_tick(
        tick_id=tick_id,
        merchant_id=merchant_id,
        trigger_id=trigger_id,
        cta=cta,
        message=message,
    )
    add_conversation_turn(tick_id, "vera", message)
    cache_last_message(merchant_id, {**action, "tick_id": tick_id})

    return action


# ── Reply ─────────────────────────────────────────────────────────
# Real judge schema:
#   Request:  { conversation_id, merchant_id, customer_id,
#               from_role, message, received_at, turn_number }
#   Response: { action: "send|end|wait", body, cta, send_as,
#               wait_seconds, rationale }

@app.route("/v1/reply", methods=["POST"])
def reply():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify(build_error_response("Invalid JSON body", "INVALID_JSON")), 400

        # Real judge fields
        conversation_id = data.get("conversation_id", "")
        merchant_id = data.get("merchant_id", "")
        customer_id = data.get("customer_id")
        from_role = data.get("from_role", "merchant")
        message_text = data.get("message", data.get("reply_text", ""))
        turn_number = data.get("turn_number", 1)

        # Also support legacy tick_id field for backward compat
        tick_id = data.get("tick_id", conversation_id)

        if not message_text:
            return jsonify(build_error_response("Missing field: message", "MISSING_FIELD")), 400

        logger.info(f"Reply: conv={conversation_id} | merchant={merchant_id} | turn={turn_number} | '{message_text[:60]}'")

        # If merchant_id not provided, try to look up from conversation
        if not merchant_id:
            tick_info = lookup_tick(tick_id)
            if tick_info:
                merchant_id = tick_info["merchant_id"]

        if not merchant_id:
            merchant_id = conversation_id or "unknown"

        # Store incoming message
        add_conversation_turn(conversation_id, from_role, message_text)

        # Get context
        merchant_payload = get_context("merchant", merchant_id) or {}
        category = extract_category(merchant_payload)
        conversation_history = get_last_turns(conversation_id, n=3)
        tick_history = get_merchant_tick_history(merchant_id, last_n=5)

        # Get original message/CTA from conversation or cache
        original_message = ""
        original_cta = ""
        tick_info = lookup_tick(tick_id)
        if tick_info:
            original_message = tick_info.get("message", "")
            original_cta = tick_info.get("cta", "")
        else:
            cached = get_cached_message(merchant_id)
            if cached:
                original_message = cached.get("body", cached.get("message", ""))
                original_cta = cached.get("cta", "")

        # Detect auto-reply pattern (turn 4+ with same message = end)
        if turn_number >= 4:
            history = get_last_turns(conversation_id, n=4)
            messages = [t["content"] for t in history if t["role"] == from_role]
            if len(messages) >= 3 and len(set(messages[-3:])) == 1:
                logger.info(f"Auto-reply detected at turn {turn_number} — ending")
                return jsonify({
                    "action": "end",
                    "body": "",
                    "cta": "",
                    "send_as": "vera",
                    "wait_seconds": 0,
                    "rationale": "Auto-reply pattern detected",
                }), 200

        # Run reply pipeline
        try:
            reply_result = run_reply_pipeline(
                reply_text=message_text,
                conversation_history=conversation_history,
                original_message=original_message,
                original_cta=original_cta,
                category=category,
                merchant_payload=merchant_payload,
                tick_history=tick_history,
            )
        except Exception as e:
            logger.error(f"Reply pipeline failed: {e}", exc_info=True)
            reply_result = {
                "message": "Thanks for your reply! I'll follow up shortly.",
                "cta": "",
                "send_as": "vera",
                "rationale": "Fallback reply",
                "suppress": False,
                "case_detected": "unknown",
                "mark_acted": False,
            }

        case = reply_result.get("case_detected", "unknown")

        # Handle suppression (hostile case)
        if reply_result.get("suppress"):
            trigger_id = (tick_info or {}).get("trigger_id", conversation_id)
            suppression_key = make_suppression_key(merchant_id, trigger_id)
            set_suppression(suppression_key, f"Merchant: {message_text[:60]}")
            logger.info(f"Suppression set: merchant={merchant_id}")

        # Mark acted if YES
        if reply_result.get("mark_acted"):
            mark_tick_acted(merchant_id, tick_id)

        # Map case to judge action field
        if case == "hostile" or reply_result.get("suppress"):
            judge_action = "end"
        elif case == "handoff":
            judge_action = "wait"
        else:
            judge_action = "send"

        reply_body = sanitize_message(reply_result.get("message", ""), max_words=120)

        # Store Vera's reply
        add_conversation_turn(conversation_id, "vera", reply_body)

        response = {
            "action": judge_action,
            "body": reply_body,
            "cta": reply_result.get("cta", ""),
            "send_as": reply_result.get("send_as", "vera"),
            "wait_seconds": 300 if judge_action == "wait" else 0,
            "rationale": reply_result.get("rationale", ""),
            "case_detected": case,
        }

        logger.info(f"Reply done | case={case} | action={judge_action}")
        return jsonify(response), 200

    except Exception as e:
        logger.error(f"/v1/reply error: {e}", exc_info=True)
        return jsonify({
            "action": "send",
            "body": "Thanks for your message! I'll be in touch.",
            "cta": "",
            "send_as": "vera",
            "wait_seconds": 0,
            "rationale": "Fallback",
        }), 200


# ── Root ──────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "bot": "Vera Bot by Pranjul Chaurasiya",
        "challenge": "magicpin India AI Challenge 2026",
        "version": "3.0.0",
        "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"],
    }), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify(build_error_response("Endpoint not found", "NOT_FOUND")), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify(build_error_response("Method not allowed", "METHOD_NOT_ALLOWED")), 405

@app.errorhandler(413)
def payload_too_large(e):
    return jsonify(build_error_response("Payload too large (max 500KB)", "PAYLOAD_TOO_LARGE")), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
