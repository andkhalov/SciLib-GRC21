"""Shared configuration for miniF2F experiment modules."""

import os
from pathlib import Path
from enum import Enum

# ── Paths ──

PROJECT_ROOT = Path(__file__).resolve().parent
EXPERIMENT_ROOT = PROJECT_ROOT.parent
DATA_ROOT = EXPERIMENT_ROOT.parents[1] / 'data'
MINIF2F_DIR = DATA_ROOT / 'miniF2F-lean4' / 'MiniF2F'
PUTNAM_DIR = DATA_ROOT / 'PutnamBench' / 'lean4' / 'src'
RESULTS_DIR = EXPERIMENT_ROOT / 'results'
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

assert MINIF2F_DIR.exists(), f"MINIF2F_DIR not found: {MINIF2F_DIR}"

# ── Kafka ──

KAFKA_BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
LPC_INPUT_TOPIC = 'scilib.commands.lean.check.run.v1'
LPC_OUTPUT_TOPIC = 'scilib.events.lean.check.completed.v1'
LEAN_CHECK_TIMEOUT = 30

# ── GraphDB ──

GRAPHDB_URL = os.getenv('GRAPHDB_URL', 'http://localhost:7200/repositories/SciLib')
GRAPHDB_TIMEOUT = float(os.getenv('GRAPHDB_TIMEOUT', '15'))
GRAPHDB_RETRIES = int(os.getenv('GRAPHDB_RETRIES', '2'))

# ── PostgreSQL ──

PG_HOST = os.getenv('PG_HOST', 'localhost')
PG_PORT = int(os.getenv('PG_PORT', '5433'))
PG_DB = os.getenv('PG_DB', 'scilib')
PG_USER = os.getenv('PG_USER', 'scilib')
PG_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'YOUR_PASSWORD_HERE')
PG_TABLE = 'mathlib_statements'

MCP_LAB_BASE = os.getenv('MCP_LAB_BASE', 'http://localhost:8111')

# ── Knowledge Graph ──

MATHLIB_BASES = ['https://scilib.ai/kg/mathlib#']
INTERP = 'https://scilib.ai/ontology/interpretation#'
USES_EDGES = [f'{INTERP}usesInType', f'{INTERP}usesInValue']

# ── Experiment ──

RANDOM_SEED = 42
PASS_K = 8
MODEL_ID = 'deepseek-ai/DeepSeek-Prover-V2-7B'
MODEL_NAME = 'DeepSeek-Prover-V2-7B'
MODEL_TEMPERATURE = 0.6

LEAN_WRAPPER = 'import Mathlib\nimport Aesop\nset_option maxHeartbeats 2000000\n\n'


class Mode(Enum):
    A0 = 'Bare model, no hints'
    A1 = 'Model reasoning, no retrieval'
    B1 = 'Vector RAG (semantic search)'
    C1 = 'Graph RAG baseline (model-generated seeds)'
    C11 = 'Structure-aware Graph RAG (pattern seeds)'
    C2 = 'Graph + Vector baseline'
    C21 = 'Structure-aware Graph + Vector'
    C22 = 'Graph Tracing (reverse deps + bridges)'
    C23 = 'Graph + model re-ranking'
    A0_R1 = 'Bare model + 1 retry on error'
    A1_B1 = 'CoT + Vector RAG'
    A1_C11 = 'CoT + Structure-aware Graph RAG'
    A1_C23 = 'CoT + Graph re-ranking'
    A1_B1_R1 = 'CoT + Vector RAG + 1 retry'
    A1_C11_R1 = 'CoT + Structure-aware Graph RAG + 1 retry'
    A1_C23_R1 = 'CoT + Graph re-ranking + 1 retry'
