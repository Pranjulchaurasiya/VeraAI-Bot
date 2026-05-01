"""
app.py — Vera Bot Flask API
magicpin India AI Challenge | Author: Pranjul Chaurasiya

Upgrades v2:
- Tick history passed to pipeline for escalation
- register_tick() so /v1/reply can reliably look up merchant
- mark_acted() when merchant says YES
- 70b model for replies
- Cleaner suppression handling
"""

import uuid
import logging
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env file — override=True ensures it works even if var is already set to empty
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
from compose import compose, compose_reply, make_suppression_key
from validator import build_error_response, validate_tick_response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


# ── Health ────────────────────────────────────────────────────────

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "bot": "vera-pranjul",
        "version": "2.0.0",
    }), 200


# ── Metadata ──────────────────────────────────────────────────────

@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "name": "Vera Bot by Pranjul",
        "author": "Pranjul Chaurasiya",
        "model": "groq/llama-3.3-70b-versatile + llama-3.1-8b-instant",
        "version": "2.0.0",
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

        valid_scopes = {"merchant", "customer", "trigger"}
        if scope not in valid_scopes:
            return jsonify(build_error_response(
                f"Invalid scope '{scope}'. Must be one of: {valid_scopes}",
                "INVALID_SCOPE"
            )), 400

        if not isinstance(version, int) or version < 0:
            return jsonify(build_error_response(
                "version must be a non-negative integer", "INVALID_VERSION"
            )), 400

        # Truncate oversized list payloads
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

@app.route("/v1/tick", methods=["POST"])
def tick():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify(build_error_response("Invalid JSON body", "INVALID_JSON")), 400

        required = ["tick_id", "merchant_id", "trigger_id"]
        for field in required:
            if field not in data:
                return jsonify(build_error_response(f"Missing field: {field}", "MISSING_FIELD")), 400

        tick_id = data["tick_id"]
        merchant_id = data["merchant_id"]
        trigger_id = data["trigger_id"]
        customer_id = data.get("customer_id")

        logger.info(f"Tick: {tick_id} | merchant={merchant_id} | trigger={trigger_id}")

        # ── Always-fresh context (handles adaptive injection) ──────
        merchant_payload = get_context("merchant", merchant_id) or {}
        trigger_payload = get_context("trigger", trigger_id) or {}
        customer_payload = get_context("customer", customer_id) if customer_id else None

        category = (
            merchant_payload.get("category")
            or merchant_payload.get("type")
            or merchant_payload.get("business_type")
            or "restaurant"
        ).lower()

        # ── Suppression check ──────────────────────────────────────
        suppression_key = make_suppression_key(merchant_id, trigger_id)
        if is_suppressed(suppression_key):
            logger.info(f"Suppressed: {merchant_id}+{trigger_id}")
            cached = get_cached_message(merchant_id)
            if cached:
                return jsonify({**cached, "tick_id": tick_id}), 200
            return jsonify({
                "tick_id": tick_id,
                "message": "I'll reach out again when the timing is right.",
                "cta": "",
                "send_as": "vera",
                "suppression_key": suppression_key,
                "rationale": "Suppressed: merchant requested silence",
            }), 200

        # ── Load tick history for escalation ──────────────────────
        history = get_merchant_tick_history(merchant_id, last_n=3)
        ignored_streak = get_ignored_streak(merchant_id)
        logger.info(f"[{merchant_id}] tick_history={len(history)} | ignored_streak={ignored_streak}")

        # ── Run 3-step pipeline ────────────────────────────────────
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
            logger.error(f"Pipeline failed for {tick_id}: {e}", exc_info=True)
            cached = get_cached_message(merchant_id)
            if cached:
                return jsonify({**cached, "tick_id": tick_id}), 200
            return jsonify({
                "tick_id": tick_id,
                "message": (
                    f"Your {category} business has a strong opportunity right now. "
                    "Want me to help you act on it?"
                ),
                "cta": "Reply YES to proceed",
                "send_as": "vera",
                "suppression_key": suppression_key,
                "rationale": "Fallback: pipeline unavailable",
            }), 200

        # ── Build final response ───────────────────────────────────
        response = compose(tick_id, merchant_id, trigger_id, pipeline_result)

        valid, msg = validate_tick_response(response)
        if not valid:
            logger.error(f"Schema validation failed for {tick_id}: {msg}")

        # ── Record tick in history ─────────────────────────────────
        record_tick(
            merchant_id=merchant_id,
            tick_id=tick_id,
            signal=pipeline_result.get("top_signal", pipeline_result.get("rationale", "unknown")),
            signal_type=pipeline_result.get("signal_type", "unknown"),
            message=response["message"],
            cta=response["cta"],
        )

        # ── Register tick→merchant mapping for /v1/reply ──────────
        register_tick(
            tick_id=tick_id,
            merchant_id=merchant_id,
            trigger_id=trigger_id,
            cta=response["cta"],
            message=response["message"],
        )

        # ── Store in conversation + cache ──────────────────────────
        add_conversation_turn(tick_id, "vera", response["message"])
        cache_last_message(merchant_id, response)

        logger.info(f"Tick {tick_id} done | cta='{response['cta']}'")
        return jsonify(response), 200

    except Exception as e:
        logger.error(f"/v1/tick error: {e}", exc_info=True)
        return jsonify(build_error_response(str(e), "INTERNAL_ERROR")), 500


