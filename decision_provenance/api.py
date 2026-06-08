"""
api.py — FastAPI microservice wrapper for ProvenanceLogger.

Exposes the logger as an HTTP sidecar so any service can log decisions
without importing the library directly.

Run with:
    pip install fastapi uvicorn
    python -m decision_provenance.api

Endpoints:
    POST /configure          — register a new threshold config
    POST /record             — log one decision
    GET  /verify             — verify chain integrity
    GET  /record/{record_id} — fetch a single record
    GET  /export/audit       — download JSONL audit log
    GET  /export/eu_ai_act   — download EU AI Act compliance report
    GET  /health             — liveness check
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field, field_validator
    from typing import Optional
except ImportError:
    raise ImportError("pip install fastapi uvicorn  to use the API server")

from . import ProvenanceLogger

# ---------------------------------------------------------------------------
# App + singleton logger (configured via env vars or /configure endpoint)
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Decision Provenance API",
    description="Tamper-evident audit logging for ML inference pipelines",
    version="1.0.0",
)

# CORS — allow requests from GitHub Pages portal and any localhost dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hitcaff.github.io",
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_logger: Optional[ProvenanceLogger] = None


def get_logger() -> ProvenanceLogger:
    if _logger is None:
        raise HTTPException(
            status_code=503,
            detail="Logger not initialised. POST /configure first."
        )
    return _logger


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ConfigureRequest(BaseModel):
    model_id: str          = Field(..., description="Human name of the model")
    model_version: str     = Field(..., description="Semver or git SHA")
    model_hash: Optional[str] = Field(None, description="SHA-256 of model weights")
    db_path: str           = Field("provenance.db", description="SQLite DB path")
    threshold: float       = Field(..., ge=0.0, le=1.0)
    above_label: str       = Field(..., description="Label when score >= threshold")
    below_label: str       = Field(..., description="Label when score < threshold")
    config_version: Optional[str] = None
    changed_by: str        = Field(..., description="Who/what is setting this config")
    change_reason: str     = Field(..., description="Why this config is being set")
    input_schema_version: str = "1.0"

    @field_validator("change_reason")
    @classmethod
    def reason_not_empty(cls, v):
        if not v.strip():
            raise ValueError("change_reason must not be empty")
        return v


class RecordRequest(BaseModel):
    input_features: dict   = Field(..., description="Feature dict (PII pre-stripped by caller)")
    output: dict           = Field(..., description="Raw model output dict")
    score: float           = Field(..., ge=0.0, le=1.0, description="Scalar decision score")
    session_id: Optional[str] = None


class ConfigureResponse(BaseModel):
    model_id: str
    config_id: str
    config_version: str
    threshold: float
    above_label: str
    above_label_id: str
    below_label: str
    message: str


class RecordResponse(BaseModel):
    record_id: str
    session_id: str
    timestamp_iso: str
    label_id: str
    label_display: str
    score: float
    threshold: float
    config_id: str
    chain_root: str
    record_count: int


class VerifyResponse(BaseModel):
    valid: bool
    message: str
    record_count: int
    chain_root: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/configure", response_model=ConfigureResponse, tags=["Setup"])
def configure(req: ConfigureRequest):
    """
    Initialise (or reconfigure) the provenance logger.
    Must be called before any /record requests.
    Can be called again whenever the threshold changes — creates a new versioned config.
    """
    global _logger

    if _logger is None or _logger.model_id != req.model_id:
        _logger = ProvenanceLogger(
            model_id=req.model_id,
            model_version=req.model_version,
            model_hash=req.model_hash,
            db_path=req.db_path,
            input_schema_version=req.input_schema_version,
        )
        # Init chain if no genesis exists yet
        if _logger.genesis.current(req.model_id) is None:
            _logger.init_chain(
                changed_by=req.changed_by,
                reason=f"API initialisation: {req.change_reason}",
            )

    cfg = _logger.set_config(
        threshold=req.threshold,
        above_label=req.above_label,
        below_label=req.below_label,
        config_version=req.config_version,
        changed_by=req.changed_by,
        change_reason=req.change_reason,
    )

    above_id = _logger.labels.get_id(req.above_label) or ""

    return ConfigureResponse(
        model_id=req.model_id,
        config_id=cfg.config_id,
        config_version=cfg.config_version,
        threshold=cfg.threshold,
        above_label=req.above_label,
        above_label_id=above_id,
        below_label=req.below_label,
        message="Logger configured successfully",
    )


@app.post("/record", response_model=RecordResponse, tags=["Logging"])
def log_record(req: RecordRequest):
    """
    Log one decision. Returns the record ID and current chain root.
    The chain root changes with every call — callers may store it externally
    for independent verification.
    """
    lg = get_logger()
    try:
        result = lg.record(
            input_features=req.input_features,
            output=req.output,
            score=req.score,
            session_id=req.session_id,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    return RecordResponse(**{k: result[k] for k in RecordResponse.model_fields})


@app.get("/verify", response_model=VerifyResponse, tags=["Audit"])
def verify():
    """Re-walk the full chain and verify cryptographic integrity."""
    lg = get_logger()
    ok, msg = lg.verify()
    return VerifyResponse(
        valid=ok,
        message=msg,
        record_count=lg.chain.record_count,
        chain_root=lg.chain.current_root,
    )


@app.get("/record/{record_id}", tags=["Audit"])
def get_record(record_id: str):
    """Fetch a single provenance record by ID."""
    lg = get_logger()
    rec = lg.get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Record {record_id!r} not found")
    return JSONResponse(content=rec)


@app.get("/records", tags=["Audit"])
def search_records(
    label_id: Optional[str] = None,
    label_display: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    genesis_id: Optional[str] = None,
    schema_version: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """
    Search decision records with optional filters.

    All filters are ANDed. Supports pagination via limit/offset.
    """
    lg = get_logger()
    results = lg.search(
        label_id=label_id,
        label_display=label_display,
        date_from=date_from,
        date_to=date_to,
        genesis_id=genesis_id,
        schema_version=schema_version,
        limit=limit,
        offset=offset,
    )
    total = lg.count(
        label_id=label_id,
        label_display=label_display,
        date_from=date_from,
        date_to=date_to,
    )
    return JSONResponse(content={
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": results,
    })


@app.get("/records/count", tags=["Audit"])
def count_records(
    label_id: Optional[str] = None,
    label_display: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Count matching records without fetching them."""
    lg = get_logger()
    return {"count": lg.count(
        label_id=label_id,
        label_display=label_display,
        date_from=date_from,
        date_to=date_to,
    )}


