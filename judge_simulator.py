"""
judge_simulator.py — Local test harness matching the REAL judge schema
Tests all 5 endpoints with the correct request/response shapes from challenge-testing-brief.md
Run: python judge_simulator.py
"""

import json
import time
import sys
import io
import requests

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_URL = "http://localhost:5000"
# Uncomment to test Railway:
# BASE_URL = "https://veraai-bot.up.railway.app"

PASS = "[PASS]"
FAIL = "[FAIL]"
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
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════
sep("TEST 1: GET /v1/healthz")
data, code = get("/v1/healthz")
check("Status 200", code == 200, f"got {code}")
check("status=ok", data.get("status") == "ok")
check("uptime_seconds present", "uptime_seconds" in data, str(data.get("uptime_seconds")))
check("contexts_loaded present", "contexts_loaded" in data)
if "contexts_loaded" in data:
    cl = data["contexts_loaded"]
    check("categories loaded", cl.get("category", 0) >= 5, f"got {cl.get('category',0)}")
    check("merchants loaded", cl.get("merchant", 0) >= 10, f"got {cl.get('merchant',0)}")
print(f"  contexts_loaded: {data.get('contexts_loaded')}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 2: GET /v1/metadata")
data, code = get("/v1/metadata")
check("Status 200", code == 200)
check("team_name present", bool(data.get("team_name")), data.get("team_name",""))
check("model present", bool(data.get("model")))
check("approach present", bool(data.get("approach")))
check("version present", bool(data.get("version")))

# ══════════════════════════════════════════════════════════════════
sep("TEST 3: POST /v1/context — All scopes + idempotency")

# Store category (use version 99 to avoid conflict with preloaded v1)
cat_body = {
    "scope": "category", "context_id": "dentists", "version": 99,
    "payload": {"slug": "dentists", "voice": {"tone": "peer_clinical"}, "offer_catalog": [], "peer_stats": {}, "digest": [], "patient_content_library": [], "seasonal_beats": [], "trend_signals": []},
    "delivered_at": "2026-04-26T09:45:00Z",
}
data, _, code = post("/v1/context", cat_body)
check("Category context stored", code == 200 and data.get("accepted") is True)
check("ack_id present", "ack_id" in data)
check("stored_at present", "stored_at" in data)

# Store merchant
merchant_body = {
    "scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 99,
    "payload": {
        "merchant_id": "m_001_drmeera_dentist_delhi", "category_slug": "dentists",
        "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi", "locality": "Lajpat Nagar", "owner_first_name": "Meera", "languages": ["en", "hi"], "verified": True},
        "performance": {"views": 2410, "calls": 18, "ctr": 0.021},
        "offers": [{"id": "o_001", "title": "Dental Cleaning @ Rs299", "status": "active"}],
        "customer_aggregate": {"total_unique_ytd": 540, "lapsed_180d_plus": 78, "high_risk_adult_count": 124},
        "signals": ["stale_posts:22d", "ctr_below_peer_median"],
    },
    "delivered_at": "2026-04-26T09:45:30Z",
}
data, _, code = post("/v1/context", merchant_body)
check("Merchant context stored", code == 200 and data.get("accepted") is True)

# Store trigger
trigger_body = {
    "scope": "trigger", "context_id": "trg_test_001", "version": 1,
    "payload": {
        "id": "trg_test_001", "scope": "merchant", "kind": "research_digest", "source": "external",
        "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None,
        "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
        "urgency": 2, "suppression_key": "research:dentists:2026-W17-test",
        "expires_at": "2026-05-03T00:00:00Z",
    },
    "delivered_at": "2026-04-26T10:32:00Z",
}
data, _, code = post("/v1/context", trigger_body)
check("Trigger context stored", code == 200 and data.get("accepted") is True)

