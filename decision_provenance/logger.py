"""
logger.py — ProvenanceLogger v1.1

New in v1.1:
  - init_chain()    — explicit genesis record (required before first record)
  - migrate_chain() — schema upgrade with audit trail
  - record_async()  — async version of record() for FastAPI/async agents
  - log_async()     — async decorator
  - search()        — query records with filters
  - count()         — count matching records
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .record import build_record, ProvenanceRecord, ValidationError, RECORD_SCHEMA_VERSION
from .chain import MerkleChain
from .config_record import ConfigChain, ConfigRecord
from .label_registry import LabelRegistry
from .genesis import GenesisChain, GenesisRecord, CURRENT_SCHEMA


def _hash_model_tag(model_id: str, model_version: str) -> str:
    return hashlib.sha256(f"{model_id}:{model_version}".encode()).hexdigest()


class ProvenanceLogger:
    """
    Tamper-evident audit logger for ML inference pipelines.

    Usage:
        logger = ProvenanceLogger(model_id="loan_scorer", model_version="2.3.1")

        # Required before first record — explicit operator intent
        logger.init_chain(changed_by="data_team", reason="production deployment")

        logger.set_config(threshold=0.6, above_label="approved",
                          below_label="denied", changed_by="ops",
                          change_reason="initial config")

        @logger.log(score_fn=lambda out: out["score"])
        def predict(features): ...

        # Async variant
        @logger.log_async(score_fn=lambda out: out["score"])
        async def predict_async(features): ...
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

        self._conn = sqlite3.connect(str(Path(db_path)), check_same_thread=False)
        self.labels  = LabelRegistry(self._conn)
        self.configs = ConfigChain(self._conn)
        self.chain   = MerkleChain(self._conn)
        self.genesis  = GenesisChain(self._conn)
        self._genesis_cache: Optional[GenesisRecord] = None  # cached after init_chain

        self._anchor_receipts: list[dict] = []

    # ------------------------------------------------------------------
    # Genesis chain management
    # ------------------------------------------------------------------

    def init_chain(self, *, changed_by: str, reason: str) -> GenesisRecord:
        """
        Explicitly initialise the provenance chain with a genesis record.
        Must be called before logging any decisions.
        """
        rec = self.genesis.init(
            model_id=self.model_id,
            created_by=changed_by,
            reason=reason,
            schema_version=CURRENT_SCHEMA,
        )
        self._genesis_cache = rec
        return rec

    def migrate_chain(self, *, changed_by: str, reason: str) -> GenesisRecord:
        """Start a new genesis segment for a schema upgrade."""
        rec = self.genesis.migrate(
            model_id=self.model_id,
            changed_by=changed_by,
            reason=reason,
            target_schema=CURRENT_SCHEMA,
        )
        self._genesis_cache = rec
        return rec

    def _require_genesis(self) -> GenesisRecord:
        """Return cached genesis or query DB once, then cache."""
        if self._genesis_cache is not None:
            return self._genesis_cache
        g = self.genesis.current(self.model_id)
        if g is None:
            raise RuntimeError(
                f"No genesis record found for model '{self.model_id}'. "
                f"Call logger.init_chain(changed_by=..., reason=...) before "
                f"logging any decisions."
            )
        self._genesis_cache = g
        return g

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
        above_id = self.labels.register(above_label)
        self.labels.register(below_label)
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
    # Core record method (sync)
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
        Log one decision synchronously.
        Requires init_chain() to have been called first.
        """
        g = self._require_genesis()
        cfg = config or self.current_config()
        if cfg is None:
            raise RuntimeError(
                "No config registered. Call set_config() before logging decisions."
            )

        if score >= cfg.threshold:
            label_id = cfg.threshold_label_id
        else:
            all_labels = self.labels.all_labels()
            below_ids = [lid for lid in all_labels if lid != cfg.threshold_label_id]
            if not below_ids:
                raise RuntimeError("No below-threshold label registered. Call set_config().")
            label_id = below_ids[0]

        label_display = self.labels.get_display(label_id) or label_id
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
            genesis_id=g.genesis_id,
            schema_version=g.schema_version,
            input_schema_version=self.input_schema_version,
            session_id=session_id,
        )

        new_root = self.chain.append(rec)

        ipfs_receipt = self._maybe_anchor_ipfs(rec, new_root)
        evm_receipt = self._maybe_anchor_evm(new_root)

        return {
            "record_id":     rec.record_id,
            "session_id":    rec.session_id,
            "timestamp_iso": rec.timestamp_iso,
            "label_id":      label_id,
            "label_display": label_display,
            "score":         score,
            "threshold":     cfg.threshold,
            "config_id":     cfg.config_id,
            "genesis_id":    g.genesis_id,
            "schema_version": g.schema_version,
            "chain_root":    new_root,
            "record_count":  self.chain.record_count,
            "ipfs_receipt":  ipfs_receipt,
            "evm_receipt":   evm_receipt,
        }

    # ------------------------------------------------------------------
    # Async record method
    # ------------------------------------------------------------------

    async def record_async(
        self,
        *,
        input_features: dict,
        output: dict,
        score: float,
        session_id: Optional[str] = None,
        config: Optional[ConfigRecord] = None,
    ) -> dict:
        """
        Async version of record(). Runs the sync DB operation in a thread pool
        so it doesn't block the event loop.

        Use this in FastAPI endpoints, async agent pipelines, or anywhere
        you're already in an async context.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.record(
                input_features=input_features,
                output=output,
                score=score,
                session_id=session_id,
                config=config,
            )
        )

    # ------------------------------------------------------------------
    # Decorator API (sync)
    # ------------------------------------------------------------------

    def log(
        self,
        *,
        score_fn: Callable[[dict], float],
        session_id_fn: Optional[Callable[[dict], str]] = None,
    ):
        """Decorator that wraps a sync predict(features: dict) -> dict function."""
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
    # Decorator API (async)
    # ------------------------------------------------------------------

    def log_async(
        self,
        *,
        score_fn: Callable[[dict], float],
        session_id_fn: Optional[Callable[[dict], str]] = None,
    ):
        """
        Decorator that wraps an async predict function.

        Usage:
            @logger.log_async(score_fn=lambda out: out["score"])
            async def predict(features: dict) -> dict:
                return await my_async_model(features)
        """
        def decorator(fn):
            @functools.wraps(fn)
            async def wrapper(input_features: dict, **kwargs) -> dict:
                output = await fn(input_features, **kwargs)
                await self.record_async(
                    input_features=input_features,
                    output=output,
                    score=score_fn(output),
                    session_id=session_id_fn(input_features) if session_id_fn else None,
                )
                return output
            return wrapper
        return decorator

    # ------------------------------------------------------------------
    # Search and count
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        label_id: Optional[str] = None,
        label_display: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        genesis_id: Optional[str] = None,
        schema_version: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Search decision records with optional filters.

        Args:
            label_id:       filter by stable label ID (e.g. "L001")
            label_display:  filter by display string (e.g. "approved")
            date_from:      ISO timestamp lower bound (inclusive)
            date_to:        ISO timestamp upper bound (inclusive)
            genesis_id:     filter by genesis segment
            schema_version: filter by record schema version
            limit:          max records to return (default 100)
            offset:         pagination offset

        Returns list of full record dicts.
        """
        return self.chain.search(
            model_id=self.model_id,
            label_id=label_id,
            label_display=label_display,
            date_from=date_from,
            date_to=date_to,
            genesis_id=genesis_id,
            schema_version=schema_version,
            limit=limit,
            offset=offset,
        )

    def count(
        self,
        *,
        label_id: Optional[str] = None,
        label_display: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> int:
        """Count matching records without fetching them."""
        return self.chain.count(
            model_id=self.model_id,
            label_id=label_id,
            label_display=label_display,
            date_from=date_from,
            date_to=date_to,
        )

    # ------------------------------------------------------------------
    # Verification and export
    # ------------------------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        return self.chain.verify()

    def export_audit_log(self, output_path: str | Path = "audit_log.jsonl") -> int:
        return self.chain.export_jsonl(output_path)

    def export_eu_ai_act(self, output_path: str | Path = "eu_ai_act_report.json") -> dict:
        import json, time as _time
        ok, msg = self.verify()
        rows = self._conn.execute(
            """SELECT label_id, label_display, config_id, timestamp_iso,
                      record_id, chain_root, genesis_id, schema_version
               FROM records ORDER BY seq ASC"""
        ).fetchall()

        label_dist: dict[str, int] = {}
        for row in rows:
            display = row[1]
            label_dist[display] = label_dist.get(display, 0) + 1

        report = {
            "report_schema":   "eu_ai_act_art13_v2",
            "generated_at":    _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "system": {
                "model_id":      self.model_id,
                "model_version": self.model_version,
                "model_hash":    self.model_hash,
            },
            "genesis_history": [g.to_dict() for g in self.genesis.all_for_model(self.model_id)],
            "label_registry":  self.labels.all_labels(),
            "config_history":  self.configs.all_configs(self.model_id),
            "audit_summary": {
                "total_decisions":       len(rows),
                "decision_distribution": label_dist,
                "chain_integrity":       {"valid": ok, "message": msg},
                "chain_root":            self.chain.current_root,
            },
            "records": [
                {
                    "record_id":          r[4],
                    "timestamp_iso":      r[3],
                    "label_id":           r[0],
                    "label_display":      r[1],
                    "config_id":          r[2],
                    "genesis_id":         r[6],
                    "schema_version":     r[7],
                    "chain_root_at_time": r[5],
                }
                for r in rows
            ],
        }

        Path(output_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    def get_record(self, record_id: str) -> Optional[dict]:
        return self.chain.get_record(record_id)

    # ------------------------------------------------------------------
    # Anchoring helpers
    # ------------------------------------------------------------------

    def _maybe_anchor_ipfs(self, rec: ProvenanceRecord, new_root: str) -> Optional[dict]:
        if not self.ipfs_anchor:
            return None
        from .anchor import anchor_record_ipfs
        try:
            receipt = anchor_record_ipfs(
                record_id=rec.record_id,
                model_id=self.model_id,
                record_hash=rec.record_hash,
                chain_root=new_root,
                pinata_jwt=self.pinata_jwt,
                ipfs_url=self.ipfs_url,
            )
            self._anchor_receipts.append(receipt)
            return receipt
        except Exception as e:
            return {"error": str(e)}

    def _maybe_anchor_evm(self, new_root: str) -> Optional[dict]:
        if not (self.evm_anchor_every > 0 and
                self.chain.record_count % self.evm_anchor_every == 0):
            return None
        from .anchor import anchor_root_evm
        try:
            receipt = anchor_root_evm(
                chain_root=new_root,
                record_count=self.chain.record_count,
                model_id=self.model_id,
                **self.evm_config,
            )
            self._anchor_receipts.append(receipt)
            return receipt
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
