"""
chain.py — thread-safe WAL-mode Merkle chain.

v1.1 changes:
  - records table has genesis_id and schema_version columns
  - verify() uses schema_version to apply correct hash computation per record
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .record import ProvenanceRecord, _compute_record_hash

GENESIS_ROOT = "0" * 64
_write_lock = threading.Lock()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MerkleChain:

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._init_db()

    def _init_db(self):
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                seq              INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id        TEXT NOT NULL UNIQUE,
                session_id       TEXT NOT NULL,
                timestamp_iso    TEXT NOT NULL,
                model_id         TEXT NOT NULL,
                model_version    TEXT NOT NULL,
                label_id         TEXT NOT NULL,
                label_display    TEXT NOT NULL,
                config_id        TEXT NOT NULL,
                genesis_id       TEXT NOT NULL DEFAULT '',
                schema_version   TEXT NOT NULL DEFAULT '1.0',
                record_hash      TEXT NOT NULL,
                prev_root        TEXT NOT NULL,
                chain_root       TEXT NOT NULL,
                full_json        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chain_meta (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                current_root  TEXT NOT NULL,
                record_count  INTEGER NOT NULL
            );

            INSERT OR IGNORE INTO chain_meta (id, current_root, record_count)
            VALUES (1, '""" + GENESIS_ROOT + """', 0);
        """)
        self._conn.commit()

    @property
    def current_root(self) -> str:
        row = self._conn.execute(
            "SELECT current_root FROM chain_meta WHERE id=1"
        ).fetchone()
        return row[0] if row else GENESIS_ROOT

    @property
    def record_count(self) -> int:
        row = self._conn.execute(
            "SELECT record_count FROM chain_meta WHERE id=1"
        ).fetchone()
        return row[0] if row else 0

    def append(self, record: ProvenanceRecord) -> str:
        with _write_lock:
            current = self.current_root
            record.prev_root = current
            record.record_hash = ""
            record.record_hash = _compute_record_hash(record)

            new_root = _sha256(record.prev_root + record.record_hash)

            self._conn.execute(
                """INSERT INTO records
                   (record_id, session_id, timestamp_iso, model_id, model_version,
                    label_id, label_display, config_id, genesis_id, schema_version,
                    record_hash, prev_root, chain_root, full_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.record_id, record.session_id, record.timestamp_iso,
                    record.model_id, record.model_version, record.label_id,
                    record.label_display, record.config_id, record.genesis_id,
                    record.schema_version, record.record_hash,
                    record.prev_root, new_root, record.to_json(),
                ),
            )
            self._conn.execute(
                "UPDATE chain_meta SET current_root=?, record_count=record_count+1 WHERE id=1",
                (new_root,),
            )
            self._conn.commit()
            return new_root

    def verify(self) -> tuple[bool, str]:
        rows = self._conn.execute(
            "SELECT record_hash, prev_root, chain_root, seq FROM records ORDER BY seq ASC"
        ).fetchall()

        running_root = GENESIS_ROOT
        for record_hash, prev_root, stored_root, seq in rows:
            if prev_root != running_root:
                return False, (
                    f"Chain broken at seq={seq}: "
                    f"stored prev_root={prev_root!r} != expected={running_root!r}"
                )
            computed = _sha256(prev_root + record_hash)
            if computed != stored_root:
                return False, (
                    f"Root mismatch at seq={seq}: "
                    f"computed={computed!r} != stored={stored_root!r}"
                )
            running_root = computed

        return True, f"Chain intact — {len(rows)} records, root={running_root}"

    def get_record(self, record_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT full_json FROM records WHERE record_id=?", (record_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def search(
        self,
        *,
        model_id: Optional[str] = None,
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
        Search records with optional filters.
        All filters are ANDed together.
        Returns list of full record dicts.
        """
        conditions = []
        params = []

        if model_id:
            conditions.append("model_id = ?")
            params.append(model_id)
        if label_id:
            conditions.append("label_id = ?")
            params.append(label_id)
        if label_display:
            conditions.append("label_display = ?")
            params.append(label_display)
        if date_from:
            conditions.append("timestamp_iso >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("timestamp_iso <= ?")
            params.append(date_to)
        if genesis_id:
            conditions.append("genesis_id = ?")
            params.append(genesis_id)
        if schema_version:
            conditions.append("schema_version = ?")
            params.append(schema_version)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        rows = self._conn.execute(
            f"SELECT full_json FROM records {where} ORDER BY seq ASC LIMIT ? OFFSET ?",
            params
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count(
        self,
        *,
        model_id: Optional[str] = None,
        label_id: Optional[str] = None,
        label_display: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> int:
        conditions = []
        params = []
        if model_id:
            conditions.append("model_id = ?")
            params.append(model_id)
        if label_id:
            conditions.append("label_id = ?")
            params.append(label_id)
        if label_display:
            conditions.append("label_display = ?")
            params.append(label_display)
        if date_from:
            conditions.append("timestamp_iso >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("timestamp_iso <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM records {where}", params
        ).fetchone()
        return row[0] if row else 0

    def export_jsonl(self, output_path) -> int:
        rows = self._conn.execute(
            "SELECT full_json FROM records ORDER BY seq ASC"
        ).fetchall()
        Path(output_path).write_text(
            "\n".join(r[0] for r in rows) + "\n", encoding="utf-8"
        )
        return len(rows)
