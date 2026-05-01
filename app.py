"""
app.py — Vera Bot Flask API v4.0
magicpin India AI Challenge | Author: Pranjul Chaurasiya

Fully spec-compliant with challenge-testing-brief.md and api-call-examples.md:
- /v1/healthz returns uptime_seconds + contexts_loaded counts
- /v1/context returns 409 for stale version
- /v1/tick returns full action schema with conversation_id, template_name, suppression_key
- /v1/reply returns action/wait/end with correct fields
- Category context passed to pipeline for voice-aware composition
- send_as = merchant_on_behalf for customer-scoped triggers
- Auto-reply detection (canned phrase + repeated message)
- Dataset pre-loaded on startup
"""

import uuid
import logging
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(override=True)

from flask import Flask, request, jsonify

from state import (
    store_context, get_context, get_contexts_loaded, get_uptime_seconds,
    add_conversation_turn, get_last_turns, get_all_turns,
    set_suppression, is_suppressed,
    cache_last_message, get_cached_message,
    record_tick, get_merchant_tick_history, get_ignored_streak,
    register_tick, lookup_tick, mark_tick_acted,
)
from agent import run_pipeline, run_reply_pipeline
from validator import build_error_response, sanitize_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

MAX_ACTIONS_PER_TICK = 20

# ── Auto-reply detection patterns ────────────────────────────────
AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "our team will respond",
    "i am currently unavailable",
    "this is an automated",
    "automated assistant",
    "aapki jaankari ke liye",
    "main ek automated",
    "we will get back to you",
    "out of office",
    "currently away",
    "will respond shortly",
    "team tak pahuncha",
]


def is_auto_reply(text: str) -> bool:
    """Detect WhatsApp Business canned auto-reply messages."""
    lower = text.lower()
    return any(pattern in lower for pattern in AUTO_REPLY_PATTERNS)


def extract_category_slug(merchant_payload: dict) -> str:
    """Extract category_slug from merchant payload."""
    return (
        merchant_payload.get("category_slug")
        or merchant_payload.get("category")
        or merchant_payload.get("type")
        or (merchant_payload.get("identity") or {}).get("category_slug")
        or (merchant_payload.get("identity") or {}).get("category")
        or "restaurants"
    ).lower()


def make_suppression_key(merchant_id: str, trigger_id: str) -> str:
    """Use trigger's own suppression_key if available, else generate one."""
    import hashlib
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{merchant_id}{trigger_id}{today}"
    return hashlib.md5(raw.encode()).hexdigest()


def make_conversation_id(merchant_id: str, trigger_id: str) -> str:
    """Generate a meaningful, decodable conversation_id."""
    # Use trigger kind if available
    trigger_payload = get_context("trigger", trigger_id) or {}
    kind = trigger_payload.get("kind", "msg")
    customer_id = trigger_payload.get("customer_id")
    if customer_id:
        return f"conv_{customer_id}_{kind}"
    return f"conv_{merchant_id}_{kind}"


# ── Dataset preloader ─────────────────────────────────────────────

def preload_dataset():
    """
    Pre-load the dataset into context_store on startup.
    This ensures the bot has base context before the judge even calls /v1/context.
    """
    import json
    from pathlib import Path

    dataset_dir = Path(__file__).parent / "dataset"
    if not dataset_dir.exists():
        logger.warning("Dataset directory not found — skipping preload")
        return

    loaded = 0

    # Load categories
    cat_dir = dataset_dir / "categories"
    if cat_dir.exists():
        for f in cat_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                slug = data.get("slug", f.stem)
                stored, _ = store_context("category", slug, 1, data)
                if stored:
                    loaded += 1
            except Exception as e:
                logger.warning(f"Failed to load category {f.name}: {e}")

    # Load merchants
    merchants_file = dataset_dir / "merchants_seed.json"
    if merchants_file.exists():
        try:
            data = json.loads(merchants_file.read_text(encoding="utf-8"))
            for merchant in data.get("merchants", []):
                mid = merchant.get("merchant_id")
                if mid:
                    stored, _ = store_context("merchant", mid, 1, merchant)
                    if stored:
                        loaded += 1
        except Exception as e:
            logger.warning(f"Failed to load merchants: {e}")

    # Load customers
    customers_file = dataset_dir / "customers_seed.json"
    if customers_file.exists():
        try:
            data = json.loads(customers_file.read_text(encoding="utf-8"))
            for customer in data.get("customers", []):
                cid = customer.get("customer_id")
                if cid:
                    stored, _ = store_context("customer", cid, 1, customer)
                    if stored:
                        loaded += 1
        except Exception as e:
            logger.warning(f"Failed to load customers: {e}")

    # Load triggers
    triggers_file = dataset_dir / "triggers_seed.json"
    if triggers_file.exists():
        try:
            data = json.loads(triggers_file.read_text(encoding="utf-8"))
            for trigger in data.get("triggers", []):
                tid = trigger.get("id")
                if tid:
                    stored, _ = store_context("trigger", tid, 1, trigger)
                    if stored:
                        loaded += 1
        except Exception as e:
            logger.warning(f"Failed to load triggers: {e}")

    logger.info(f"Dataset preloaded: {loaded} contexts")


