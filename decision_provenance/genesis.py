"""
genesis.py — genesis record: the cryptographic root of trust for a provenance chain.

Every ProvenanceLogger chain begins with a GenesisRecord written by an explicit
call to logger.init_chain(changed_by=..., reason=...).

The genesis record commits to:
  - schema_version: the hash payload schema used by this chain
  - model_id: the model this chain belongs to
  - who started it and why (operator intent, not library side-effect)

Every subsequent decision record references genesis_id and includes
schema_version in its hash. This closes the attack surface where a
record could be retroactively relabelled with a different schema.

Chain segments:
  When the schema changes (library upgrade), a new genesis record is written
  with migrated_from pointing to the previous genesis. The verifier walks
  genesis segments to know which schema to apply to each decision record.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional


CURRENT_SCHEMA = "1.1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


@dataclass
class GenesisRecord:
    genesis_id: str
    schema_version: str
    model_id: str
    created_at: str
    created_by: str
    reason: str
    migrated_from: str        # empty string if first genesis for this model
    genesis_hash: str         # SHA-256 of all above fields

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def _compute_genesis_hash(g: GenesisRecord) -> str:
    payload = {
        "genesis_id":     g.genesis_id,
        "schema_version": g.schema_version,
        "model_id":       g.model_id,
        "created_at":     g.created_at,
        "created_by":     g.created_by,
        "reason":         g.reason,
        "migrated_from":  g.migrated_from,
    }
    return _sha256(_canonical(payload))


class GenesisChain:
    """
    Manages genesis records for a provenance chain.

    One genesis record per chain initialisation. When the schema changes,
    a new genesis is written with migrated_from pointing to the previous one.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._init_db()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS genesis_records (
                seq           INTEGER PRIMARY KEY AUTOINCREMENT,
                genesis_id    TEXT NOT NULL UNIQUE,
                schema_version TEXT NOT NULL,
                model_id      TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                created_by    TEXT NOT NULL,
                reason        TEXT NOT NULL,
                migrated_from TEXT NOT NULL DEFAULT '',
                genesis_hash  TEXT NOT NULL,
                full_json     TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def init(
        self,
        *,
        model_id: str,
        created_by: str,
        reason: str,
        schema_version: str = CURRENT_SCHEMA,
    ) -> GenesisRecord:
        """
        Write a new genesis record. Raises if one already exists for this model
        unless migrating — use migrate() for upgrades.
        """
        if not created_by.strip():
            raise ValueError("created_by is mandatory — identify who is starting this chain")
        if not reason.strip():
            raise ValueError("reason is mandatory — document why this chain is being started")

        existing = self.current(model_id)
        if existing:
            raise RuntimeError(
                f"Genesis record already exists for model '{model_id}' "
                f"(genesis_id={existing.genesis_id}). "
                f"Use logger.migrate_chain() to start a new schema segment."
            )

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = GenesisRecord(
            genesis_id=str(uuid.uuid4()),
            schema_version=schema_version,
            model_id=model_id,
            created_at=ts,
            created_by=created_by,
            reason=reason,
            migrated_from="",
            genesis_hash="",
        )
        rec.genesis_hash = _compute_genesis_hash(rec)
        self._write(rec)
        return rec

    def migrate(
        self,
        *,
        model_id: str,
        changed_by: str,
        reason: str,
        target_schema: str = CURRENT_SCHEMA,
    ) -> GenesisRecord:
        """
        Write a new genesis record for a schema upgrade.
        migrated_from points to the previous genesis_id.
        """
        if not changed_by.strip():
            raise ValueError("changed_by is mandatory")
        if not reason.strip():
            raise ValueError("reason is mandatory")

        prev = self.current(model_id)
        prev_id = prev.genesis_id if prev else ""

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = GenesisRecord(
            genesis_id=str(uuid.uuid4()),
            schema_version=target_schema,
            model_id=model_id,
            created_at=ts,
            created_by=changed_by,
            reason=reason,
            migrated_from=prev_id,
            genesis_hash="",
        )
        rec.genesis_hash = _compute_genesis_hash(rec)
        self._write(rec)
        return rec

    def current(self, model_id: str) -> Optional[GenesisRecord]:
        """Return the most recent genesis record for this model."""
        row = self._conn.execute(
            "SELECT full_json FROM genesis_records WHERE model_id=? ORDER BY seq DESC LIMIT 1",
            (model_id,)
        ).fetchone()
        if not row:
            return None
        return GenesisRecord(**json.loads(row[0]))

    def all_for_model(self, model_id: str) -> list[GenesisRecord]:
        rows = self._conn.execute(
            "SELECT full_json FROM genesis_records WHERE model_id=? ORDER BY seq ASC",
            (model_id,)
        ).fetchall()
        return [GenesisRecord(**json.loads(r[0])) for r in rows]

    def schema_at(self, model_id: str, timestamp_iso: str) -> str:
        """Return the schema version that was active at a given timestamp."""
        row = self._conn.execute(
            """SELECT schema_version FROM genesis_records
               WHERE model_id=? AND created_at <= ?
               ORDER BY seq DESC LIMIT 1""",
            (model_id, timestamp_iso)
        ).fetchone()
        return row[0] if row else CURRENT_SCHEMA

    def _write(self, rec: GenesisRecord):
        self._conn.execute(
            """INSERT INTO genesis_records
               (genesis_id, schema_version, model_id, created_at, created_by,
                reason, migrated_from, genesis_hash, full_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rec.genesis_id, rec.schema_version, rec.model_id, rec.created_at,
             rec.created_by, rec.reason, rec.migrated_from, rec.genesis_hash,
             rec.to_json())
        )
        self._conn.commit()
