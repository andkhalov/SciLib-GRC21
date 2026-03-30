"""SciLib-GRC21: Graph-structured premise retrieval for Lean 4.

Clean production implementation of C21 mode from experiments 140-143.
Zero LLM calls — regex pattern extraction → GraphDB expansion → PG enrichment → Qdrant vector.
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass, field as dc_field
from typing import List, Dict, Set, Tuple, Iterable, Optional

import requests
import psycopg2

import config as cfg

log = logging.getLogger("graph_rag")


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def is_clean_iri(u: str) -> bool:
    return (isinstance(u, str) and u.startswith("http")
            and " " not in u and "\n" not in u
            and "<" not in u and ">" not in u)


def _pg_conn():
    return psycopg2.connect(
        host=cfg.PG_HOST, port=cfg.PG_PORT, database=cfg.PG_DB,
        user=cfg.PG_USER, password=cfg.PG_PASSWORD,
    )


def sparql_select(query: str, timeout: float = None) -> List[Dict[str, str]]:
    timeout = timeout or cfg.GRAPHDB_TIMEOUT
    for attempt in range(cfg.GRAPHDB_RETRIES + 1):
        try:
            r = requests.post(
                cfg.GRAPHDB_URL,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=timeout,
            )
            if r.status_code != 200:
                log.warning("SPARQL HTTP %s (attempt %d)", r.status_code, attempt)
                if attempt < cfg.GRAPHDB_RETRIES:
                    time.sleep(0.3 * (2 ** attempt))
                    continue
                return []
            data = r.json()
            rows = data.get("results", {}).get("bindings", []) or []
            return [{k: v.get("value") for k, v in b.items()} for b in rows]
        except Exception as e:
            log.warning("SPARQL error (attempt %d): %s", attempt, e)
            if attempt < cfg.GRAPHDB_RETRIES:
                time.sleep(0.3 * (2 ** attempt))
    return []


# ═══════════════════════════════════════════════════════════════════════
# STEP 1: FEATURE EXTRACTION (regex, 0 LLM calls)
# ═══════════════════════════════════════════════════════════════════════

_TYPE_SYMBOLS = {
    'ℝ': 'Real', 'ℕ': 'Nat', 'ℤ': 'Int', 'ℚ': 'Rat', 'ℂ': 'Complex',
    'Finset': 'Finset', 'Multiset': 'Multiset',
}
_OP_SYMBOLS = {
    '≥': 'ge', '≤': 'le', '>': 'gt', '<': 'lt',
    '^': 'pow', '∣': 'dvd', '%': 'mod', '∑': 'sum', '∏': 'prod',
}
_RE_DIV = re.compile(r'(?<![/])/(?![/])')
_RE_MUL = re.compile(r'\*')
_RE_ADD = re.compile(r'(?<![a-zA-Z0-9_])\+')


def extract_goal_features(stmt: str) -> Dict:
    types_found, ops_found = set(), set()
    for sym, tp in _TYPE_SYMBOLS.items():
        if sym in stmt:
            types_found.add(tp)
    for sym, op in _OP_SYMBOLS.items():
        if sym in stmt:
            ops_found.add(op)
    if _RE_DIV.search(stmt):
        ops_found.add('div')
    if _RE_MUL.search(stmt):
        ops_found.add('mul')
    if _RE_ADD.search(stmt):
        ops_found.add('add')
    if '=' in stmt:
        ops_found.add('eq')
    return {'types': types_found, 'ops': ops_found}


# ═══════════════════════════════════════════════════════════════════════
# STEP 2: PATTERN CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════

GOAL_PATTERNS = [
    {'name': 'ineq_div',
     'detect': lambda f: ('ge' in f['ops'] or 'le' in f['ops']) and 'div' in f['ops'],
     'seeds': ['div_le_iff₀', 'le_div_iff₀', 'one_div', 'div_self', 'mul_div_cancel'],
     'domain_filter': "module LIKE 'Mathlib.Algebra.Order%' OR module LIKE 'Mathlib.Order%'",
     'simp_kw': ['div', 'le_div', 'div_le']},
    {'name': 'ineq_pow',
     'detect': lambda f: ('ge' in f['ops'] or 'le' in f['ops']) and 'pow' in f['ops'],
     'seeds': ['sq_nonneg', 'pow_nonneg', 'sq_abs', 'pow_succ'],
     'domain_filter': "module LIKE 'Mathlib.Algebra%' OR module LIKE 'Mathlib.Analysis%'",
     'simp_kw': ['pow', 'sq_', 'nonneg']},
    {'name': 'ineq_basic',
     'detect': lambda f: any(op in f['ops'] for op in ('ge', 'le', 'gt', 'lt')),
     'seeds': ['le_antisymm', 'not_lt', 'le_of_eq', 'mul_le_mul_of_nonneg_left'],
     'domain_filter': "module LIKE 'Mathlib.Order%' OR module LIKE 'Mathlib.Algebra.Order%'",
     'simp_kw': ['le_', 'lt_', 'nonneg']},
    {'name': 'divisibility',
     'detect': lambda f: 'dvd' in f['ops'] or 'mod' in f['ops'],
     'seeds': ['dvd_refl', 'dvd_trans', 'dvd_mul_left', 'Nat.Prime'],
     'domain_filter': "module LIKE 'Mathlib.Data.Nat%' OR module LIKE 'Mathlib.NumberTheory%' OR module LIKE 'Mathlib.RingTheory%'",
     'simp_kw': ['dvd', 'mod', 'prime']},
    {'name': 'finset_sum',
     'detect': lambda f: 'sum' in f['ops'] or 'prod' in f['ops'],
     'seeds': ['Finset.prod_le_prod', 'mul_comm', 'mul_one'],
     'domain_filter': "module LIKE 'Mathlib.Algebra.BigOperators%' OR module LIKE 'Mathlib.Combinatorics%'",
     'simp_kw': ['Finset', 'sum', 'prod']},
    {'name': 'nat_arith',
     'detect': lambda f: 'Nat' in f['types'],
     'seeds': ['Nat.Prime', 'Nat.cast_le', 'Nat.cast_lt', 'Nat.cast_inj'],
     'domain_filter': "module LIKE 'Mathlib.Data.Nat%' OR module LIKE 'Mathlib.NumberTheory%'",
     'simp_kw': ['Nat.', 'succ', 'zero']},
    {'name': 'real_field',
     'detect': lambda f: 'Real' in f['types'],
     'seeds': ['mul_comm', 'mul_one', 'div_self', 'one_div', 'sq_nonneg'],
     'domain_filter': "module LIKE 'Mathlib.Analysis%' OR module LIKE 'Mathlib.Algebra.Order%'",
     'simp_kw': ['Real', 'field', 'div']},
    {'name': 'int_arith',
     'detect': lambda f: 'Int' in f['types'],
     'seeds': ['mul_comm', 'mul_assoc', 'mul_neg', 'dvd_refl'],
     'domain_filter': "module LIKE 'Mathlib.Data.Int%' OR module LIKE 'Mathlib.NumberTheory%'",
     'simp_kw': ['Int.', 'neg', 'abs']},
    {'name': 'algebra_eq',
     'detect': lambda f: 'eq' in f['ops'] and any(op in f['ops'] for op in ('mul', 'add', 'pow')),
     'seeds': ['mul_comm', 'mul_assoc', 'mul_one', 'mul_neg'],
     'domain_filter': "module LIKE 'Mathlib.Algebra%'",
     'simp_kw': ['mul_', 'comm', 'assoc']},
]


def classify_goal(features: Dict) -> List[Dict]:
    return [p for p in GOAL_PATTERNS if p['detect'](features)]


def collect_pattern_seeds(patterns: List[Dict]) -> List[str]:
    seen: Set[str] = set()
    seeds: List[str] = []
    for pat in patterns:
        for s in pat['seeds']:
            if s not in seen:
                seen.add(s)
                seeds.append(s)
    return seeds


# ═══════════════════════════════════════════════════════════════════════
# STEP 3-4: SPARQL SEED RESOLUTION + GRAPH EXPANSION
# ═══════════════════════════════════════════════════════════════════════

def graph_find_candidates(
    names: Iterable[str],
    bases: List[str] = None,
    limit_per_name: int = 3,
) -> Dict[str, List[str]]:
    bases = bases or cfg.MATHLIB_BASES
    names = [n for n in names if isinstance(n, str) and n]
    if not names:
        return {}
    uris = [f"<{b}{n}>" for n in names for b in bases]
    values = " ".join(uris)
    q = f"""SELECT DISTINCT ?u WHERE {{
  VALUES ?u {{ {values} }}
  {{ ?u ?p ?o . }} UNION {{ ?s ?p ?u . }}
}}"""
    rows = sparql_select(q)
    found = {r["u"] for r in rows if r.get("u")}
    out: Dict[str, List[str]] = {}
    for n in names:
        cands = [f"{b}{n}" for b in bases if f"{b}{n}" in found]
        out[n] = cands[:limit_per_name]
    return out


def graph_expand_by_type(seed_uris: List[str], limit: int = None) -> List[str]:
    limit = limit or cfg.GRAPH_TYPE_LIMIT
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return []
    values = " ".join([f"<{u}>" for u in seed_uris])
    q = f"""SELECT ?neighbor (COUNT(DISTINCT ?seed) AS ?shared) WHERE {{
  VALUES ?seed {{ {values} }}
  ?seed <{cfg.INTERP}usesInType> ?type_entity .
  ?neighbor <{cfg.INTERP}usesInType> ?type_entity .
  FILTER(?neighbor != ?seed)
}} GROUP BY ?neighbor ORDER BY DESC(?shared) LIMIT {limit}"""
    return [r["neighbor"] for r in sparql_select(q) if r.get("neighbor")]


def graph_expand_by_value(seed_uris: List[str], limit: int = None) -> List[str]:
    limit = limit or cfg.GRAPH_VALUE_LIMIT
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return []
    values = " ".join([f"<{u}>" for u in seed_uris])
    q = f"""SELECT ?neighbor (COUNT(DISTINCT ?seed) AS ?co_use) WHERE {{
  VALUES ?seed {{ {values} }}
  {{ ?neighbor <{cfg.INTERP}usesInValue> ?seed . }}
  UNION
  {{ ?seed <{cfg.INTERP}usesInValue> ?neighbor . }}
  FILTER(?neighbor != ?seed)
}} GROUP BY ?neighbor ORDER BY DESC(?co_use) LIMIT {limit}"""
    return [r["neighbor"] for r in sparql_select(q) if r.get("neighbor")]


# ═══════════════════════════════════════════════════════════════════════
# STEP 5: POSTGRESQL ENRICHMENT
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class EnrichedRow:
    uri: str
    name: str
    lean_code: str
    kind: str = ""
    attrs: List[str] = dc_field(default_factory=list)
    module: str = ""


def pg_fetch_enriched(uris: List[str]) -> List[EnrichedRow]:
    uris = [u for u in uris if isinstance(u, str) and u]
    if not uris:
        return []
    ph = ",".join(["%s"] * len(uris))
    sql = f"""SELECT uri, name, lean_code, module,
       source_jixia::jsonb->'decl'->>'kind',
       source_jixia::jsonb->'decl'->'modifiers'->'attrs'