# ── Health ────────────────────────────────────────────────────────

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "uptime_seconds": get_uptime_seconds(),
        "contexts_loaded": get_contexts_loaded(),
        "bot": "vera-pranjul",
        "version": "4.0.0",
    }), 200


# ── Metadata ──────────────────────────────────────────────────────

@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "team_name": "Pranjul Chaurasiya",
        "team_members": ["Pranjul Chaurasiya"],
        "model": "groq/llama-3.3-70b-versatile + llama-3.1-8b-instant",
        "approach": (
            "3-step agentic: signal_rank → compose(category-voice-aware) → critic | "
            "70b replies | tick memory + escalation | auto-reply detection | "
            "dataset pre-loaded | merchant_on_behalf for customer triggers"
        ),
        "contact_email": "research.pranjul@gmail.com",
        "version": "4.0.0",
        "submitted_at": "2026-05-02T00:00:00Z",
    }), 200


# ── Context ───────────────────────────────────────────────────────

@app.route("/v1/context", methods=["POST"])
def context():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"accepted": False, "reason": "invalid_json"}), 400

        required = ["scope", "context_id", "version", "payload"]
        for field in required:
            if field not in data:
                return jsonify({"accepted": False, "reason": "missing_field", "details": f"Missing: {field}"}), 400

        scope = data["scope"]
        context_id = data["context_id"]
        version = data["version"]
        payload = data["payload"]

        valid_scopes = {"merchant", "customer", "trigger", "category"}
        if scope not in valid_scopes:
            return jsonify({"accepted": False, "reason": "invalid_scope", "details": f"Must be one of: {valid_scopes}"}), 400

        if not isinstance(version, int) or version < 0:
            return jsonify({"accepted": False, "reason": "invalid_version", "details": "version must be non-negative int"}), 400

        # Truncate oversized list payloads
        if isinstance(payload, list) and len(payload) > 50:
            payload = payload[-3:]
            logger.warning(f"Truncated oversized payload for {scope}:{context_id}")

        stored, current_version = store_context(scope, context_id, version, payload)

        if not stored:
            # Stale version — return 409 as per spec
            return jsonify({
                "accepted": False,
                "reason": "stale_version",
                "current_version": current_version,
            }), 409

        logger.info(f"Context stored: {scope}:{context_id} v{version}")
        return jsonify({
            "accepted": True,
            "ack_id": f"ack_{context_id}_v{version}",
            "stored_at": datetime.now(timezone.utc).isoformat() + "Z",
        }), 200

    except Exception as e:
        logger.error(f"/v1/context error: {e}", exc_info=True)
        return jsonify({"accepted": False, "reason": "internal_error", "details": str(e)}), 500


# ── Tick ──────────────────────────────────────────────────────────

@app.route("/v1/tick", methods=["POST"])
def tick():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"actions": []}), 200

        now = data.get("now", datetime.now(timezone.utc).isoformat())
        available_triggers = data.get("available_triggers", [])

        logger.info(f"Tick: {len(available_triggers)} triggers | now={now}")

        if not available_triggers:
            return jsonify({"actions": []}), 200

        actions = []
        for trigger_id in available_triggers[:MAX_ACTIONS_PER_TICK]:
            try:
                action = _process_trigger(trigger_id, now)
                if action:
                    actions.append(action)
            except Exception as e:
                logger.error(f"Error processing trigger {trigger_id}: {e}", exc_info=True)
                continue

        logger.info(f"Tick done: {len(actions)} actions")
        return jsonify({"actions": actions}), 200

    except Exception as e:
        logger.error(f"/v1/tick error: {e}", exc_info=True)
        return jsonify({"actions": []}), 200