@app.get("/export/audit", tags=["Export"])
def export_audit():
    """Download the full JSONL audit log."""
    lg = get_logger()
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    lg.export_audit_log(tmp.name)
    return FileResponse(
        tmp.name,
        media_type="application/x-ndjson",
        filename=f"{lg.model_id}_audit_log.jsonl",
    )


@app.get("/export/eu_ai_act", tags=["Export"])
def export_eu_ai_act():
    """Download the EU AI Act Article 13 compliance report."""
    lg = get_logger()
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    lg.export_eu_ai_act(tmp.name)
    return FileResponse(
        tmp.name,
        media_type="application/json",
        filename=f"{lg.model_id}_eu_ai_act_report.json",
    )


@app.get("/health", tags=["Meta"])
def health():
    """Liveness check."""
    lg = _logger
    genesis = None
    if lg:
        g = lg.genesis.current(lg.model_id)
        genesis = {
            "genesis_id": g.genesis_id if g else None,
            "schema_version": g.schema_version if g else None,
            "created_by": g.created_by if g else None,
        }
    return {
        "status": "ok",
        "version": "1.1.0",
        "logger_ready": lg is not None,
        "model_id": lg.model_id if lg else None,
        "record_count": lg.chain.record_count if lg else 0,
        "genesis": genesis,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "decision_provenance.api:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=False,
    )
