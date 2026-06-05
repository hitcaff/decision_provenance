"""
label_registry.py — stable label ID registry.

Hash L001, not "approved". The display string can change; the ID never does.
Registry is itself hashed and stored — any addition is auditable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LabelRegistry:
    """
    Append-only registry mapping stable label IDs to display strings.

    IDs are auto-assigned as L001, L002, ... and never change.
    Display strings can be updated (logged as a new version of the same ID).
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._init_db()
        self._cache: dict[str, str] = {}   # label_id -> display string
        self._reload_cache()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS label_registry (
                label_id      TEXT NOT NULL,
                display       TEXT NOT NULL,
                version       INTEGER NOT NULL DEFAULT 1,
                registered_at TEXT NOT NULL,
                registry_hash TEXT NOT NULL,
                PRIMARY KEY (label_id, version)
            );
        """)
        self._conn.commit()

    def _reload_cache(self):
        rows = self._conn.execute(
            """SELECT label_id, display FROM label_registry
               WHERE version = (
                   SELECT MAX(version) FROM label_registry l2
                   WHERE l2.label_id = label_registry.label_id
               )"""
        ).fetchall()
        self._cache = {r[0]: r[1] for r in rows}

    def register(self, display: str) -> str:
        """
        Register a new label display string and return its stable ID.
        If display already exists (case-insensitive), returns existing ID.
        """
        import time
        # Check for existing match (case-insensitive)
        for lid, disp in self._cache.items():
            if disp.lower() == display.lower():
                return lid

        # New ID
        count = len(self._cache) + 1
        label_id = f"L{count:03d}"
        registry_hash = _sha256(f"{label_id}:{display}:{time.time()}")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self._conn.execute(
            "INSERT INTO label_registry (label_id, display, version, registered_at, registry_hash) VALUES (?,?,1,?,?)",
            (label_id, display, ts, registry_hash)
        )
        self._conn.commit()
        self._cache[label_id] = display
        return label_id

    def get_display(self, label_id: str) -> Optional[str]:
        return self._cache.get(label_id)

    def get_id(self, display: str) -> Optional[str]:
        for lid, disp in self._cache.items():
            if disp.lower() == display.lower():
                return lid
        return None

    def all_labels(self) -> dict[str, str]:
        return dict(self._cache)
