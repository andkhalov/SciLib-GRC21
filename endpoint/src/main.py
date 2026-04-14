"""SciLib-GRC21: Graph-structured premise retrieval API for Lean 4.

Endpoint POST /search accepts Lean 4 code and returns structured hints
using the C21 Graph+Vector RAG pipeline (zero LLM calls).
"""

import logging
import time
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import config as cfg
from graph_rag import graph_hints_c21, format_hints_text, extract_goal_features
from lean_checker import has_proof_content, get_checker

# ── Logging ──

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lean-grag")

# ── FastAPI app ──

app = FastAPI(
    title="SciLib-GRC21",
    description="Graph-structured premise retrieval and proof verification for Lean 4. "
                "POST /search — categorized Mathlib lemma hints (apply/rw/simp). "
                "POST /check — Lean 4 proof verification via Mathlib REPL.",
    version="1.1.0",
)


# ── Models ──

class SearchRequest(BaseModel):
    lean_code: str = Field(..., description="Lean 4 code (theorem statement with sorry)")
    num_results: int = Field(default=10, ge=1, le=50, description="Max hints to return")
    include_vector: bool = Field(default=True, description="Include Qdrant vector search results")


class HintItem(BaseModel):
    name: str
    signature: str
    role: str  # apply, rw, simp, def, vector
    source: str  # graph, simp, vector


class SearchResponse(BaseModel):
    hints_text: str = Field(description="Formatted hints text for LLM prompt")
    hints_structured: Dict[str, str] = Field(description="Hints by section (apply_hints, rw_hints, simp_hints)")
    hints_list: List[HintItem] = Field(description="Individual hint items")
    features: Dict = Field(description="Detected goal features (types, ops)")
    processing_time_ms: int = Field(description="Processing time in milliseconds")


# ── Endpoints ──

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "lean-grag", "mode": "GRC21"}


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """Search for relevant Mathlib lemmas given a Lean 4 theorem statement.

    Uses C21 pipeline: regex pattern extraction → GraphDB expansion →
    PostgreSQL enrichment → candidate classification → Qdrant vector augmentation.
    Zero LLM calls.
    """
    t0 = time.time()

    try:
        features = extract_goal_features(req.lean_code)
        hints = graph_hints_c21(req.lean_code)
    except Exception as e:
        log.error("Search error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    hints_text = format_hints_text(hints)
    processing_ms = int((time.time() - t0) * 1000)

    # Build individual hint items from sections
    hint_items = []
    for section_key, section_text in hints.items():
        source = "vector" if section_key == "vector_hints" else ("simp" if section_key == "simp_hints" else "graph")
        role_map = {"apply_hints": "apply", "rw_hints": "rw", "simp_hints": "simp", "vector_hints": "vector"}
        role = role_map.get(section_key, "apply")

        if section_key == "simp_hints":
            # Simp hints are comma-separated names
            names_part = section_text.split(": ", 1)[1] if ": " in section_text else section_text
            for name in names_part.split(", "):
                name = name.strip()
                if name:
                    hint_items.append(HintItem(name=name, signature="", role="simp", source="simp"))
        else:
            # Parse "-- name\nsignature" blocks
            blocks = section_text.split("\n\n")
            for block in blocks:
                lines = block.strip().split("\n")
                if len(lines) >= 2 and lines[0].startswith("-- "):
                    name = lines[0][3:].strip()
                    sig = "\n".join(lines[1:]).strip()
                    hint_items.append(HintItem(name=name, signature=sig[:300], role=role, source=source))

    log.info("Search completed: %d hints, %dms, features=%s",
             len(hint_items), processing_ms, features)

    return SearchResponse(
        hints_text=hints_text,
        hints_structured=hints,
        hints_list=hint_items,
        features=features,
        processing_time_ms=processing_ms,
    )


class CheckRequest(BaseModel):
    lean_code: str = Field(..., description="Lean 4 code to verify")
    timeout: int = Field(default=30, ge=5, le=120, description="Verification timeout in seconds")


class CheckResponse(BaseModel):
    success: bool = Field(description="True if Lean verified the proof without errors")
    error_class: Optional[str] = Field(default=None, description="Error category (TACTIC_FAILURE, PARSE_ERROR, etc.)")
    error_message: str = Field(default="", description="Lean error message (truncated to 2000 chars)")
    time_ms: int = Field(default=0, description="Lean verification time in milliseconds")
    sanity_ok: bool = Field(description="True if code passed sanity checks (not sorry/empty/NL)")
    sanity_reason: str = Field(default="OK", description="Sanity check result explanation")
    processing_time_ms: int = Field(description="Total processing time including sanity + Lean check")


@app.post("/check", response_model=CheckResponse)
async def check(req: CheckRequest):
    """Verify Lean 4 code using the Mathlib REPL.

    Includes sanity checks: rejects sorry-only proofs, bare imports,
    comment-only code, and natural language text that Lean would
    trivially accept without actually verifying anything.

    Environment: Lean 4.28.0-rc1, Mathlib (Lean 4.26.0 toolchain).
    """
    t0 = time.time()

    # Sanity check first
    sanity_ok, sanity_reason = has_proof_content(req.lean_code)
    if not sanity_ok:
        processing_ms = int((time.time() - t0) * 1000)
        log.info("Check rejected by sanity: %s", sanity_reason)
        return CheckResponse(
            success=False,
            error_class="SANITY_CHECK_FAILED",
            error_message=sanity_reason,
            time_ms=0,
            sanity_ok=False,
            sanity_reason=sanity_reason,
            processing_time_ms=processing_ms,
        )

    # Send to Lean service via Kafka
    try:
        checker = get_checker()
        result = checker.check(req.lean_code, timeout=req.timeout)
    except Exception as e:
        log.error("Lean check error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    processing_ms = int((time.time() - t0) * 1000)

    log.info("Check completed: success=%s, error=%s, lean_ms=%s, total_ms=%s",
             result['success'], result.get('error_class'), result.get('time_ms'), processing_ms)

    return CheckResponse(
        success=result['success'],
        error_class=result.get('error_class'),
        error_message=result.get('error_message', ''),
        time_ms=result.get('time_ms', 0),
        sanity_ok=True,
        sanity_reason="OK",
        processing_time_ms=processing_ms,
    )


@app.get("/info")
async def info():
    """Service information and configuration."""
    return {
        "service": "SciLib-GRC21",
        "mode": "C21 (Structure-aware Graph + Vector RAG)",
        "description": "Graph-structured premise retrieval for Lean 4",
        "pipeline": [
            "1. Feature extraction (regex, 0 LLM)",
            "2. Pattern classification (9 patterns)",
            "3. Seed resolution (SPARQL → GraphDB)",
            "4. Graph expansion (usesInType, usesInValue)",
            "5. PostgreSQL enrichment (lean_code, attrs)",
            "6. Candidate classification (apply/rw/simp/def)",
            "7. Structured formatting",
            "8. Vector augmentation (Qdrant)",
        ],
        "config": {
            "graphdb_url": cfg.GRAPHDB_URL,
            "pg_host": cfg.PG_HOST,
            "qdrant_host": cfg.QDRANT_HOST,
            "qdrant_collection": cfg.QDRANT_COLLECTION,
        },
    }


if __name__ == "__main__":
    log.info("Starting SciLib-GRC21 on %s:%d", cfg.HOST, cfg.PORT)
    uvicorn.run(app, host=cfg.HOST, port=cfg.PORT, log_level=cfg.LOG_LEVEL.lower())