# Idempotency — same version should return 409
data, _, code = post("/v1/context", trigger_body)
check("Idempotent (same version → 409)", code == 409 and data.get("accepted") is False, f"code={code} accepted={data.get('accepted')}")
check("stale_version reason", data.get("reason") == "stale_version", data.get("reason",""))

# Version upgrade
trigger_v2 = {**trigger_body, "version": 2}
data, _, code = post("/v1/context", trigger_v2)
check("Version upgrade accepted (200)", code == 200 and data.get("accepted") is True)

# Invalid scope
data, _, code = post("/v1/context", {"scope": "invalid", "context_id": "x", "version": 1, "payload": {}})
check("Invalid scope → 400", code == 400)

# ══════════════════════════════════════════════════════════════════
sep("TEST 4: POST /v1/tick — Real judge schema")

tick_body = {
    "now": "2026-04-26T10:35:00Z",
    "available_triggers": ["trg_001_research_digest_dentists"],
}
print("  Running pipeline (may take ~7s with Groq)...")
data, elapsed, code = post("/v1/tick", tick_body, timeout=30)

check("Status 200", code == 200, f"got {code}")
check("actions array present", "actions" in data, str(type(data.get("actions"))))
check("actions is list", isinstance(data.get("actions"), list))

actions = data.get("actions", [])
if actions:
    a = actions[0]
    check("action has conversation_id", bool(a.get("conversation_id")), a.get("conversation_id",""))
    check("action has merchant_id", bool(a.get("merchant_id")), a.get("merchant_id",""))
    check("action has body (non-empty)", bool(a.get("body")), a.get("body","")[:60])
    check("action has cta", "cta" in a, a.get("cta",""))
    check("action has send_as", "send_as" in a, a.get("send_as",""))
    check("action has trigger_id", "trigger_id" in a)
    check("action has suppression_key", bool(a.get("suppression_key")), a.get("suppression_key",""))
    check("action has rationale", bool(a.get("rationale")))
    check("action has template_name", "template_name" in a)
    check(f"Response < 30s", elapsed < 30, f"{elapsed:.2f}s")
    check(f"Response < 15s (ideal)", elapsed < 15, f"{elapsed:.2f}s")
    print(f"\n  📨 Body:    {a.get('body','N/A')}")
    print(f"  📣 CTA:     {a.get('cta','N/A')}")
    print(f"  👤 send_as: {a.get('send_as','N/A')}")
    print(f"  🔑 conv_id: {a.get('conversation_id','N/A')}")
    print(f"  ⏱  Time:   {elapsed:.2f}s")
else:
    check("actions not empty", False, "bot returned empty actions[]")
    print("  (bot chose not to send — may be suppressed or no context)")

# ══════════════════════════════════════════════════════════════════
sep("TEST 5: POST /v1/tick — Multiple triggers")

# Store a customer trigger
post("/v1/context", {
    "scope": "customer", "context_id": "c_001_priya_for_m001", "version": 1,
    "payload": {
        "customer_id": "c_001_priya_for_m001", "merchant_id": "m_001_drmeera_dentist_delhi",
        "identity": {"name": "Priya", "language_pref": "hi-en mix", "age_band": "25-35"},
        "relationship": {"last_visit": "2026-05-12", "visits_total": 4},
        "state": "lapsed_soft",
        "preferences": {"preferred_slots": "weekday_evening"},
        "consent": {"opted_in_at": "2025-11-04", "scope": ["recall_reminders"]},
    },
    "delivered_at": "2026-04-26T10:00:00Z",
})
post("/v1/context", {
    "scope": "trigger", "context_id": "trg_003_recall_due_priya", "version": 1,
    "payload": {
        "id": "trg_003_recall_due_priya", "scope": "customer", "kind": "recall_due",
        "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": "c_001_priya_for_m001",
        "payload": {"service_due": "6_month_cleaning", "available_slots": [{"label": "Wed 5 Nov, 6pm"}, {"label": "Thu 6 Nov, 5pm"}]},
        "urgency": 3, "suppression_key": "recall:c_001_priya_for_m001:6mo",
        "expires_at": "2026-11-30T00:00:00Z",
    },
    "delivered_at": "2026-04-26T10:00:00Z",
})

