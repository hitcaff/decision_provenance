"""
test_live_anchor.py — runs a live end-to-end anchor test on Polygon Amoy.

Creates a few provenance records, verifies the chain,
then anchors the Merkle root to the deployed ProvenanceRegistry contract.

Run with:
    python3 test_live_anchor.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))

from decision_provenance import ProvenanceLogger
from decision_provenance.anchor import anchor_root_evm

DB = "/tmp/live_anchor_test.db"

# ---------------------------------------------------------------------------
# Setup logger
# ---------------------------------------------------------------------------
logger = ProvenanceLogger(
    model_id="loan_scorer",
    model_version="2.3.1",
    db_path=DB,
)

logger.set_config(
    threshold=0.6,
    above_label="approved",
    below_label="denied",
    changed_by="hitesh",
    change_reason="live anchor test on Polygon Amoy",
)

# ---------------------------------------------------------------------------
# Log a few decisions
# ---------------------------------------------------------------------------
print("Logging decisions...")
decisions = [
    ({"income": 95000,  "credit_score": 740}, 0.82),
    ({"income": 42000,  "credit_score": 590}, 0.31),
    ({"income": 130000, "credit_score": 810}, 0.91),
    ({"income": 28000,  "credit_score": 550}, 0.18),
    ({"income": 180000, "credit_score": 790}, 0.88),
]

for features, score in decisions:
    result = logger.record(
        input_features=features,
        output={"score": score},
        score=score,
    )
    print(f"  {result['label_display']:<10} score={score:.2f}  record_id={result['record_id'][:8]}...")

# ---------------------------------------------------------------------------
# Verify chain
# ---------------------------------------------------------------------------
print()
ok, msg = logger.verify()
print(f"Chain integrity: {'✅' if ok else '❌'} {msg}")

# ---------------------------------------------------------------------------
# Anchor to Polygon Amoy
# ---------------------------------------------------------------------------
print()
print("Anchoring chain root to Polygon Amoy...")

private_key      = os.environ.get("PROVENANCE_SIGNER_KEY")
rpc_url          = os.environ.get("POKT_RPC_URL")
contract_address = os.environ.get("CONTRACT_ADDRESS")

if not all([private_key, rpc_url, contract_address]):
    print("❌ Missing environment variables. Make sure PROVENANCE_SIGNER_KEY, POKT_RPC_URL, and CONTRACT_ADDRESS are set.")
    sys.exit(1)

receipt = anchor_root_evm(
    chain_root=logger.chain.current_root,
    record_count=logger.chain.record_count,
    model_id="loan_scorer",
    private_key=private_key,
    contract_address=contract_address,
    rpc_url=rpc_url,
)

print(f"✅ Anchor transaction confirmed!")
print(f"   TX hash:      {receipt['tx_hash']}")
print(f"   Block:        {receipt['block_number']}")
print(f"   Chain root:   {receipt['chain_root'][:32]}...")
print(f"   Records:      {receipt['record_count']}")
print()
print(f"Verify on-chain:")
print(f"   https://amoy.polygonscan.com/tx/{receipt['tx_hash']}")

# Save receipt
with open("anchor_receipt.json", "w") as f:
    json.dump(receipt, f, indent=2)
print(f"\nReceipt saved to anchor_receipt.json")

logger.close()
