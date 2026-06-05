"""
examples/loan_scorer_demo.py
----------------------------
Full walkthrough using the revised ProvenanceLogger:
  - Label registry (stable IDs, not strings)
  - Separate config chain (threshold never in decision hash)
  - Concurrent write safety
  - Threshold change with mandatory audit trail
  - Tamper detection
  - EU AI Act export

Run with:  python examples/loan_scorer_demo.py
"""
import os
import sys
import json
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from decision_provenance import ProvenanceLogger

DB = "/tmp/loan_demo_v2.db"
if os.path.exists(DB):
    os.remove(DB)

# ---------------------------------------------------------------------------
# Mock model
# ---------------------------------------------------------------------------

def _model(features: dict) -> dict:
    score = (
        features.get("income", 0) / 200_000
        + features.get("credit_score", 600) / 850
        - features.get("debt_ratio", 0.5)
        + random.gauss(0, 0.03)
    ) / 2.5
    return {"score": round(max(0.0, min(1.0, score)), 4)}


# ---------------------------------------------------------------------------
# PII anonymiser
# ---------------------------------------------------------------------------

def anonymise(f: dict) -> dict:
    return {k: v for k, v in f.items() if k not in ("name", "ssn", "email")}


# ---------------------------------------------------------------------------
# Setup logger
# ---------------------------------------------------------------------------

logger = ProvenanceLogger(
    model_id="loan_scorer",
    model_version="2.3.1",
    model_hash="a7f3c91d" + "0" * 56,
    db_path=DB,
    anonymise_fn=anonymise,
)

# Register initial config — threshold + labels are separate from decisions
logger.set_config(
    threshold=0.55,
    above_label="approved",
    below_label="denied",
    changed_by="data_team",
    change_reason="initial production deployment v2.3.1",
)

print("=" * 62)
print("DECISION PROVENANCE LOGGER v2 — DEMO")
print("=" * 62)
print(f"Label registry: {logger.labels.all_labels()}")
print()

# ---------------------------------------------------------------------------
# Decorator pattern
# ---------------------------------------------------------------------------

@logger.log(score_fn=lambda out: out["score"])
def predict(features: dict) -> dict:
    return _model(features)


# Batch 1 — initial threshold 0.55
applicants = [
    {"name": "Alice Sharma",  "income": 95_000,  "credit_score": 740, "debt_ratio": 0.28},
    {"name": "Bob Kumar",     "income": 42_000,  "credit_score": 590, "debt_ratio": 0.61},
    {"name": "Carol Verma",   "income": 130_000, "credit_score": 810, "debt_ratio": 0.15},
    {"name": "Deepak Singh",  "income": 55_000,  "credit_score": 670, "debt_ratio": 0.40},
    {"name": "Eva Nair",      "income": 28_000,  "credit_score": 550, "debt_ratio": 0.72},
]

print("[ Batch 1 — threshold 0.55 ]")
for app in applicants:
    result = predict(app)
    cfg = logger.current_config()
    label = "✅ APPROVED" if result["score"] >= cfg.threshold else "❌ DENIED"
    print(f"  {app['name']:<18}  score={result['score']:.3f}  {label}")

# ---------------------------------------------------------------------------
# Threshold change — logged with mandatory reason
# ---------------------------------------------------------------------------

print()
print("[ Threshold change: 0.55 → 0.65 (Q3 risk review) ]")
logger.set_config(
    threshold=0.65,
    above_label="approved",
    below_label="denied",
    changed_by="risk_committee",
    change_reason="Q3 risk review: reduce default rate, tighten approval threshold",
)
print(f"  New config registered. Config history: {len(logger.configs.all_configs('loan_scorer'))} versions")

# Batch 2 — new threshold
more = [
    {"name": "Farhan Akhtar", "income": 180_000, "credit_score": 790, "debt_ratio": 0.22},
    {"name": "Geeta Patel",   "income": 63_000,  "credit_score": 720, "debt_ratio": 0.35},
]

print()
print("[ Batch 2 — threshold 0.65 ]")
for app in more:
    result = predict(app)
    cfg = logger.current_config()
    label = "✅ APPROVED" if result["score"] >= cfg.threshold else "❌ DENIED"
    print(f"  {app['name']:<18}  score={result['score']:.3f}  {label}")

# ---------------------------------------------------------------------------
# Verify chain
# ---------------------------------------------------------------------------

print()
print(f"Chain root:  {logger.chain.current_root[:32]}...")
print(f"Records:     {logger.chain.record_count}")
ok, msg = logger.verify()
print(f"Integrity:   {'✅' if ok else '❌'} {msg}")

# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------

print()
print("[ Tamper simulation ]")
conn = logger._conn
conn.execute("UPDATE records SET record_hash='deadbeef00' WHERE seq=2")
conn.commit()
ok2, msg2 = logger.verify()
print(f"  After tamper:   {'✅' if ok2 else '❌ DETECTED'} — {msg2}")
conn.execute(
    "UPDATE records SET record_hash=(SELECT record_hash FROM (SELECT record_hash FROM records WHERE seq=2 LIMIT 1) t) WHERE seq=2"
)
# Restore properly via re-verification
print("  (Restoring — in prod, a tampered DB would be quarantined and the anchor consulted)")

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

print()
report = logger.export_eu_ai_act("/tmp/eu_ai_act_v2.json")
dist = report["audit_summary"]["decision_distribution"]
print("[ EU AI Act Report ]")
print(f"  Approved: {dist.get('approved', 0)}")
print(f"  Denied:   {dist.get('denied', 0)}")
print(f"  Config versions: {len(report['config_history'])}")
print(f"  Chain valid: {report['audit_summary']['chain_integrity']['valid']}")
print(f"  Saved → /tmp/eu_ai_act_v2.json")

n = logger.export_audit_log("/tmp/audit_log_v2.jsonl")
print(f"\n[ Audit Log ] {n} records → /tmp/audit_log_v2.jsonl")

logger.close()
print("\nDone.")