data, elapsed, code = post("/v1/tick", {
    "now": "2026-04-26T11:00:00Z",
    "available_triggers": ["trg_003_recall_due_priya"],
}, timeout=30)
check("Customer trigger status 200", code == 200)
actions = data.get("actions", [])
if actions:
    a = actions[0]
    check("Customer trigger has body", bool(a.get("body")), a.get("body","")[:60])
    check("Customer trigger has customer_id", bool(a.get("customer_id")), a.get("customer_id",""))
    check("send_as = merchant_on_behalf", a.get("send_as") == "merchant_on_behalf", a.get("send_as",""))
    print(f"  📨 Customer msg: {a.get('body','N/A')[:80]}")
else:
    check("Customer trigger has body", False, "empty actions")

# ══════════════════════════════════════════════════════════════════
sep("TEST 6: POST /v1/reply — Real judge schema (5 cases)")

# Get a conversation_id from a previous tick
conv_id = "conv_m_001_drmeera_dentist_delhi_research_digest"

reply_cases = [
    ("YES",      "Yes please send the abstract. Also draft the patient WhatsApp."),
    ("NO",       "Not interested right now"),
    ("OFF_TOPIC","Can you also help me file my GST this month?"),
    ("HOSTILE",  "Stop messaging me. This is useless spam."),
    ("HANDOFF",  "Talk to my manager about this"),
]

for case_name, msg_text in reply_cases:
    body = {
        "conversation_id": conv_id,
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "from_role": "merchant",
        "message": msg_text,
        "received_at": "2026-04-26T10:42:00Z",
        "turn_number": 2,
    }
    data, elapsed, code = post("/v1/reply", body, timeout=30)
    check(f"CASE {case_name} — status 200", code == 200, f"got {code}")
    check(f"CASE {case_name} — action field", data.get("action") in ("send","wait","end"), data.get("action",""))
    check(f"CASE {case_name} — < 30s", elapsed < 30, f"{elapsed:.2f}s")
    print(f"    [{case_name}] action={data.get('action')} | body={data.get('body','')[:60]}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 7: Auto-reply detection")

auto_reply_msg = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
conv_auto = "conv_auto_test_001"

# Turn 2 — first auto-reply
data, _, code = post("/v1/reply", {
    "conversation_id": conv_auto, "merchant_id": "m_001_drmeera_dentist_delhi",
    "from_role": "merchant", "message": auto_reply_msg,
    "received_at": "2026-04-26T10:42:00Z", "turn_number": 2,
})
check("Auto-reply turn 2 — status 200", code == 200)
check("Auto-reply turn 2 — action=send (flag for owner)", data.get("action") == "send", data.get("action",""))
print(f"  Turn 2: action={data.get('action')} | {data.get('body','')[:60]}")

# Turn 3 — second auto-reply
data, _, code = post("/v1/reply", {
    "conversation_id": conv_auto, "merchant_id": "m_001_drmeera_dentist_delhi",
    "from_role": "merchant", "message": auto_reply_msg,
    "received_at": "2026-04-26T10:47:00Z", "turn_number": 3,
})
check("Auto-reply turn 3 — action=wait", data.get("action") == "wait", data.get("action",""))
check("Auto-reply turn 3 — wait_seconds > 0", data.get("wait_seconds", 0) > 0, str(data.get("wait_seconds",0)))
print(f"  Turn 3: action={data.get('action')} | wait={data.get('wait_seconds',0)}s")