# ── Reply ─────────────────────────────────────────────────────────

@app.route("/v1/reply", methods=["POST"])
def reply():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify(build_error_response("Invalid JSON body", "INVALID_JSON")), 400

        required = ["tick_id", "reply_text", "replied_by"]
        for field in required:
            if field not in data:
                return jsonify(build_error_response(f"Missing field: {field}", "MISSING_FIELD")), 400

        tick_id = data["tick_id"]
        reply_text = data["reply_text"]
        replied_by = data["replied_by"]

        logger.info(f"Reply: tick={tick_id} | by={replied_by} | '{reply_text[:60]}'")

        # ── Reliable merchant lookup via tick registry ─────────────
        tick_info = lookup_tick(tick_id)
        if tick_info:
            merchant_id = tick_info["merchant_id"]
            trigger_id = tick_info["trigger_id"]
            original_cta = tick_info["cta"]
            original_message = tick_info["message"]
        else:
            # Fallback: scan last_message_cache
            from state import last_message_cache
            merchant_id = None
            trigger_id = tick_id
            original_cta = ""
            original_message = ""
            for mid, cached in last_message_cache.items():
                if cached.get("tick_id") == tick_id:
                    merchant_id = mid
                    original_cta = cached.get("cta", "")
                    original_message = cached.get("message", "")
                    break
            if not merchant_id:
                merchant_id = tick_id  # last resort

        # ── Store merchant reply ───────────────────────────────────
        add_conversation_turn(tick_id, "merchant", reply_text)

        # ── Get context + history ──────────────────────────────────
        merchant_payload = get_context("merchant", merchant_id) or {}
        category = (
            merchant_payload.get("category")
            or merchant_payload.get("type")
            or merchant_payload.get("business_type")
            or "restaurant"
        ).lower()

        conversation_history = get_last_turns(tick_id, n=3)
        tick_history = get_merchant_tick_history(merchant_id, last_n=5)

        # ── Run reply pipeline (70b) ───────────────────────────────
        try:
            reply_result = run_reply_pipeline(
                reply_text=reply_text,
                conversation_history=conversation_history,
                original_message=original_message,
                original_cta=original_cta,
                category=category,
                merchant_payload=merchant_payload,
                tick_history=tick_history,
            )
        except Exception as e:
            logger.error(f"Reply pipeline failed for {tick_id}: {e}", exc_info=True)
            reply_result = {
                "message": "Thanks for your reply! I'll follow up shortly.",
                "cta": "",
                "send_as": "vera",
                "rationale": "Fallback reply",
                "suppress": False,
                "case_detected": "unknown",
                "mark_acted": False,
            }

        # ── Handle suppression ─────────────────────────────────────
        if reply_result.get("suppress"):
            suppression_key = make_suppression_key(merchant_id, trigger_id)
            set_suppression(suppression_key, f"Merchant: {reply_text[:60]}")
            logger.info(f"Suppression set: merchant={merchant_id}")

        # ── Mark tick as acted if merchant said YES ────────────────
        if reply_result.get("mark_acted"):
            mark_tick_acted(merchant_id, tick_id)
            logger.info(f"Tick {tick_id} marked as acted for merchant={merchant_id}")

        # ── Store Vera's reply in conversation ─────────────────────
        add_conversation_turn(tick_id, "vera", reply_result.get("message", ""))

        # ── Build response ─────────────────────────────────────────
        response = compose_reply(tick_id, merchant_id, trigger_id, reply_result)

        logger.info(f"Reply {tick_id} done | case={reply_result.get('case_detected')}")
        return jsonify(response), 200

    except Exception as e:
        logger.error(f"/v1/reply error: {e}", exc_info=True)
        return jsonify(build_error_response(str(e), "INTERNAL_ERROR")), 500


# ── Root ──────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "bot": "Vera Bot by Pranjul Chaurasiya",
        "challenge": "magicpin India AI Challenge 2026",
        "version": "2.0.0",
        "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"],
    }), 200


# ── Error handlers ────────────────────────────────────────────────

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
