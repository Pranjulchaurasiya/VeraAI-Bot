"""
state.py — In-memory context + conversation store for Vera Bot
Thread-safe. All stores are module-level dicts protected by locks.
"""

import threading
import time
from datetime import datetime, timezone
from collections import defaultdict

# ── Locks ─────────────────────────────────────────────────────────
_context_lock = threading.Lock()
_conv_lock = threading.Lock()
_suppression_lock = threading.Lock()
_tick_history_lock = threading.Lock()
_tick_map_lock = threading.Lock()

# ── Server start time (for uptime_seconds in healthz) ─────────────
SERVER_START_TIME = time.time()

# ── Stores ────────────────────────────────────────────────────────

# context_store: "scope:context_id" → {"version": N, "payload": {...}, "stored_at": "..."}
context_store = {}

# context_counts: scope → count of unique context_ids stored
context_counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}

# conversation_store: conversation_id → [{"role": str, "content": str, "at": str}]
conversation_store = {}

# suppression_store: suppression_key → {"suppressed_at": "...", "reason": "..."}
suppression_store = {}

# last_message_cache: merchant_id → last tick action dict
last_message_cache = {}

# tick_history: merchant_id → list of tick records (chronological)
tick_history = defaultdict(list)

# tick_to_merchant: tick_id/conversation_id → {merchant_id, trigger_id, cta, message}
tick_to_merchant = {}


# ── Context ───────────────────────────────────────────────────────

def store_context(scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, int]:
    """
    Atomically store context.
    Returns (True, 0) if stored.
    Returns (False, current_version) if no-op (same/older version).
    """
    key = f"{scope}:{context_id}"
    with _context_lock:
        existing = context_store.get(key)
        if existing and existing["version"] >= version:
            return False, existing["version"]  # stale — return current version
        is_new = key not in context_store
        context_store[key] = {
            "version": version,
            "payload": payload,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        if is_new and scope in context_counts:
            context_counts[scope] += 1
        return True, version


def get_context(scope: str, context_id: str) -> dict | None:
    """Retrieve context payload by scope and id."""
    key = f"{scope}:{context_id}"
    with _context_lock:
        entry = context_store.get(key)
        return entry["payload"] if entry else None


def get_context_version(scope: str, context_id: str) -> int:
    """Get current stored version for a context."""
    key = f"{scope}:{context_id}"
    with _context_lock:
        entry = context_store.get(key)
        return entry["version"] if entry else 0


def get_contexts_loaded() -> dict:
    """Return count of loaded contexts per scope (for healthz)."""
    with _context_lock:
        return dict(context_counts)


def get_uptime_seconds() -> int:
    """Return server uptime in seconds."""
    return int(time.time() - SERVER_START_TIME)


# ── Conversation ──────────────────────────────────────────────────

def add_conversation_turn(conversation_id: str, role: str, content: str):
    """Append a turn to conversation history."""
    with _conv_lock:
        if conversation_id not in conversation_store:
            conversation_store[conversation_id] = []
        conversation_store[conversation_id].append({
            "role": role,
            "content": content,
            "at": datetime.now(timezone.utc).isoformat(),
        })


def get_last_turns(conversation_id: str, n: int = 5) -> list:
    """Get last N turns of conversation."""
    with _conv_lock:
        history = conversation_store.get(conversation_id, [])
        return history[-n:]


def get_all_turns(conversation_id: str) -> list:
    """Get all turns of a conversation."""
    with _conv_lock:
        return list(conversation_store.get(conversation_id, []))


# ── Suppression ───────────────────────────────────────────────────

def set_suppression(suppression_key: str, reason: str):
    """Mark a suppression key as active."""
    with _suppression_lock:
        suppression_store[suppression_key] = {
            "suppressed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }


def is_suppressed(suppression_key: str) -> bool:
    """Check if a suppression key is active."""
    with _suppression_lock:
        return suppression_key in suppression_store


# ── Tick History ──────────────────────────────────────────────────

def record_tick(
    merchant_id: str,
    tick_id: str,
    signal: str,
    signal_type: str,
    message: str,
    cta: str,
):
    """Record a tick in merchant's history for escalation tracking."""
    with _tick_history_lock:
        history = tick_history[merchant_id]
        tick_num = len(history) + 1
        history.append({
            "tick_num": tick_num,
            "tick_id": tick_id,
            "signal": signal,
            "signal_type": signal_type,
            "message": message,
            "cta": cta,
            "acted": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


def mark_tick_acted(merchant_id: str, tick_id: str):
    """Mark a tick as acted upon (merchant said YES)."""
    with _tick_history_lock:
        for record in tick_history[merchant_id]:
            if record["tick_id"] == tick_id:
                record["acted"] = True
                break


def get_merchant_tick_history(merchant_id: str, last_n: int = 3) -> list:
    """Get last N tick records for a merchant."""
    with _tick_history_lock:
        history = tick_history[merchant_id]
        return list(history[-last_n:])


def get_ignored_streak(merchant_id: str) -> int:
    """How many consecutive ticks the merchant has NOT acted on."""
    with _tick_history_lock:
        history = tick_history[merchant_id]
        streak = 0
        for record in reversed(history):
            if not record["acted"]:
                streak += 1
            else:
                break
        return streak


# ── Tick-to-Merchant Map ──────────────────────────────────────────

def register_tick(
    tick_id: str,
    merchant_id: str,
    trigger_id: str,
    cta: str,
    message: str,
    conversation_id: str = "",
):
    """Register tick/conversation → merchant mapping for /v1/reply lookup."""
    with _tick_map_lock:
        info = {
            "merchant_id": merchant_id,
            "trigger_id": trigger_id,
            "cta": cta,
            "message": message,
            "conversation_id": conversation_id or tick_id,
        }
        tick_to_merchant[tick_id] = info
        if conversation_id and conversation_id != tick_id:
            tick_to_merchant[conversation_id] = info


def lookup_tick(tick_id: str) -> dict | None:
    """Look up merchant info from tick_id or conversation_id."""
    with _tick_map_lock:
        return tick_to_merchant.get(tick_id)


# ── Last Message Cache ────────────────────────────────────────────

def cache_last_message(merchant_id: str, response: dict):
    """Cache last successful tick action for fallback."""
    last_message_cache[merchant_id] = response


def get_cached_message(merchant_id: str) -> dict | None:
    """Retrieve cached last message for a merchant."""
    return last_message_cache.get(merchant_id)