def _process_trigger(trigger_id: str, now: str) -> dict | None:
    """Process one trigger → return action dict or None to skip."""
    trigger_payload = get_context("trigger", trigger_id) or {}

    # Extract merchant_id and customer_id from trigger
    merchant_id = (
        trigger_payload.get("merchant_id")
        or (trigger_payload.get("payload") or {}).get("merchant_id")
        or trigger_id
    )
    customer_id = (
        trigger_payload.get("customer_id")
        or (trigger_payload.get("payload") or {}).get("customer_id")
    )

    merchant_payload = get_context("merchant", merchant_id) or {}
    customer_payload = get_context("customer", customer_id) if customer_id else None

    # Get category and category context
    category_slug = extract_category_slug(merchant_payload)
    category_payload = get_context("category", category_slug) or {}

    # Use trigger's own suppression_key if present
    suppression_key = (
        trigger_payload.get("suppression_key")
        or make_suppression_key(merchant_id, trigger_id)
    )

    if is_suppressed(suppression_key):
        logger.info(f"Suppressed: {merchant_id}+{trigger_id}")
        return None

    # Load tick history for escalation
    history = get_merchant_tick_history(merchant_id, last_n=3)

    # Run pipeline
    try:
        pipeline_result = run_pipeline(
            merchant_id=merchant_id,
            trigger_id=trigger_id,
            merchant_payload=merchant_payload,
            trigger_payload=trigger_payload,
            category_payload=category_payload,
            customer_payload=customer_payload,
            category=category_slug,
            tick_history=history,
        )
    except Exception as e:
        logger.error(f"Pipeline failed for {trigger_id}: {e}", exc_info=True)
        cached = get_cached_message(merchant_id)
        if cached:
            pipeline_result = cached
        else:
            owner = (merchant_payload.get("identity") or {}).get("owner_first_name", "")
            pipeline_result = {
                "message": f"{owner + ', ' if owner else ''}there's a strong opportunity right now. Want me to help?",
                "cta": "open_ended",
                "send_as": "vera",
                "template_name": "vera_generic_v1",
                "template_params": [],
                "rationale": "Fallback: pipeline unavailable",
            }

    message = sanitize_message(pipeline_result.get("message", ""), max_words=120)
    if not message.strip():
        return None  # skip empty messages — judge penalizes them

    cta = pipeline_result.get("cta", "open_ended")
    send_as = pipeline_result.get("send_as", "vera")
    template_name = pipeline_result.get("template_name", f"vera_{trigger_payload.get('kind', 'generic')}_v1")
    template_params = pipeline_result.get("template_params", [])
    rationale = pipeline_result.get("rationale", "")

    # Enrich rationale with critic scores
    scores = pipeline_result.get("scores")
    if scores:
        avg = sum(scores.values()) / len(scores)
        rationale = f"[scores avg={avg:.1f}] {rationale}"

    # Generate meaningful conversation_id
    conversation_id = make_conversation_id(merchant_id, trigger_id)

    action = {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": send_as,
        "trigger_id": trigger_id,
        "template_name": template_name,
        "template_params": template_params,
        "body": message,
        "cta": cta,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }

    # Record in state
    tick_id = f"tick_{trigger_id}_{uuid.uuid4().hex[:8]}"
    record_tick(
        merchant_id=merchant_id,
        tick_id=tick_id,
        signal=pipeline_result.get("top_signal", trigger_payload.get("kind", "unknown")),
        signal_type=trigger_payload.get("kind", "unknown"),
        message=message,
        cta=cta,
    )
    register_tick(
        tick_id=tick_id,
        merchant_id=merchant_id,
        trigger_id=trigger_id,
        cta=cta,
        message=message,
        conversation_id=conversation_id,
    )
    add_conversation_turn(conversation_id, "vera", message)
    cache_last_message(merchant_id, {**action, "tick_id": tick_id})

    return action


# ── Reply ─────────────────────────────────────────────────────────

