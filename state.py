"""
state.py — In-memory context + conversation store for Vera Bot
Thread-safe. All stores are module-level dicts protected by locks.
"""

import threading
from datetime import datetime, timezone
from collections import defaultdict

# ── Locks ─────────────────────────────────────────────────────────
_context_lock = threading.Lock()
_conv_lock = threading.Lock()
_suppression_lock = threading.Lock()
_tick_history_lock = threading.Lock()
_tick_map_lock = threading.Lock()

# ── Stores ────────────────────────────────────────────────────────

# context_store: "scope:context_id" → {"version": N, "payload": {...}, "stored_at": "..."}
context_store = {}

# conversation_store: tick_id → [{"role": "vera"|"merchant", "content": "..."}]
conversation_store = {}

# suppression_store: suppression_key → {"suppressed_at": "...", "reason": "..."}
suppression_store = {}

# last_message_cache: merchant_id → last tick response dict (fallback on Groq failure)
last_message_cache = {}

# tick_history: merchant_id → list of tick records (chronological)
# Each record: {tick_id, tick_num, signal, signal_type, message, cta, acted, timestamp}
tick_history = defaultdict(list)

# tick_to_merchant: tick_id → {merchant_id, trigger_id, cta, message}
# Enables /v1/reply to look up merchant from tick_id reliably
tick_to_merchant = {}


# ── Context ───────────────────────────────────────────────────────

def store_context(scope: str, context_id: str, version: int, payload: dict) -> bool:
    """
    Atomically store context. Returns True if stored, False if no-op (same/older version).
    """
    key = f"{scope}:{context_id}"
    with _context_lock:
        existing = context_store.get(key)
        if existing and existing["version"] >= version:
            return False  # no-op
        context_store[key] = {
            "version": version,
            "payload": payload,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        return True


def get_context(scope: str, context_id: str) -> dict | None:
    """Retrieve context payload by scope and id."""
    key = f"{scope}:{context_id}"
    with _context_lock:
        entry = context_store.get(key)
        return entry["payload"] if entry else None


def get_context_entry(scope: str, context_id: str) -> dict | None:
    """Retrieve full context entry (with version/stored_at)."""
    key = f"{scope}:{context_id}"
    with _context_lock:
        return context_store.get(key)


# ── Conversation ──────────────────────────────────────────────────

def add_conversation_turn(tick_id: str, role: str, content: str):
    """Append a turn to conversation history for a tick."""
    with _conv_lock:
        if tick_id not in conversation_store:
            conversation_store[tick_id] = []
        conversation_store[tick_id].append({
            "role": role,
            "content": content,
            "at": datetime.now(timezone.utc).isoformat(),
        })


def get_last_turns(tick_id: str, n: int = 3) -> list:
    """Get last N turns of conversation for a tick."""
    with _conv_lock:
        history = conversation_store.get(tick_id, [])
        return history[-n:]


# ── Suppression ───────────────────────────────────────────────────

def set_suppression(suppression_key: str, reason: str):
    """Mark a merchant+trigger+date combo as suppressed."""
    with _suppression_lock:
        suppression_store[suppression_key] = {
            "suppressed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }


def is_suppressed(suppression_key: str) -> bool:
    """Check if a suppression key is active."""
    with _suppression_lock:
        return suppression_key in suppression_store


# ── Tick History (NEW — enables escalation across 12 ticks) ───────

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
            "acted": False,  # updated when merchant replies YES
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


def get_tick_count(merchant_id: str) -> int:
    """How many ticks has this merchant received so far."""
    with _tick_history_lock:
        return len(tick_history[merchant_id])


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


# ── Tick-to-Merchant Map (NEW — fixes reply route lookup) ─────────

def register_tick(tick_id: str, merchant_id: str, trigger_id: str, cta: str, message: str):
    """Register tick_id → merchant mapping so /v1/reply can look it up."""
    with _tick_map_lock:
        tick_to_merchant[tick_id] = {
            "merchant_id": merchant_id,
            "trigger_id": trigger_id,
            "cta": cta,
            "message": message,
        }


def lookup_tick(tick_id: str) -> dict | None:
    """Look up merchant info from tick_id."""
    with _tick_map_lock:
        return tick_to_merchant.get(tick_id)


# ── Last Message Cache ────────────────────────────────────────────

def cache_last_message(merchant_id: str, response: dict):
    """Cache last successful tick response for fallback."""
    last_message_cache[merchant_id] = response


def get_cached_message(merchant_id: str) -> dict | None:
    """Retrieve cached last message for a merchant."""
    return last_message_cache.get(merchant_id)
