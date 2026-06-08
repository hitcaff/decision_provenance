"""
record.py — canonical provenance record v1.1

New in v1.1:
  - schema_version: committed in the hash — closes retroactive relabelling attack
  - genesis_id: cryptographic FK to the genesis record that started this chain

What's in the hash:
  record_id, session_id, timestamp_utc, model_id, model_version, model_hash,
  input_hash, output_hash, label_id, config_id, input_schema_version,
  schema_version, genesis_id

What's NOT in the hash (by design):
  label_display  — display string, can be renamed
  threshold      — lives in ConfigChain, referenced by config_id
  runtime_env    — informational only
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

RECORD_SCHEMA_VERSION = "1.1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


class ValidationError(ValueError):
    pass


def _validate_features(features: Any, name: str = "features"):
    if not isinstance(features, dict):
        raise ValidationError(f"{name} must be a dict, got {type(features).__name__}")
    if not features:
        raise ValidationError(f"{name} must not be empty")
    try:
        json.dumps(features, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"{name} contains non-serialisable value: {e}") from e


@dataclass
class ProvenanceRecord:
    record_id: str
    session_id: str
    timestamp_utc: float
    timestamp_iso: str
    model_id: str
    model_version: str
    model_hash: str
    input_hash: str
    output_hash: str
    label_id: str
    label_display: str        # NOT in hash
    config_id: str
    input_schema_version: str
    schema_version: str       # NEW in v1.1 — IN hash
    genesis_id: str           # NEW in v1.1 — IN hash
    runtime_env: dict         # NOT in hash
    prev_root: str
    record_hash: str = field(default="")

    def __post_init__(self):
        if not self.record_hash:
            self.record_hash = _compute_record_hash(self)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def _compute_record_hash(r: ProvenanceRecord) -> str:
    payload = {
        "record_id":            r.record_id,
        "session_id":           r.session_id,
        "timestamp_utc":        r.timestamp_utc,
        "model_id":             r.model_id,
        "model_version":        r.model_version,
        "model_hash":           r.model_hash,
        "input_hash":           r.input_hash,
        "output_hash":          r.output_hash,
        "label_id":             r.label_id,
        "config_id":            r.config_id,
        "input_schema_version": r.input_schema_version,
        "schema_version":       r.schema_version,    # v1.1
        "genesis_id":           r.genesis_id,        # v1.1
    }
    return _sha256(_canonical(payload))


def build_record(
    *,
    model_id: str,
    model_version: str,
    model_hash: str,
    input_features: dict,
    output: dict,
    label_id: str,
    label_display: str,
    config_id: str,
    genesis_id: str,
    input_schema_version: str = "1.0",
    schema_version: str = RECORD_SCHEMA_VERSION,
    session_id: Optional[str] = None,
    prev_root: str = "",
) -> ProvenanceRecord:
    _validate_features(input_features, "input_features")
    _validate_features(output, "output")
    if not label_id.strip():
        raise ValidationError("label_id must not be empty")
    if not config_id.strip():
        raise ValidationError("config_id must not be empty")
    if not genesis_id.strip():
        raise ValidationError("genesis_id must not be empty — call logger.init_chain() first")

    ts = time.time()
    return ProvenanceRecord(
        record_id=str(uuid.uuid4()),
        session_id=session_id or str(uuid.uuid4()),
        timestamp_utc=ts,
        timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        model_id=model_id,
        model_version=model_version,
        model_hash=model_hash,
        input_hash=_sha256(_canonical(input_features)),
        output_hash=_sha256(_canonical(output)),
        label_id=label_id,
        label_display=label_display,
        config_id=config_id,
        genesis_id=genesis_id,
        schema_version=schema_version,
        input_schema_version=input_schema_version,
        runtime_env={
            "python": platform.python_version(),
            "os": platform.system(),
            "node": platform.node(),
        },
        prev_root=prev_root,
    )