@app.route("/v1/reply", methods=["POST"])
def reply():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"action": "send", "body": "", "rationale": "empty request"}), 200

        conversation_id = data.get("conversation_id", "")
        merchant_id = data.get("merchant_id", "")
        customer_id = data.get("customer_id")
        from_role = data.get("from_role", "merchant")
        message_text = data.get("message", "")
        turn_number = data.get("turn_number", 1)

        if not message_text:
            return jsonify({"action": "end", "rationale": "empty message"}), 200

        logger.info(f"Reply: conv={conversation_id} | turn={turn_number} | '{message_text[:60]}'")

        # Resolve merchant_id from conversation registry if not provided
        if not merchant_id:
            tick_info = lookup_tick(conversation_id)
            if tick_info:
                merchant_id = tick_info["merchant_id"]
        if not merchant_id:
            merchant_id = conversation_id or "unknown"

        # Store incoming turn
        add_conversation_turn(conversation_id, from_role, message_text)

        # ── Auto-reply detection (fast path — no LLM needed) ──────
        if is_auto_reply(message_text):
            all_turns = get_all_turns(conversation_id)
            merchant_turns = [t["content"] for t in all_turns if t.get("role") == from_role]

            if turn_number >= 4 or len(merchant_turns) >= 3:
                # 3+ auto-replies → end
                logger.info(f"Auto-reply x3 detected at turn {turn_number} — ending")
                return jsonify({
                    "action": "end",
                    "rationale": "Auto-reply detected 3+ times — owner not at phone. Closing conversation.",
                }), 200
            elif turn_number >= 3 or len(merchant_turns) >= 2:
                # 2nd auto-reply → wait 4 hours
                logger.info(f"Auto-reply x2 at turn {turn_number} — waiting 4h")
                return jsonify({
                    "action": "wait",
                    "wait_seconds": 14400,
                    "rationale": "Same auto-reply twice — backing off 4 hours to wait for owner.",
                }), 200
            else:
                # First auto-reply → flag it for owner
                logger.info(f"Auto-reply detected at turn {turn_number} — flagging for owner")
                add_conversation_turn(conversation_id, "vera", "")
                return jsonify({
                    "action": "send",
                    "body": "Looks like an auto-reply 😊 When the owner sees this, just reply 'Yes' to continue.",
                    "cta": "binary_yes_no",
                    "send_as": "vera",
                    "wait_seconds": 0,
                    "rationale": "Detected auto-reply; one prompt to flag it for the owner.",
                }), 200

        # ── Get context ───────────────────────────────────────────
        merchant_payload = get_context("merchant", merchant_id) or {}
        category_slug = extract_category_slug(merchant_payload)
        conversation_history = get_last_turns(conversation_id, n=5)
        tick_history = get_merchant_tick_history(merchant_id, last_n=5)

        # Get original message/CTA
        tick_info = lookup_tick(conversation_id)
        original_message = ""
        original_cta = ""
        if tick_info:
            original_message = tick_info.get("message", "")
            original_cta = tick_info.get("cta", "")
        else:
            cached = get_cached_message(merchant_id)
            if cached:
                original_message = cached.get("body", "")
                original_cta = cached.get("cta", "")

        # ── Run reply pipeline ────────────────────────────────────
        try:
            reply_result = run_reply_pipeline(
                reply_text=message_text,
                conversation_history=conversation_history,
                original_message=original_message,
                original_cta=original_cta,
                category=category_slug,
                merchant_payload=merchant_payload,
                tick_history=tick_history,
                turn_number=turn_number,
            )
        except Exception as e:
            logger.error(f"Reply pipeline failed: {e}", exc_info=True)
            reply_result = {
                "message": "Thanks for your reply! I'll follow up shortly.",
                "cta": "",
                "send_as": "vera",
                "action": "send",
                "wait_seconds": 0,
                "rationale": "Fallback reply",
                "suppress": False,
                "case_detected": "unknown",
                "mark_acted": False,
            }

        case = reply_result.get("case_detected", "unknown")
        judge_action = reply_result.get("action", "send")

        # Handle suppression
        if reply_result.get("suppress") or judge_action == "end":
            trigger_id = (tick_info or {}).get("trigger_id", conversation_id)
            suppression_key = make_suppression_key(merchant_id, trigger_id)
            set_suppression(suppression_key, f"Merchant: {message_text[:60]}")
            logger.info(f"Suppression set: merchant={merchant_id}")

        # Mark acted
        if reply_result.get("mark_acted"):
            tick_id = (tick_info or {}).get("tick_id", conversation_id)
            mark_tick_acted(merchant_id, tick_id)

        reply_body = sanitize_message(reply_result.get("message", ""), max_words=120)

        # Store Vera's reply
        if reply_body:
            add_conversation_turn(conversation_id, "vera", reply_body)

        response = {
            "action": judge_action,
            "body": reply_body if judge_action == "send" else "",
            "cta": reply_result.get("cta", "") if judge_action == "send" else "",
            "send_as": reply_result.get("send_as", "vera"),
            "wait_seconds": reply_result.get("wait_seconds", 0),
            "rationale": reply_result.get("rationale", ""),
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


# ── Teardown (optional — wipe state at end of test) ───────────────

@app.route("/v1/teardown", methods=["POST"])
def teardown():
    """Optional endpoint — judge may call this at end of test."""
    from state import (
        context_store, conversation_store, suppression_store,
        last_message_cache, tick_history, tick_to_merchant, context_counts
    )
    context_store.clear()
    conversation_store.clear()
    suppression_store.clear()
    last_message_cache.clear()
    tick_history.clear()
    tick_to_merchant.clear()
    for k in context_counts:
        context_counts[k] = 0
    logger.info("State wiped via /v1/teardown")
    return jsonify({"status": "wiped"}), 200


# ── Root ──────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "bot": "Vera Bot by Pranjul Chaurasiya",
        "challenge": "magicpin India AI Challenge 2026",
        "version": "4.0.0",
        "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"],
    }), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify(build_error_response("Not found", "NOT_FOUND")), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify(build_error_response("Method not allowed", "METHOD_NOT_ALLOWED")), 405

@app.errorhandler(413)
def payload_too_large(e):
    return jsonify(build_error_response("Payload too large", "PAYLOAD_TOO_LARGE")), 413


# ── Startup ───────────────────────────────────────────────────────

with app.app_context():
    preload_dataset()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