FROM {cfg.PG_TABLE} WHERE uri IN ({ph})"""
    conn = None
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(sql, uris)
        out = []
        for (uri, name, lc, mod, kind, attrs_j) in (cur.fetchall() or []):
            attrs = []
            if attrs_j:
                try:
                    parsed = json.loads(attrs_j) if isinstance(attrs_j, str) else attrs_j
                    for a in parsed:
                        n = a.get("name", [None])[0]
                        if n:
                            attrs.append(n)
                except Exception:
                    pass
            out.append(EnrichedRow(
                str(uri or ""), str(name or ""), str(lc or ""),
                str(kind or ""), attrs, str(mod or "")))
        return out
    finally:
        if conn:
            conn.close()


def pg_fetch_simp_lemmas(domain_filter: str, keywords: List[str], limit: int = None) -> List[EnrichedRow]:
    limit = limit or cfg.SIMP_LIMIT
    if not keywords:
        return []
    kw_cond = " OR ".join([f"lean_code LIKE '%%{kw}%%'" for kw in keywords])
    sql = f"""SELECT uri, name, lean_code, module,
       source_jixia::jsonb->'decl'->>'kind',
       source_jixia::jsonb->'decl'->'modifiers'->'attrs'
FROM {cfg.PG_TABLE}
WHERE source_jixia::jsonb->'decl'->'modifiers'->'attrs' @> '[{{"name": ["simp"]}}]'
  AND ({domain_filter}) AND ({kw_cond}) AND length(lean_code) < 500
