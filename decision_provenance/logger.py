"""
logger.py — ProvenanceLogger: the single entry point for all provenance operations.

Orchestrates:
  LabelRegistry   → stable label IDs, never hash the display string
  ConfigChain     → versioned threshold records, separate from decisions
  MerkleChain     → tamper-evident decision chain
  per-record IPFS → optional, closes local-mutation window immediately

Quick start:
    logger = ProvenanceLogger(
        model_id="loan_scorer",
        model_version="2.3.1",
    )
    logger.set_config(
        threshold=0.6,
        above_label="approved",
        below_label="denied",
        changed_by="data_team",
        change_reason="initial deployment",
    )

    @logger.log(score_fn=lambda out: out["score"])
    def predict(features: dict) -> dict:
        ...
"""
from __future__ import annotations

import functools
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .record import build_record, ProvenanceRecord, ValidationError
from .chain import MerkleChain
from .config_record import ConfigChain, ConfigRecord
from .label_registry import LabelRegistry


def _hash_model_tag(model_id: str, model_version: str) -> str:
    return hashlib.sha256(f"{model_id}:{model_version}".encode()).hexdigest()


class ProvenanceLogger:
    """
    Wrap any ML inference function to log tamper-evident provenance records.

    Args:
        model_id:             Human name of the model
        model_version:        Semver or git SHA
        model_hash:           SHA-256 of serialised weights. If None, derived
                              from model_id + model_version.
        db_path:              Path to the SQLite database
        input_schema_version: Version of your feature schema
        anonymise_fn:         Optional PII stripper — runs before any hashing
        ipfs_anchor:          If True, every record is pinned to IPFS immediately.
                              Requires 'requests'. Pass pinata_jwt or ipfs_url below.
        pinata_jwt:           Pinata JWT for IPFS pinning (optional)
        ipfs_url:             Local IPFS node URL (default: http://localhost:5001)
        evm_anchor_every:     Anchor chain root to EVM every N records (0 = disabled)
        evm_config:           Dict with keys: private_key, contract_address, rpc_url
    """

    def __init__(
        self,
        model_id: str,
        model_version: str,
        model_hash: Optional[str] = None,
        db_path: str | Path = "provenance.db",
        input_schema_version: str = "1.0",
        anonymise_fn: Optional[Callable[[dict], dict]] = None,
        ipfs_anchor: bool = False,
        pinata_jwt: Optional[str] = None,
        ipfs_url: str = "http://localhost:5001",
        evm_anchor_every: int = 0,
        evm_config: Optional[dict] = None,
    ):
        if not model_id.strip():
            raise ValidationError("model_id must not be empty")
        if not model_version.strip():
            raise ValidationError("model_version must not be empty")

        self.model_id = model_id
        self.model_version = model_version
        self.model_hash = model_hash or _hash_model_tag(model_id, model_version)
        self.input_schema_version = input_schema_version
        self.anonymise_fn = anonymise_fn
        self.ipfs_anchor = ipfs_anchor
        self.pinata_jwt = pinata_jwt
        self.ipfs_url = ipfs_url
        self.evm_anchor_every = evm_anchor_every
        self.evm_config = evm_config or {}

        # Single shared connection — WAL mode handles concurrent reads
        self._conn = sqlite3.connect(str(Path(db_path)), check_same_thread=False)
        self.labels  = LabelRegistry(self._conn)
        self.configs = ConfigChain(self._conn)
        self.chain   = MerkleChain(self._conn)

        self._anchor_receipts: list[dict] = []

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def set_config(
        self,
        *,
        threshold: float,
        above_label: str,
        below_label: str,
        config_version: Optional[str] = None,
        changed_by: str,
        change_reason: str,
    ) -> ConfigRecord:
        """
        Register a new threshold configuration. Must be called before any
        inference if no prior config exists.

        Args:
            threshold:      Decision boundary 0.0–1.0
            above_label:    Label assigned when score >= threshold
            below_label:    Label assigned when score < threshold
            config_version: Optional version tag (auto-incremented if None)
            changed_by:     Who/what is making this change
            change_reason:  Why the config is changing (mandatory)
        """
        above_id = self.labels.register(above_label)
        self.labels.register(below_label)   # ensure both are registered

        existing = self.configs.all_configs(self.model_id)
        cv = config_version or f"cfg-{len(existing) + 1}"

        return self.configs.register(
            model_id=self.model_id,
            config_version=cv,
            threshold=threshold,
            threshold_label_id=above_id,
            changed_by=changed_by,
            change_reason=change_reason,
        )

    def current_config(self) -> Optional[ConfigRecord]:
        return self.configs.current_config(self.model_id)

    # ------------------------------------------------------------------
    # Core record method
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        input_features: dict,
        output: dict,
        score: float,
        session_id: Optional[str] = None,
        config: Optional[ConfigRecord] = None,
    ) -> dict:
        """
        Log one decision. Returns a summary dict with record_id and chain_root.

        Args:
            input_features: Raw feature dict (PII stripped by anonymise_fn if set)
            output:         Raw model output dict
            score:          Scalar score used to determine the label
            session_id:     Optional caller-supplied request ID
            config:         Override active config (uses current_config() if None)
        """
        cfg = config or self.current_config()
        if cfg is None:
            raise RuntimeError(
                "No config registered. Call set_config() before logging decisions."
            )

        # Determine label from score
        if score >= cfg.threshold:
            label_id = cfg.threshold_label_id
        else:
            # Find the below-threshold label: first label that isn't the above label
            all_labels = self.labels.all_labels()
            below_ids = [lid for lid, _ in all_labels.items() if lid != cfg.threshold_label_id]
            if not below_ids:
                raise RuntimeError("No below-threshold label registered. Call set_config().")
            label_id = below_ids[0]

        label_display = self.labels.get_display(label_id) or label_id

        # Anonymise before hashing
        safe_input = self.anonymise_fn(input_features) if self.anonymise_fn else input_features

        rec = build_record(
            model_id=self.model_id,
            model_version=self.model_version,
            model_hash=self.model_hash,
            input_features=safe_input,
            output=output,
            label_id=label_id,
            label_display=label_display,
            config_id=cfg.config_id,
            input_schema_version=self.input_schema_version,
            session_id=session_id,
        )

        new_root = self.chain.append(rec)

        # Per-record IPFS anchor
        ipfs_receipt = None
        if self.ipfs_anchor:
            from .anchor import anchor_record_ipfs
            try:
                ipfs_receipt = anchor_record_ipfs(
                    record_id=rec.record_id,
                    model_id=self.model_id,
                    record_hash=rec.record_hash,
                    chain_root=new_root,
                    pinata_jwt=self.pinata_jwt,
                    ipfs_url=self.ipfs_url,
                )
                self._anchor_receipts.append(ipfs_receipt)
            except Exception as e:
                # Anchor failure is non-fatal — local chain is still intact
                ipfs_receipt = {"error": str(e)}

        # Periodic EVM anchor
        evm_receipt = None
        if self.evm_anchor_every > 0 and self.chain.record_count % self.evm_anchor_every == 0:
            from .anchor import anchor_root_evm
            try:
                evm_receipt = anchor_root_evm(
                    chain_root=new_root,
                    record_count=self.chain.record_count,
                    model_id=self.model_id,
                    **self.evm_config,
                )
                self._anchor_receipts.append(evm_receipt)
            except Exception as e:
                evm_receipt = {"error": str(e)}

        return {
            "record_id":    rec.record_id,
            "session_id":   rec.session_id,
            "timestamp_iso": rec.timestamp_iso,
            "label_id":     label_id,
            "label_display": label_display,
            "score":        score,
            "threshold":    cfg.threshold,
            "config_id":    cfg.config_id,
            "chain_root":   new_root,
            "record_count": self.chain.record_count,
            "ipfs_receipt": ipfs_receipt,
            "evm_receipt":  evm_receipt,
        }

    # ------------------------------------------------------------------
    # Decorator API
    # ------------------------------------------------------------------

    def log(
        self,
        *,
        score_fn: Callable[[dict], float],
        session_id_fn: Optional[Callable[[dict], str]] = None,
    ):
        """
        Decorator that wraps a predict(features: dict) -> dict function.

        Args:
            score_fn:       Maps raw output dict → scalar float score
            session_id_fn:  Maps input features → session ID (optional)
        """
        def decorator(fn: Callable[[dict], dict]):
            @functools.wraps(fn)
            def wrapper(input_features: dict, **kwargs) -> dict:
                output = fn(input_features, **kwargs)
                self.record(
                    input_features=input_features,
                    output=output,
                    score=score_fn(output),
                    session_id=session_id_fn(input_features) if session_id_fn else None,
                )
                return output
            return wrapper
        return decorator

    # ------------------------------------------------------------------
    # Verification + export
    # ------------------------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        return self.chain.verify()

    def export_audit_log(self, output_path: str | Path = "audit_log.jsonl") -> int:
        return self.chain.export_jsonl(output_path)

    def export_eu_ai_act(self, output_path: str | Path = "eu_ai_act_report.json") -> dict:
        """
        Structured compliance report for EU AI Act Article 13.
        Decision distribution uses label display strings (human-readable)
        but the audit trail references stable label IDs throughout.
        """
        import json, time
        ok, msg = self.verify()

        rows = self._conn.execute(
            """SELECT label_id, label_display, config_id, timestamp_iso, record_id, chain_root
               FROM records ORDER BY seq ASC"""
        ).fetchall()

        label_dist: dict[str, int] = {}
        for row in rows:
            display = row[1]
            label_dist[display] = label_dist.get(display, 0) + 1

        report = {
            "report_schema":   "eu_ai_act_art13_v2",
            "generated_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "system": {
                "model_id":      self.model_id,
                "model_version": self.model_version,
                "model_hash":    self.model_hash,
            },
            "label_registry":  self.labels.all_labels(),
            "config_history":  self.configs.all_configs(self.model_id),
            "audit_summary": {
                "total_decisions":    len(rows),
                "decision_distribution": label_dist,
                "chain_integrity":    {"valid": ok, "message": msg},
                "chain_root":         self.chain.current_root,
            },
            "records": [
                {
                    "record_id":   r[4],
                    "timestamp_iso": r[3],
                    "label_id":    r[0],
                    "label_display": r[1],
                    "config_id":   r[2],
                    "chain_root_at_time": r[5],
                }
                for r in rows
            ],
        }

        Path(output_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    def get_record(self, record_id: str) -> Optional[dict]:
        return self.chain.get_record(record_id)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
