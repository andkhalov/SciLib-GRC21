# System Architecture

## Overview

SciLib-GRC21 is deployed as a microservice within the SciLib platform — a Kafka-first architecture for scientific knowledge management. The premise retrieval endpoint (`lean-grag`) operates independently of the LLM inference pipeline, requiring only three backend services.

## Service Topology

```
                         ┌──────────────────────────┐
    External Request ──→ │   Tailscale Funnel        │
                         │   (HTTPS termination)     │
                         └────────────┬──────────────┘
                                      │
                         ┌────────────▼──────────────┐
                         │   lean-grag (port 8503)    │
                         │   FastAPI + Uvicorn        │
                         │   Container: scilib-lean-  │
                         │   grag                     │
                         └──┬─────────┬──────────┬───┘
                            │         │          │
                   ┌────────▼──┐ ┌────▼───┐ ┌────▼──────┐
                   │  GraphDB   │ │  Post- │ │  MCP-Lab  │
                   │  (SPARQL)  │ │ greSQL │ │  → Qdrant │
                   │  port 7200 │ │  5432  │ │    8100   │
                   └────────────┘ └────────┘ └───────────┘
```

## Backend Dependencies

### GraphDB (Ontotext GraphDB Free)

- **Content**: SciLib mathematical ontology — 33.8 million RDF triples
- **Source**: Built from Mathlib4 source code analysis (Jixia metadata extractor)
- **Key edges**:
  - `usesInType`: declaration A uses declaration B in its type signature
  - `usesInValue`: declaration A uses declaration B in its proof/definition body
- **Namespace**: `https://scilib.ai/kg/mathlib#` (e.g., `https://scilib.ai/kg/mathlib#dvd_trans`)
- **Query language**: SPARQL 1.1

### PostgreSQL

- **Table**: `mathlib_statements` (213,338 rows)
- **Columns**: `uri`, `name`, `lean_code`, `module`, `source_jixia` (JSON with Lean declaration metadata including attributes, kind, signature)
- **Purpose**: Full Lean source code and attribute lookup after graph expansion

### Qdrant (via MCP-Lab)

- **Collection**: `scilib_mathlib_v1`
- **Embedding model**: SciLibMath_v1 (custom, trained on Mathlib statements)
- **Purpose**: Semantic similarity search for vector augmentation (Step 8 of C21 pipeline)
- **Access**: Through MCP-Lab HTTP API (`/tools/call` with `semantic_search` tool)

## Docker Configuration

```yaml
lean-grag:
  build:
    context: ..
    dockerfile: services/lean-grag/Dockerfile
  container_name: scilib-lean-grag
  ports:
    - "8503:8503"
  environment:
    GRAPHDB_URL: http://graphdb:7200/repositories/SciLib
    POSTGRES_HOST: postgres
    POSTGRES_PORT: "5432"
    POSTGRES_DB: scilib
    QDRANT_HOST: qdrant
    QDRANT_PORT: "6333"
    QDRANT_COLLECTION: scilib_mathlib_v1
  depends_on:
    - postgres
    - graphdb
    - qdrant
  networks:
    - scilib-network
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8503/health"]
    interval: 30s
```

## Performance

- **Latency**: 400–1300ms per request (depending on graph expansion depth)
- **No GPU required**: Zero LLM calls in the pipeline
- **Bottleneck**: SPARQL queries to GraphDB (~200ms) + PostgreSQL enrichment (~100ms)
