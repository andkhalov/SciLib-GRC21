# API Documentation

## Base URL

```
https://scilib.tailb97193.ts.net/grag
```

## Endpoints

### GET /health

Health check.

**Response:**
```json
{"status": "healthy", "service": "lean-grag", "mode": "GRC21"}
```

### GET /info

Pipeline description and configuration.

**Response:**
```json
{
  "service": "SciLib-GRC21",
  "mode": "C21 (Structure-aware Graph + Vector RAG)",
  "pipeline": ["1. Feature extraction (regex, 0 LLM)", "..."],
  "config": {"graphdb_url": "...", "pg_host": "...", "qdrant_host": "..."}
}
```

### POST /search

Search for relevant Mathlib lemmas given a Lean 4 theorem statement.

**Request body:**
```json
{
  "lean_code": "import Mathlib\n...\ntheorem name ... := by sorry",
  "num_results": 10,
  "include_vector": true
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `lean_code` | string | required | Lean 4 code containing a theorem statement with `sorry` |
| `num_results` | int | 10 | Maximum number of hints (1–50) |
| `include_vector` | bool | true | Include Qdrant vector search results |

**Response:**
```json
{
  "hints_text": "-- Useful theorems (use with apply / exact / have):\n-- dvd_trans\n@[trans] theorem dvd_trans ...\n\n-- Useful rewrites (use with rw [...]):\n...\n\n-- Simp lemmas (use with simp [...]): dvd_refl, ...",
  "hints_structured": {
    "apply_hints": "-- Useful theorems ...",
    "rw_hints": "-- Useful rewrites ...",
    "simp_hints": "-- Simp lemmas ...",
    "vector_hints": "Similar Mathlib examples:\n..."
  },
  "hints_list": [
    {
      "name": "dvd_trans",
      "signature": "@[trans] theorem dvd_trans : a ∣ b → b ∣ c → a ∣ c",
      "role": "apply",
      "source": "graph"
    }
  ],
  "features": {
    "types": ["Nat"],
    "ops": ["eq", "lt", "dvd"]
  },
  "processing_time_ms": 650
}
```

| Field | Description |
|-------|-------------|
| `hints_text` | All hints concatenated as a single text block (ready for LLM prompt) |
| `hints_structured` | Hints grouped by tactic section |
| `hints_list` | Individual hint items with metadata |
| `features` | Detected types and operations from the input |
| `processing_time_ms` | Server-side processing time |

**Hint roles:**
- `apply` — Use with `apply`, `exact`, or `have`
- `rw` — Use with `rw [...]`
- `simp` — Use with `simp [...]`
- `vector` — Semantically similar (from vector search)

## Usage Example

### Python

```python
import requests

response = requests.post(
    "https://scilib.tailb97193.ts.net/grag/search",
    json={
        "lean_code": """import Mathlib
set_option maxHeartbeats 0
open BigOperators Real Nat Topology Rat

theorem example (n : ℕ) (h₀ : n < 101) (h₁ : 101 ∣ (123456 - n)) :
  n = 34 := by sorry"""
    }
)
hints = response.json()
print(hints["hints_text"])
```

### curl

```bash
curl -X POST https://scilib.tailb97193.ts.net/grag/search \
  -H "Content-Type: application/json" \
  -d '{"lean_code": "theorem foo (n : ℕ) (h : n > 0) : n ≥ 1 := by sorry"}'
```

### Integration with LLM Prover

```python
# 1. Get hints from SciLib-GRC21
hints = requests.post(GRAG_URL, json={"lean_code": statement}).json()

# 2. Build prompt (same format as experiment)
prompt = f"""You may find the following Mathlib lemmas useful:
{hints["hints_text"]}

Complete the following Lean 4 code:

```lean4
{statement}
```"""

# 3. Generate proof with your model
proof = model.generate(prompt)
```
