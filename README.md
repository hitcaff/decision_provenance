# decision-provenance

**When your ML model denies a loan, rejects a resume, or fires a fraud flag — can you prove what it saw, what it decided, and that nobody changed the record afterward?**

Most teams can't. The model ran, a decision was made, a row was written to a database. That row can be edited, deleted, or quietly overwritten. There's no proof it hasn't been.

`decision-provenance` fixes this with one decorator. Every automated decision gets a SHA-256 hash of its inputs and outputs, chained into a rolling Merkle tree. Mutate any past record and every subsequent root breaks — detectable offline, no blockchain required. Optional on-chain anchoring makes the proof public.

```python
pip install decision-provenance
```

[![Tests](https://github.com/hitcaff/decision_provenance/actions/workflows/ci.yml/badge.svg)](https://github.com/hitcaff/decision_provenance/actions)
[![PyPI](https://img.shields.io/pypi/v/decision-provenance)](https://pypi.org/project/decision-provenance/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## The problem in one sentence

You cannot currently prove that a past automated decision wasn't altered — and regulators are starting to ask.

The EU AI Act Article 13 requires high-risk AI systems to log decisions with sufficient granularity to identify the cause of results. GDPR Article 22 requires explainability for automated decisions. India's DPDP Act is following the same direction. None of these requirements are met by writing to a database row.

---

## What it does

Wraps any Python model inference function and:

- Hashes the exact inputs, outputs, model version, and decision label into a tamper-evident record
- Chains every record so any mutation to any past record breaks every subsequent hash
- Versions your threshold changes separately — so a recalibration is a documented event, not a silent overwrite
- Exports a structured EU AI Act Article 13 compliance report on demand
- Optionally anchors the chain root to IPFS or any EVM chain for public verifiability

---

## Quickstart — two minutes

```python
from decision_provenance import ProvenanceLogger

logger = ProvenanceLogger(
    model_id="loan_scorer",
    model_version="2.3.1",
    db_path="provenance.db",
)

logger.set_config(
    threshold=0.6,
    above_label="approved",
    below_label="denied",
    changed_by="data_team",
    change_reason="initial production deployment",
)

@logger.log(score_fn=lambda out: out["score"])
def predict(features: dict) -> dict:
    return my_model(features)  # your existing function, unchanged

# Every call now logs a tamper-evident record automatically
result = predict({"income": 95_000, "credit_score": 740, "debt_ratio": 0.28})
```

Verify the full chain anytime:

```python
ok, message = logger.verify()
# True  → "Chain intact — 1247 records, root=a3f8..."
# False → "Root mismatch at seq=43: ..."
```

---

## Live demo

Paste any record ID into the verification portal to see the full cryptographic proof:

**[hitcaff.github.io/decision_provenance](https://hitcaff.github.io/decision_provenance)**

---

## Use cases

Any model making a consequential automated decision:

- **Loan / credit scoring** — prove what features your model saw before denying an application
- **Fraud detection** — tamper-evident log of every flag with the exact input state
- **Resume screening** — audit trail for every hire/reject with model version and threshold at the time
- **Content moderation** — every ban or removal decision with its confidence score and policy version
- **Healthcare triage** — immutable record of every risk score and routing decision
- **Algorithmic trading** — cryptographic proof of every signal with entry, stop loss, target, and confidence
- **AI agent pipelines** — audit every tool call and decision node in a multi-step agent

---

## How it works

Three independent chains share one SQLite database:

```
LabelRegistry  → stable label IDs (L001, L002...)
                 "approved" can be renamed; L001 never changes in the hash

ConfigChain    → versioned threshold records
                 threshold 0.55 → 0.65 is a new ConfigRecord, not a mutation
                 every change requires a mandatory change_reason

MerkleChain    → decision records
                 new_root = SHA-256(prev_root ∥ record_hash)
                 prev_root assigned inside write lock — concurrency safe
                 any mutation breaks every subsequent root
```

**What is in the decision hash:**
`model_id + model_version + model_hash + input_hash + output_hash + label_id + config_id + timestamp`

**What is deliberately NOT in the hash:**
- `label_display` — can be renamed without affecting the decision
- `threshold` — lives in ConfigChain, referenced by `config_id`
- `runtime_env` — informational only

---

## Threshold changes

Every threshold change is a new ConfigRecord — not a mutation. It requires a reason:

```python
logger.set_config(
    threshold=0.65,
    above_label="approved",
    below_label="denied",
    changed_by="risk_committee",
    change_reason="Q3 risk review: reduce default rate",
)
```

The EU AI Act export includes the full config history, so an auditor can reconstruct which threshold was active for any decision.

---

## Export

```python
# Full JSONL audit log
logger.export_audit_log("audit_log.jsonl")

# EU AI Act Article 13 compliance report
report = logger.export_eu_ai_act("compliance_report.json")
# Includes: label_registry, config_history,
#           decision_distribution, chain_integrity
```

---

## On-chain anchoring (optional)

Local SQLite is already tamper-evident. External anchoring adds public verifiability — the chain root exists outside your infrastructure and cannot be altered retroactively.

```python
# Periodic EVM anchor every 100 records
logger = ProvenanceLogger(
    ...,
    evm_anchor_every=100,
    evm_config={
        "private_key":      os.environ["SIGNER_KEY"],
        "contract_address": "0xe516e5b3dbb50e4f25811be7de4f101d01f01de0",
        "rpc_url":          os.environ["POKT_RPC_URL"],
    },
)
```

Deploy contracts/ProvenanceRegistry.sol once per organisation. ~35,000 gas per anchor call on Polygon.

Verified contract on Polygonscan:
https://amoy.polygonscan.com/address/0xe516e5b3dbb50e4f25811be7de4f101d01f01de0#code

---

## Run as a microservice

```bash
docker run -d -p 8000:8000 hitcaff9/decision-provenance:latest
```

```
POST /configure       initialise or reconfigure the logger
POST /record          log one decision
GET  /verify          verify chain integrity
GET  /record/{id}     fetch single record
GET  /export/audit    download JSONL audit log
GET  /export/eu_ai_act download compliance report
GET  /health          liveness check
```

---

## Threat model

| Threat | Protection |
|--------|------------|
| DB record mutation | Merkle chain — any change breaks all subsequent roots |
| Label string rename | Label registry — hash uses stable ID, not display string |
| Silent threshold change | ConfigChain — every change is a new versioned record with mandatory reason |
| Concurrent write corruption | Write lock + SQLite WAL mode |
| Determined attacker with DB access | External anchor (IPFS/EVM) — root already exists outside the DB |
| Compromised model lying to logger | Out of scope — requires HSM + model signing at training time |

---

## Tests

```bash
python -m pytest tests/ -v
```

38 tests covering hash determinism, label registry, config chain, Merkle chain integrity,
tamper detection, input validation, concurrency (20 simultaneous threads), EU AI Act export,
and threshold change audit trail. All passing on Python 3.10, 3.11, 3.12.

---

## Install options

```bash
# Core library (zero dependencies)
pip install decision-provenance

# With IPFS anchoring
pip install "decision-provenance[ipfs]"

# With EVM anchoring
pip install "decision-provenance[evm]"

# With FastAPI microservice
pip install "decision-provenance[api]"

# Everything
pip install "decision-provenance[all]"
```

---

## Disclaimer

decision-provenance provides cryptographic tamper-evidence infrastructure that
supports EU AI Act Article 13 documentation requirements. It is an open source tool —
compliance certification requires your own legal review.

---

## Links

- PyPI: https://pypi.org/project/decision-provenance/
- Docker: https://hub.docker.com/r/hitcaff9/decision-provenance
- Verification portal: https://hitcaff.github.io/decision_provenance
- Contract (verified): https://amoy.polygonscan.com/address/0xe516e5b3dbb50e4f25811be7de4f101d01f01de0#code
- Live integration: https://github.com/hitcaff/indian-market-agent
