"""
validator.py — Response schema validator for Vera Bot
"""

import re
from typing import Any


REQUIRED_TICK_FIELDS = {"tick_id", "message", "cta", "send_as", "suppression_key", "rationale"}
REQUIRED_CONTEXT_FIELDS = {"accepted", "ack_id", "stored_at"}
REQUIRED_REPLY_FIELDS = {"tick_id", "message", "cta", "send_as", "suppression_key", "rationale"}


def validate_tick_response(data: dict) -> tuple[bool, str]:
    """Validate /v1/tick response schema."""
    missing = REQUIRED_TICK_FIELDS - set(data.keys())
    if missing:
        return False, f"Missing fields: {missing}"
    if not isinstance(data.get("message"), str) or len(data["message"].strip()) == 0:
        return False, "message must be a non-empty string"
    if not isinstance(data.get("cta"), str):
        return False, "cta must be a string"
    if data.get("send_as") != "vera":
        return False, "send_as must be 'vera'"
    if not isinstance(data.get("suppression_key"), str):
        return False, "suppression_key must be a string"
    return True, "ok"


def validate_context_response(data: dict) -> tuple[bool, str]:
    """Validate /v1/context response schema."""
    missing = REQUIRED_CONTEXT_FIELDS - set(data.keys())
    if missing:
        return False, f"Missing fields: {missing}"
    if not isinstance(data.get("accepted"), bool):
        return False, "accepted must be a boolean"
    return True, "ok"


def sanitize_message(message: str, max_words: int = 120) -> str:
    """Trim message to max_words if it exceeds limit."""
    words = message.split()
    if len(words) > max_words:
        return " ".join(words[:max_words]) + "..."
    return message


def truncate_payload(payload: Any, max_items: int = 3) -> Any:
    """Truncate oversized payload to most recent N items if it's a list."""
    if isinstance(payload, list) and len(payload) > max_items:
        return payload[-max_items:]
    return payload


def build_error_response(message: str, code: str) -> dict:
    """Standard error response format."""
    return {"error": message, "code": code}


def is_valid_iso_timestamp(ts: str) -> bool:
    """Basic ISO 8601 timestamp validation."""
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    return bool(re.match(pattern, ts))
