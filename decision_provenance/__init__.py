"""
decision-provenance v1.1.0

Tamper-evident audit logging for ML inference pipelines.

New in v1.1:
  - init_chain()   — explicit genesis record (required before first record)
  - migrate_chain() — schema upgrade with audit trail
  - record_async() — async version of record()
  - log_async()    — async decorator
  - search()       — query records with filters
  - count()        — count matching records
  - CLI            — python -m decision_provenance verify/stats/export/search
"""
from .logger import ProvenanceLogger
from .record import ProvenanceRecord, build_record, ValidationError, RECORD_SCHEMA_VERSION
from .chain import MerkleChain
from .config_record import ConfigChain, ConfigRecord
from .label_registry import LabelRegistry
from .genesis import GenesisChain, GenesisRecord, CURRENT_SCHEMA
from .anchor import anchor_record_ipfs, anchor_root_evm

__version__ = "1.1.1"

__all__ = [
    "ProvenanceLogger",
    "ProvenanceRecord",
    "build_record",
    "ValidationError",
    "RECORD_SCHEMA_VERSION",
    "MerkleChain",
    "ConfigChain",
    "ConfigRecord",
    "LabelRegistry",
    "GenesisChain",
    "GenesisRecord",
    "CURRENT_SCHEMA",
    "anchor_record_ipfs",
    "anchor_root_evm",
]
