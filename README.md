# SciLib-GRC21: Graph-Structured Premise Retrieval for Lean 4 Theorem Proving

## Overview

This repository contains the experimental evaluation and production endpoint for **SciLib-GRC21** — a graph-structured premise retrieval system for automated Lean 4 theorem proving. The system uses a mathematical knowledge graph (SciLib ontology) to provide **categorized Mathlib lemma hints** (apply/rw/simp) to a prover model, achieving statistically significant improvements over three state-of-the-art Lean search engines.

### Key Results

On the MiniF2F benchmark (488 tasks, 50,752 runs), SciLib-GRC21 (mode C21) significantly outperforms:

| Baseline | Domain | Wilcoxon p-value | Significance |
|----------|--------|-----------------|--------------|
| **LeanSearch** (Gao et al., 2024) | ALL tasks (488) | **p = 0.032** | * |
| **LeanFinder** (Lu et al., 2025) | Partial-capability (59) | **p = 0.012** | * |
| **LeanExplore** (Asher, 2025) | ALL tasks (488) | **p = 0.001** | ** |

On tasks where the base model has partial capability (solves 1–4 out of 8 attempts), C21 achieves **48.9% pass@1** vs 37.9% for LeanSearch and 28.8% for the bare model — a near-doubling of success rate.

## Repository Structure

```
SciLib-GRC21/
├── README.md                          # This file
├── experiment/
│   ├── code/                          # Experiment source code (for reproducibility)
│   │   ├── config.py                  # Shared configuration
│   │   ├── rag.py                     # All RAG strategies (C1-C23, B1)
│   │   ├── solver.py                  # Prompt building + generation + Lean check
│   │   ├── model.py                   # DeepSeek-Prover-V2-7B interface
│   │   ├── db.py                      # PostgreSQL logging
│   │   ├── run_experiment.py          # Main experiment runner (16 modes)
│   │   ├── retrieval.py               # External baseline API clients
│   │   ├── run_baselines.py           # Baseline experiment runner
│   │   ├── run_combined.py            # Combined runner (SciLib + baselines)
│   │   └── EXPERIMENT_141.md          # Experiment 141 specification
│   ├── results/
│   │   ├── report_final.md            # Full experiment report
│   │   ├── combined_per_task.csv      # Per-task pass rates (488 tasks × 7 modes)
│   │   ├── exp140_per_task.csv        # Ablation study (244 tasks × 16 modes)
│   │   ├── summary_stats.csv          # Aggregated statistics by strata
│   │   └── generate_final_figures.py  # Figure generation script
│   └── figures/                       # All figures (EN + RU versions)
│       ├── fig_bar_strata_en.png      # pass@1 by difficulty stratum
│       ├── fig_ablation_140_en.png    # 16-mode ablation study
│       ├── fig_component_contribution.png  # Graph vs Vector contribution
│       ├── fig_strat_a0_en.png        # A0 stratification analysis
│       └── ...                        # (11 figures total)
├── endpoint/
│   ├── Dockerfile                     # Production container
│   ├── requirements.txt               # Python dependencies
│   └── src/
│       ├── main.py                    # FastAPI API (POST /search, GET /health)
│       ├── graph_rag.py               # C21 pipeline implementation
│       └── config_endpoint.py         # Environment configuration
└── docs/
    ├── architecture.md                # System architecture description
    └── api.md                         # API documentation
```

## The C21 Pipeline

SciLib-GRC21 uses zero LLM calls for premise retrieval. The pipeline:

1. **Feature Extraction** (regex) — Detects types ({Nat, Int, Real}) and operations ({eq, lt, dvd, pow, mod}) from the theorem statement.

2. **Pattern Classification** — Matches to 9 predefined mathematical patterns (e.g., `divisibility`, `ineq_pow`, `nat_arith`), each providing seed lemma names and domain filters.

3. **Seed Resolution** (SPARQL → GraphDB) — Resolves seed names to URIs in the SciLib knowledge graph (33.8M RDF triples built from Mathlib dependencies).

4. **Graph Expansion** (SPARQL) — Follows typed edges:
   - `usesInType`: lemmas sharing type-level dependencies
   - `usesInValue`: lemmas used in proofs of seeds

5. **PostgreSQL Enrichment** — Fetches full Lean code, attributes (`@[simp]`, `@[trans]`), and module metadata for all candidates.

