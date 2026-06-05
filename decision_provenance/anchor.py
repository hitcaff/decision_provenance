"""
anchor.py — per-record IPFS anchoring + periodic EVM anchoring.

Design:
  - Every decision record is anchored to IPFS immediately (free, ~200ms).
    This closes the local-mutation attack window per-record.
  - The Merkle chain root is anchored to EVM periodically (configurable cadence).
    This provides a public, unforgeable timestamp without paying gas per record.

IPFS anchor payload per record:
  {record_id, model_id, record_hash, chain_root, anchored_at}

EVM anchor payload (periodic):
  {chain_root, record_count, model_id} → emitted as on-chain event
"""
from __future__ import annotations

import json
import time
from typing import Optional


# ---------------------------------------------------------------------------
# IPFS — per-record
# ---------------------------------------------------------------------------

def anchor_record_ipfs(
    *,
    record_id: str,
    model_id: str,
    record_hash: str,
    chain_root: str,
    pinata_jwt: Optional[str] = None,
    ipfs_url: str = "http://localhost:5001",
) -> dict:
    """
    Pin a single record anchor to IPFS immediately after it's written.
    Returns dict with 'cid', 'record_id', 'anchored_at'.
    """
    try:
        import requests
    except ImportError:
        raise ImportError("pip install requests  to use IPFS anchoring")

    payload = {
        "schema":      "decision_provenance_record_anchor_v1",
        "record_id":   record_id,
        "model_id":    model_id,
        "record_hash": record_hash,
        "chain_root":  chain_root,
        "anchored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if pinata_jwt:
        import requests as req
        resp = req.post(
            "https://api.pinata.cloud/pinning/pinJSONToIPFS",
            headers={
                "Authorization": f"Bearer {pinata_jwt}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"pinataContent": payload}),
            timeout=30,
        )
        resp.raise_for_status()
        cid = resp.json()["IpfsHash"]
    else:
        import requests as req
        resp = req.post(
            f"{ipfs_url}/api/v0/add",
            files={"file": json.dumps(payload).encode()},
            timeout=30,
        )
        resp.raise_for_status()
        cid = resp.json()["Hash"]

    return {
        "backend":     "ipfs",
        "cid":         cid,
        "record_id":   record_id,
        "chain_root":  chain_root,
        "anchored_at": payload["anchored_at"],
    }


# ---------------------------------------------------------------------------
# EVM — periodic chain root anchor
# ---------------------------------------------------------------------------

REGISTRY_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "root",        "type": "bytes32"},
            {"internalType": "string",  "name": "modelId",     "type": "string"},
            {"internalType": "uint256", "name": "recordCount", "type": "uint256"},
        ],
        "name": "anchor",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "sender",      "type": "address"},
            {"indexed": False, "internalType": "bytes32", "name": "root",        "type": "bytes32"},
            {"indexed": False, "internalType": "string",  "name": "modelId",     "type": "string"},
            {"indexed": False, "internalType": "uint256", "name": "recordCount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "timestamp",   "type": "uint256"},
        ],
        "name": "Anchored",
        "type": "event",
    },
]


def anchor_root_evm(
    *,
    chain_root: str,
    record_count: int,
    model_id: str,
    private_key: str,
    contract_address: str,
    rpc_url: str,
) -> dict:
    """
    Write the current Merkle chain root to a deployed ProvenanceRegistry contract.
    Call this periodically (e.g. every 100 records or hourly) — not per-record.

    Args:
        chain_root:       Current Merkle root (hex string)
        record_count:     Number of records at this point
        model_id:         Model identifier
        private_key:      Signing key — load from env, never hardcode
        contract_address: Deployed ProvenanceRegistry address
        rpc_url:          EVM RPC — use POKT Grove City for decentralised access
                          e.g. https://eth-mainnet.rpc.grove.city/v1/<app_id>
    """
    try:
        from web3 import Web3
    except ImportError:
        raise ImportError("pip install web3  to use EVM anchoring")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")

    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=REGISTRY_ABI,
    )

    root_bytes = bytes.fromhex(chain_root)
    tx = contract.functions.anchor(root_bytes, model_id, record_count).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      80_000,
        "gasPrice": w3.eth.gas_price,
    })

    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    return {
        "backend":      "evm",
        "tx_hash":      tx_hash.hex(),
        "block_number": receipt.blockNumber,
        "contract":     contract_address,
        "chain_root":   chain_root,
        "record_count": record_count,
        "anchored_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
