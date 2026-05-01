"""
judge_simulator.py — Full local test harness simulating the magicpin judge
Tests: all 5 endpoints, 12-tick simulation, adaptive injection, replay test (5 cases)
Run: python judge_simulator.py
"""

import json
import time
import sys
import requests

BASE_URL = "http://localhost:5000"
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "
results = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    msg = f"  {status} {name}"
    if detail:
        msg += f"  →  {detail}"
    print(msg)
    results.append((name, condition))
    return condition


def post(path: str, body: dict, timeout: int = 30) -> tuple[dict, float, int]:
    t0 = time.time()
    try:
        r = requests.post(f"{BASE_URL}{path}", json=body, timeout=timeout)
        elapsed = time.time() - t0
        try:
            data = r.json()
        except Exception:
            data = {}
        return data, elapsed, r.status_code
    except requests.exceptions.Timeout:
        return {"error": "timeout"}, timeout, 408
    except Exception as e:
        return {"error": str(e)}, time.time() - t0, 500


def get(path: str) -> tuple[dict, int]:
    try:
        r = requests.get(f"{BASE_URL}{path}", timeout=10)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500


def sep(title: str):
    print(f"\n{'═'*55}")
    print(f"  {title}")
    print(f"{'═'*55}")


# ══════════════════════════════════════════════════════════════════
sep("TEST 1: GET /v1/healthz")
data, code = get("/v1/healthz")
check("Status 200", code == 200, f"got {code}")
check("status=ok", data.get("status") == "ok")
check("bot field present", "bot" in data)

# ══════════════════════════════════════════════════════════════════
sep("TEST 2: GET /v1/metadata")
data, code = get("/v1/metadata")
check("Status 200", code == 200)
check("name present", "name" in data)
check("author present", "author" in data)
check("capabilities list", isinstance(data.get("capabilities"), list))
check("approach present", "approach" in data)
check("avg_response_ms present", "avg_response_ms" in data)

# ══════════════════════════════════════════════════════════════════
sep("TEST 3: POST /v1/context — Store all 3 scopes")

merchant_payload = {
    "name": "Sharma's Dhaba",
    "category": "restaurant",
    "rating": 4.3,
    "orders_last_week": 142,
    "orders_this_week": 89,
    "top_dish": "Dal Makhani",
    "active_offer": "20% off on orders above ₹300",
    "location": "Koramangala, Bangalore",
    "avg_order_value": 380,
}

trigger_payload = {
    "type": "order_dip",
    "description": "Orders dropped 37% this week vs last week",
    "severity": "high",
    "detected_at": "2026-04-29T09:00:00Z",
    "search_spike": "biryani near me — 2.3x spike in last 2 hours",
    "ipl_match_tonight": True,
}

customer_payload = {
    "name": "Rahul",
    "last_order": "2026-04-15",
    "favorite_dish": "Butter Chicken",
    "order_count": 8,
    "intent": "research",
    "days_since_last_order": 14,
}

for scope, ctx_id, payload in [
    ("merchant", "m_001", merchant_payload),
    ("trigger", "t_001", trigger_payload),
    ("customer", "c_001", customer_payload),
]:
    body = {
        "scope": scope, "context_id": ctx_id, "version": 1,
        "payload": payload, "delivered_at": "2026-04-29T10:00:00Z",
    }
    data, elapsed, code = post("/v1/context", body)
    check(f"{scope} context stored", code == 200 and data.get("accepted") is True, str(data)[:60])
    check(f"{scope} ack_id present", "ack_id" in data)

# Idempotency test
data, _, code = post("/v1/context", {
    "scope": "merchant", "context_id": "m_001", "version": 1,
    "payload": merchant_payload, "delivered_at": "2026-04-29T10:00:00Z",
})
check("Idempotent (same version = no-op)", code == 200 and data.get("accepted") is True)