# Turn 4 — third auto-reply
data, _, code = post("/v1/reply", {
    "conversation_id": conv_auto, "merchant_id": "m_001_drmeera_dentist_delhi",
    "from_role": "merchant", "message": auto_reply_msg,
    "received_at": "2026-04-26T10:52:00Z", "turn_number": 4,
})
check("Auto-reply turn 4 — action=end", data.get("action") == "end", data.get("action",""))
print(f"  Turn 4: action={data.get('action')}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 8: Intent transition (YES → immediate action)")

data, _, code = post("/v1/reply", {
    "conversation_id": conv_id, "merchant_id": "m_001_drmeera_dentist_delhi",
    "from_role": "merchant", "message": "Ok lets do it. Whats next?",
    "received_at": "2026-04-26T10:45:00Z", "turn_number": 3,
})
check("Intent transition — status 200", code == 200)
check("Intent transition — action=send", data.get("action") == "send", data.get("action",""))
body_text = data.get("body", "").lower()
qualifying_words = ["would you", "do you", "can you tell", "what if", "how about", "just to plan"]
is_qualifying = any(w in body_text for w in qualifying_words)
check("Intent transition — NOT qualifying (switched to action)", not is_qualifying, data.get("body","")[:80])
print(f"  Response: {data.get('body','')[:100]}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 9: Adaptive injection (mid-test context update)")

# Inject new category version with additional digest item
data, _, code = post("/v1/context", {
    "scope": "category", "context_id": "dentists", "version": 2,
    "payload": {
        "slug": "dentists", "voice": {"tone": "peer_clinical"},
        "digest": [
            {"id": "d_2026W17_jida_fluoride", "kind": "research", "title": "3-month fluoride recall cuts caries 38%", "source": "JIDA Oct 2026, p.14"},
            {"id": "d_2026W17_dci_radiograph", "kind": "compliance", "title": "DCI revised radiograph dose limits effective 2026-12-15", "source": "DCI circular 2026-11-04"},
        ],
        "offer_catalog": [], "peer_stats": {}, "patient_content_library": [], "seasonal_beats": [], "trend_signals": [],
    },
    "delivered_at": "2026-04-26T10:50:00Z",
})
check("Adaptive injection v2 accepted", code == 200 and data.get("accepted") is True)

data, elapsed, code = post("/v1/tick", {
    "now": "2026-04-26T10:55:00Z",
    "available_triggers": ["trg_001_research_digest_dentists"],
}, timeout=30)
check("Adaptive tick status 200", code == 200)
actions = data.get("actions", [])
check("Adaptive tick has actions", len(actions) > 0)
if actions:
    print(f"  📨 Adaptive: {actions[0].get('body','N/A')[:80]}")

# ══════════════════════════════════════════════════════════════════
sep("TEST 10: Healthz after warmup — contexts_loaded counts")

data, code = get("/v1/healthz")
check("Healthz still 200", code == 200)
cl = data.get("contexts_loaded", {})
check("categories >= 5", cl.get("category", 0) >= 5, f"got {cl.get('category',0)}")
check("merchants >= 10", cl.get("merchant", 0) >= 10, f"got {cl.get('merchant',0)}")
check("triggers >= 25", cl.get("trigger", 0) >= 25, f"got {cl.get('trigger',0)}")
print(f"  contexts_loaded: {cl}")

# ══════════════════════════════════════════════════════════════════
sep("RESULTS SUMMARY")

passed = sum(1 for _, ok in results if ok)
total = len(results)
pct = (passed / total * 100) if total else 0
print(f"\n  {passed}/{total} checks passed ({pct:.0f}%)\n")

failed = [(name, ok) for name, ok in results if not ok]
if failed:
    print(f"  Failed checks:")
    for name, _ in failed:
        print(f"    ❌ {name}")
    print()

if pct >= 90:
    print(f"  [EXCELLENT] Bot is ready for submission!")
    sys.exit(0)
elif pct >= 75:
    print(f"  [GOOD] Minor issues to fix before submitting.")
    sys.exit(0)
else:
    print(f"  [FAIL] Needs fixes before submitting.")
    sys.exit(1)
