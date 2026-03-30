"""SciLib-GRC21 configuration from environment variables."""

import os


# ── GraphDB ──
GRAPHDB_URL = os.getenv("GRAPHDB_URL", "http://graphdb:7200/repositories/SciLib")
GRAPHDB_TIMEOUT = float(os.getenv("GRAPHDB_TIMEOUT", "15"))
GRAPHDB_RETRIES = int(os.getenv("GRAPHDB_RETRIES", "2"))

# ── PostgreSQL ──
PG_HOST = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB = os.getenv("POSTGRES_DB", "scilib")
PG_USER = os.getenv("POSTGRES_USER", "scilib")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "YOUR_PASSWORD_HERE")
PG_TABLE = "mathlib_statements"

# ── Qdrant ──
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "scilib_mathlib_v1")

# ── Knowledge Graph ──
MATHLIB_BASES = [os.getenv("MATHLIB_BASE", "https://scilib.ai/kg/mathlib#")]
INTERP = os.getenv("INTERP_BASE", "https://scilib.ai/ontology/interpretation#")
USES_EDGES = [f"{INTERP}usesInType", f"{INTERP}usesInValue"]

# ── Server ──
HOST = os.getenv("LEAN_GRAG_HOST", "0.0.0.0")
PORT = int(os.getenv("LEAN_GRAG_PORT", "8503"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Retrieval limits ──
GRAPH_TYPE_LIMIT = int(os.getenv("GRAPH_TYPE_LIMIT", "20"))
GRAPH_VALUE_LIMIT = int(os.getenv("GRAPH_VALUE_LIMIT", "15"))
PG_ENRICHMENT_LIMIT = int(os.getenv("PG_ENRICHMENT_LIMIT", "25"))
SIMP_LIMIT = int(os.getenv("SIMP_LIMIT", "5"))
VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "5"))
VECTOR_SCORE_THRESHOLD = float(os.getenv("VECTOR_SCORE_THRESHOLD", "0.3"))