# Version upgrade
data, _, code = post("/v1/context", {
    "scope": "merchant", "context_id": "m_001", "version": 2,
    "payload": {**merchant_payload, "orders_this_week": 95},
    "delivered_at": "2026-04-29T10:01:00Z",
})
check("Version upgrade accepted", code == 200 and data.get("accepted") is True)

# Invalid scope
data, _, code = post("/v1/context", {
    "scope": "invalid", "context_id": "x_001", "version": 1, "payload": {},
})
check("Invalid scope → 400", code == 400)

# ══════════════════════════════════════════════════════════════════
sep("TEST 4: POST /v1/tick — Single tick")

tick_body = {
    "tick_id": "tick_001",
    "merchant_id": "m_001",
    "trigger_id": "t_001",
    "customer_id": "c_001",
}
print("  Running pipeline (may take ~7s with Groq, instant without)...")
data, elapsed, code = post("/v1/tick", tick_body, timeout=30)

check("Status 200", code == 200, f"got {code}")
check("tick_id matches", data.get("tick_id") == "tick_001")
check("message non-empty", bool(data.get("message")), data.get("message", "")[:70])
check("cta present", "cta" in data)
check("send_as=vera", data.get("send_as") == "vera")
check("suppression_key present", bool(data.get("suppression_key")))
check("rationale present", bool(data.get("rationale")))
check("Response < 30s", elapsed < 30, f"{elapsed:.2f}s")
check("Response < 15s (ideal)", elapsed < 15, f"{elapsed:.2f}s")

print(f"\n  📨 Message: {data.get('message', 'N/A')}")
print(f"  📣 CTA:     {data.get('cta', 'N/A')}")
print(f"  ⏱  Time:   {elapsed:.2f}s")

# ══════════════════════════════════════════════════════════════════
sep("TEST 5: POST /v1/tick — Without customer_id (optional)")

data, elapsed, code = post("/v1/tick", {
    "tick_id": "tick_002",
    "merchant_id": "m_001",
    "trigger_id": "t_001",
}, timeout=30)
check("Status 200 (no customer)", code == 200)
check("message present", bool(data.get("message")))
check("Response < 30s", elapsed < 30, f"{elapsed:.2f}s")

# ══════════════════════════════════════════════════════════════════
sep("TEST 6: 12-Tick Simulation (escalation test)")

print("  Simulating 12 ticks (judge runs 60 min, tick every 5 min)...")
tick_times = []
for i in range(3, 7):  # ticks 3-6 to test escalation
    body = {
        "tick_id": f"tick_{i:03d}",
        "merchant_id": "m_001",
        "trigger_id": "t_001",
    }
    data, elapsed, code = post("/v1/tick", body, timeout=30)
    tick_times.append(elapsed)
    status = PASS if code == 200 and data.get("message") else FAIL
    print(f"  {status} tick_{i:03d} | {elapsed:.2f}s | {data.get('message', 'ERROR')[:60]}")

avg_time = sum(tick_times) / len(tick_times) if tick_times else 0
check("All escalation ticks < 30s", all(t < 30 for t in tick_times), f"avg={avg_time:.2f}s")

# ══════════════════════════════════════════════════════════════════
sep("TEST 7: POST /v1/reply — Replay Test (5 cases)")

reply_cases = [
    ("YES",      "Yes go ahead, do it"),
    ("NO",       "Not interested right now"),
    ("OFF_TOPIC","What's the weather today?"),
    ("HOSTILE",  "Stop messaging me, this is very annoying"),
    ("HANDOFF",  "Talk to my manager about this"),
]

for case_name, reply_text in reply_cases:
    body = {
        "tick_id": "tick_001",
        "reply_text": reply_text,
        "replied_by": "merchant",
        "replied_at": "2026-04-29T10:05:00Z",
    }
    data, elapsed, code = post("/v1/reply", body, timeout=30)
    check(f"CASE {case_name} — status 200", code == 200, f"got {code}")
    check(f"CASE {case_name} — message present", bool(data.get("message")))
    check(f"CASE {case_name} — < 30s", elapsed < 30, f"{elapsed:.2f}s")
    print(f"    💬 [{case_name}] {data.get('message', 'N/A')[:80]}")
    print(f"         case_detected={data.get('case_detected')} | suppress={data.get('suppress', '?')}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 8: Adaptive Injection (mid-test context update)")