ORDER BY length(lean_code) ASC LIMIT {limit}"""
    conn = None
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(sql)
        out = []
        for (uri, name, lc, mod, kind, attrs_j) in (cur.fetchall() or []):
            attrs = []
            if attrs_j:
                try:
                    parsed = json.loads(attrs_j) if isinstance(attrs_j, str) else attrs_j
                    for a in parsed:
                        n = a.get("name", [None])[0]
                        if n:
                            attrs.append(n)
                except Exception:
                    pass
            out.append(EnrichedRow(
                str(uri or ""), str(name or ""), str(lc or ""),
                str(kind or ""), attrs, str(mod or "")))
        return out
    finally:
        if conn:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════
# STEP 6: CANDIDATE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════

def classify_candidate(row: EnrichedRow) -> str:
    if 'simp' in row.attrs:
        return 'simp'
    if row.kind in ('definition', 'abbrev'):
        return 'def'
    head = row.lean_code.split(':=')[0] if row.lean_code else ""
    if '↔' in head or (' = ' in head and '≤' not in head and '≥' not in head):
        return 'rw'
    return 'apply'


# ═══════════════════════════════════════════════════════════════════════
# STEP 7: STRUCTURED FORMATTING
# ═══════════════════════════════════════════════════════════════════════

def format_structured_hints(candidates: List[Tuple[EnrichedRow, str]]) -> Dict[str, str]:
    apply_items, rw_items, simp_names = [], [], []
    for row, role in candidates:
        sig = (row.lean_code or "").split(':=')[0].split('\n  by')[0].strip()
        if len(sig) > 300:
            sig = sig[:300] + " ..."
        if role == 'apply':
            apply_items.append(f"-- {row.name}\n{sig}")
        elif role == 'rw':
            rw_items.append(f"-- {row.name}\n{sig}")
        elif role == 'simp':
            simp_names.append(row.name)
        elif role == 'def':
            apply_items.append(f"-- [def] {row.name}\n{sig}")
    result = {}
    if apply_items:
        result['apply_hints'] = ("-- Useful theorems (use with apply / exact / have):\n"
                                 + "\n\n".join(apply_items[:5]))
    if rw_items:
        result['rw_hints'] = ("-- Useful rewrites (use with rw [...]):\n"
                              + "\n\n".join(rw_items[:5]))
    if simp_names:
        result['simp_hints'] = "-- Simp lemmas (use with simp [...]): " + ", ".join(simp_names[:10])
    return result


# ═══════════════════════════════════════════════════════════════════════
# STEP 8: QDRANT VECTOR SEARCH ("+Vector" part of C21)
# ═══════════════════════════════════════════════════════════════════════

_qdrant_client = None


def _get_qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(host=cfg.QDRANT_HOST, port=cfg.QDRANT_PORT)
    return _qdrant_client


MCP_LAB_URL = os.getenv("MCP_LAB_URL", "http://mcp-lab:8100")


def vector_search(query: str, top_k: int = None, score_threshold: float = None) -> List[Dict]:
    """Search for similar Lean statements via MCP semantic_search."""
    top_k = top_k or cfg.VECTOR_TOP_K
    score_threshold = score_threshold or cfg.VECTOR_SCORE_THRESHOLD
    try:
        payload = {
            "name": "semantic_search",
            "arguments": {
                "query": query,
                "content_modality": "lean",
                "search_modality": "avg",
                "top_k": top_k,
                "score_threshold": score_threshold,
                "model_id": "SciLibMath_v1",
                "collection": cfg.QDRANT_COLLECTION,
            },
        }
        r = requests.post(f"{MCP_LAB_URL}/tools/call", json=payload, timeout=20)
        resp = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if not isinstance(resp, dict) or resp.get("error"):
            return []
        result = resp.get("result")
        if not isinstance(result, dict):
            return []
        results = result.get("results") or []
        out = []
        for item in results:
            out.append({
                'name': item.get('name', ''),
                'lean_code': item.get('lean_code', ''),
                'score': float(item.get('score', 0.0)),
            })
        return out
    except Exception as e:
        log.warning("Vector search error: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════
# C21 MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════

def _extract_theorem_stmt(formal_statement: str) -> str:
    """Extract just the theorem part from full .lean file."""
    match = re.search(r'(theorem\s+\w+[\s\S]*?:=\s*(?:by\s+)?sorry)', formal_statement)
    return match.group(1) if match else formal_statement


def _get_seed_uris(stmt: str) -> Tuple[List[Dict], List[str], List[str]]:
    """Steps 1-4: feature extraction → pattern classification → seed resolution."""
    features = extract_goal_features(stmt)
    patterns = classify_goal(features)
    seed_names = collect_pattern_seeds(patterns)
    if not seed_names:
        seed_names = ['mul_comm', 'mul_one', 'le_antisymm']
    existing = graph_find_candidates(seed_names)
    seed_uris = []
    for n in seed_names:
        for uri in existing.get(n, []):
            if uri not in seed_uris:
                seed_uris.append(uri)
    return patterns, seed_names, seed_uris


def graph_hints_c21(stmt: str) -> Dict[str, str]:
    """Full C21 pipeline: graph hints (C11) + vector augmentation.

    Returns dict with keys: apply_hints, rw_hints, simp_hints.
    """
    theorem_stmt = _extract_theorem_stmt(stmt)
    patterns, seed_names, seed_uris = _get_seed_uris(theorem_stmt)
    if not seed_uris:
        return {}

    # Step 5: Graph expansion
    type_neighbors = graph_expand_by_type(seed_uris)
    value_neighbors = graph_expand_by_value(seed_uris)

    # Step 6: PG enrichment
    all_uris = list(dict.fromkeys(seed_uris + type_neighbors[:10] + value_neighbors[:8]))
    all_rows = pg_fetch_enriched(all_uris[:cfg.PG_ENRICHMENT_LIMIT])
    rows_by_uri = {r.uri: r for r in all_rows}

    # Step 7: Simp lemmas
    simp_rows = []
    for pat in patterns[:2]:
        simp_rows.extend(pg_fetch_simp_lemmas(pat['domain_filter'], pat['simp_kw']))

    # Candidate assembly
    seen, candidates = set(), []
    for uri in seed_uris:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            candidates.append((row, classify_candidate(row)))
    for uri in type_neighbors[:10]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            candidates.append((row, 'apply'))
    for uri in value_neighbors[:8]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            candidates.append((row, 'simp' if 'simp' in row.attrs else 'rw'))
    for row in simp_rows:
        if row.name not in seen:
            seen.add(row.name)
            candidates.append((row, 'simp'))

    # Step 8: Format structured hints (graph part)
    hints = format_structured_hints(candidates)

    # Step 9: Vector augmentation (the "+Vector" part distinguishing C21 from C11)
    vec_results = vector_search(theorem_stmt)
    if vec_results:
        vec_items = []
        for vr in vec_results:
            if vr['name'] and vr['name'] not in seen:
                seen.add(vr['name'])
                code = vr.get('lean_code', '')
                sig = code.split(':=')[0].strip()[:300] if code else vr['name']
                vec_items.append(f"{vr['name']}\n{sig}")
        if vec_items:
            existing_hints = "\n\n".join(
                v for v in [hints.get('apply_hints', ''), hints.get('rw_hints', '')]
                if v
            )
            vec_text = "\n\n".join(vec_items[:3])
            if existing_hints:
                hints['vector_hints'] = f"Similar Mathlib examples:\n{vec_text}"

    return hints


def format_hints_text(hints: Dict[str, str]) -> str:
    """Format hints dict into a single text block for the prompt."""
    sections = []
    for key in ('apply_hints', 'rw_hints', 'simp_hints', 'vector_hints'):
        if key in hints:
            sections.append(hints[key])
    return "\n\n".join(sections)