6. **Candidate Classification** — Each lemma categorized by tactic usage:
   - `apply`: theorems for `apply`/`exact`/`have`
   - `rw`: equalities for `rw [...]`
   - `simp`: `@[simp]`-tagged for `simp [...]`
   - `def`: definitions

7. **Structured Formatting** — Hints organized with tactic guidance headers.

8. **Vector Augmentation** (Qdrant) — Semantic search adds embedding-similar lemmas.

### Key Differentiator

Unlike generic semantic search (LeanSearch, LeanFinder, LeanExplore), SciLib-GRC21 provides **tactic-annotated hints**:

```
-- Useful theorems (use with apply / exact / have):
-- dvd_trans
@[trans] theorem dvd_trans : a ∣ b → b ∣ c → a ∣ c

-- Useful rewrites (use with rw [...]):
-- Nat.cast_le
@[simp] theorem Nat.cast_le : ...

-- Simp lemmas (use with simp [...]): dvd_refl, dvd_mul_left, not_lt, ...
```

The model knows **what** to use and **how** to use it.

## Live Endpoint

The SciLib-GRC21 endpoint is available at:

```
https://scilib.tailb97193.ts.net/grag/
```

### API

**Health check:**
```bash
curl https://scilib.tailb97193.ts.net/grag/health
```

**Search for lemma hints:**
```bash
curl -X POST https://scilib.tailb97193.ts.net/grag/search \
  -H "Content-Type: application/json" \
  -d '{"lean_code": "theorem foo (n : ℕ) (h : n > 0) : n ≥ 1 := by sorry"}'
```

**Response format:**
```json
{
  "hints_text": "-- Useful theorems (use with apply):\n...",
  "hints_structured": {
    "apply_hints": "...",
    "rw_hints": "...",
    "simp_hints": "...",
    "vector_hints": "..."
  },
  "hints_list": [
    {"name": "le_antisymm", "signature": "...", "role": "apply", "source": "graph"}
  ],
  "features": {"types": ["Nat"], "ops": ["eq", "gt", "ge"]},
  "processing_time_ms": 650
}
```

**Interactive API docs:** [Swagger UI](https://scilib.tailb97193.ts.net/grag/docs)

## Infrastructure Behind the Endpoint

The endpoint runs as a Docker container (`scilib-lean-grag`, port 8503) within the SciLib microservices architecture:

```
                    ┌─────────────────────┐
  User Request ───→ │  SciLib-GRC21 API   │ (FastAPI, port 8503)
                    │  (lean-grag)        │
                    └────┬────┬────┬──────┘
                         │    │    │
                    ┌────┘    │    └────┐
                    ▼         ▼         ▼
              ┌──────────┐ ┌──────┐ ┌──────────┐
              │ GraphDB  │ │  PG  │ │  Qdrant  │
              │ (SPARQL) │ │(SQL) │ │ (Vector) │
              │ 33.8M    │ │213K  │ │ Semantic │
              │ triples  │ │stmts │ │ Search   │
              └──────────┘ └──────┘ └──────────┘
```

- **GraphDB**: Ontotext GraphDB with SciLib ontology — RDF knowledge graph of Mathlib dependencies (typed edges: `usesInType`, `usesInValue`)
- **PostgreSQL**: `mathlib_statements` table — 213,338 Lean 4 statements with full source code, Jixia metadata, and attributes
- **Qdrant**: Vector database with SciLibMath_v1 embeddings for semantic similarity search

## Experiments

### Experiment 140 — Ablation Study (MiniF2F Test, 244 tasks)

16 RAG modes tested (31,232 runs). C21 emerged as the best non-retry mode on hard tasks (10.0% pass@1 vs 3.2% for the bare model).

### Experiments 141–143 — Baseline Comparison (MiniF2F Test+Valid, 488 tasks)

Three SOTA Lean search engines compared with C21 under identical conditions (same model, same tasks, same pass@8, same temperature):

- **Exp 141**: LeanSearch + LeanFinder on Test (3,904 runs)
- **Exp 142**: A0, B1, C21, C23, BL_LS, BL_LF on Valid (11,712 runs)
- **Exp 143**: LeanExplore on Test+Valid (3,904 runs)

**Total: 50,752 runs across 488 tasks.**

See `experiment/results/report_final.md` for the full report.

## Citation

Citation details will be provided upon publication.

## License

This work is part of the SciLib project. See individual files for licensing details.