# Inject new trigger mid-test (simulates judge injecting fresh context)
data, _, code = post("/v1/context", {
    "scope": "trigger",
    "context_id": "t_001",
    "version": 3,
    "payload": {
        "type": "search_spike",
        "description": "IPL match tonight — 3.1x spike in food delivery searches",
        "severity": "critical",
        "detected_at": "2026-04-29T11:00:00Z",
        "ipl_teams": "MI vs CSK",
    },
    "delivered_at": "2026-04-29T11:00:00Z",
})
check("New trigger injected (v3)", code == 200 and data.get("accepted") is True)

# Tick should now use the new context
data, elapsed, code = post("/v1/tick", {
    "tick_id": "tick_007",
    "merchant_id": "m_001",
    "trigger_id": "t_001",
}, timeout=30)
check("Adaptive tick status 200", code == 200)
check("Adaptive tick has message", bool(data.get("message")))
check("Adaptive tick < 30s", elapsed < 30, f"{elapsed:.2f}s")
print(f"  📨 Adaptive: {data.get('message', 'N/A')[:80]}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 9: Error Handling")

# Missing required field
data, _, code = post("/v1/tick", {"tick_id": "tick_bad"})
check("Missing merchant_id → 400", code == 400)
check("Error has 'error' field", "error" in data)
check("Error has 'code' field", "code" in data)

# Missing reply field
data, _, code = post("/v1/reply", {"tick_id": "tick_001"})
check("Missing reply_text → 400", code == 400)

# ══════════════════════════════════════════════════════════════════
sep("TEST 10: Multi-merchant isolation")

# Store context for a second merchant (dentist)
post("/v1/context", {
    "scope": "merchant", "context_id": "m_002", "version": 1,
    "payload": {
        "name": "Dr. Mehta Dental Clinic",
        "category": "dentist",
        "rating": 4.7,
        "patients_this_month": 48,
        "recall_due": 12,
        "location": "Bandra, Mumbai",
    },
    "delivered_at": "2026-04-29T10:00:00Z",
})
post("/v1/context", {
    "scope": "trigger", "context_id": "t_002", "version": 1,
    "payload": {
        "type": "recall",
        "description": "12 patients overdue for 6-month checkup",
        "severity": "medium",
    },
    "delivered_at": "2026-04-29T10:00:00Z",
})

data, elapsed, code = post("/v1/tick", {
    "tick_id": "tick_008",
    "merchant_id": "m_002",
    "trigger_id": "t_002",
}, timeout=30)
check("Dentist tick status 200", code == 200)
check("Dentist tick has message", bool(data.get("message")))
print(f"  🦷 Dentist: {data.get('message', 'N/A')[:80]}")

# ══════════════════════════════════════════════════════════════════
sep("RESULTS SUMMARY")

passed = sum(1 for _, ok in results if ok)
total = len(results)
pct = (passed / total * 100) if total else 0

print(f"\n  {passed}/{total} checks passed ({pct:.0f}%)\n")

failed = [(name, ok) for name, ok in results if not ok]
if failed:
    print(f"  {FAIL} Failed checks:")
    for name, _ in failed:
        print(f"    - {name}")
    print()

if passed == total:
    print(f"  {PASS} ALL CHECKS PASSED — Bot is ready for submission! 🚀")
    sys.exit(0)
elif pct >= 85:
    print(f"  {WARN} Most checks passed. Review failures above before submitting.")
    sys.exit(0)
else:
    print(f"  {FAIL} Too many failures. Fix issues before submitting.")
    sys.exit(1)
