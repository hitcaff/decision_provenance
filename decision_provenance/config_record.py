"""
config_record.py — versioned threshold / model config records.

Threshold changes are logged as a separate chain, never mixed into decision hashes.
Auditors join decision records + config records by timestamp to reconstruct
which threshold was active for any given decision.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Optional


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


@dataclass
class ConfigRecord:
    config_id: str
    model_id: str
    config_version: str          # e.g. "cfg-3" or semver
    threshold: float
    threshold_label_id: str      # which label is produced above threshold
    effective_from: str          # ISO timestamp
    changed_by: str              # identifier of who/what changed this
    change_reason: str           # mandatory justification
    prev_config_id: str          # chains config records together
    config_hash: str             # SHA-256 of all above fields

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def _hash_config(c: ConfigRecord) -> str:
    payload = {
        "config_id":          c.config_id,
        "model_id":           c.model_id,
        "config_version":     c.config_version,
        "threshold":          c.threshold,
        "threshold_label_id": c.threshold_label_id,
        "effective_from":     c.effective_from,
        "changed_by":         c.changed_by,
        "change_reason":      c.change_reason,
        "prev_config_id":     c.prev_config_id,
    }
    return _sha256(_canonical(payload))


class ConfigChain:
    """
    Separate append-only chain for model configuration records.
    Every threshold change is logged here with a mandatory reason.
    Decision records reference the active config_id — never embed the threshold value.
    """

    GENESIS = "CONFIG_GENESIS"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._init_db()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS config_records (
                seq              INTEGER PRIMARY KEY AUTOINCREMENT,
                config_id        TEXT NOT NULL UNIQUE,
                model_id         TEXT NOT NULL,
                config_version   TEXT NOT NULL,
                threshold        REAL NOT NULL,
                threshold_label_id TEXT NOT NULL,
                effective_from   TEXT NOT NULL,
                changed_by       TEXT NOT NULL,
                change_reason    TEXT NOT NULL,
                prev_config_id   TEXT NOT NULL,
                config_hash      TEXT NOT NULL,
                full_json        TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def register(
        self,
        *,
        model_id: str,
        config_version: str,
        threshold: float,
        threshold_label_id: str,
        changed_by: str,
        change_reason: str,
    ) -> ConfigRecord:
        """Register a new config version. Returns the ConfigRecord."""
        # Validate inputs
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"Threshold must be 0.0–1.0, got {threshold}")
        if not change_reason.strip():
            raise ValueError("change_reason is mandatory — document why the config changed")
        if not changed_by.strip():
            raise ValueError("changed_by is mandatory — identify who/what made this change")

        prev = self.current_config(model_id)
        prev_id = prev.config_id if prev else self.GENESIS
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"

        rec = ConfigRecord(
            config_id=str(uuid.uuid4()),
            model_id=model_id,
            config_version=config_version,
            threshold=threshold,
            threshold_label_id=threshold_label_id,
            effective_from=ts,
            changed_by=changed_by,
            change_reason=change_reason,
            prev_config_id=prev_id,
            config_hash="",
        )
        rec.config_hash = _hash_config(rec)

        self._conn.execute(
            """INSERT INTO config_records
               (config_id, model_id, config_version, threshold, threshold_label_id,
                effective_from, changed_by, change_reason, prev_config_id, config_hash, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.config_id, rec.model_id, rec.config_version, rec.threshold,
                rec.threshold_label_id, rec.effective_from, rec.changed_by,
                rec.change_reason, rec.prev_config_id, rec.config_hash, rec.to_json(),
            )
        )
        self._conn.commit()
        return rec

    def current_config(self, model_id: str) -> Optional[ConfigRecord]:
        """Return the most recently registered config for this model."""
        row = self._conn.execute(
            """SELECT full_json FROM config_records
               WHERE model_id=? ORDER BY seq DESC LIMIT 1""",
            (model_id,)
        ).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        return ConfigRecord(**d)

    def config_at(self, model_id: str, timestamp_iso: str) -> Optional[ConfigRecord]:
        """Return the config that was active at a given timestamp."""
        row = self._conn.execute(
            """SELECT full_json FROM config_records
               WHERE model_id=? AND effective_from <= ?
               ORDER BY seq DESC LIMIT 1""",
            (model_id, timestamp_iso)
        ).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        return ConfigRecord(**d)

    def all_configs(self, model_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT full_json FROM config_records WHERE model_id=? ORDER BY seq ASC",
            (model_id,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]
