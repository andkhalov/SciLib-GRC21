"""Retrieval-Augmented Generation: graph (SPARQL/PG) and semantic (MCP) pipelines.

All RAG strategies for modes B1, C1, C11, C2, C21, C22, C23.
"""

import os
import re
import json
import time
import random
import logging
from dataclasses import dataclass, field as dc_field
from typing import List, Dict, Tuple, Optional, Set, Iterable

import requests
import psycopg2

from .config import (
    GRAPHDB_URL, GRAPHDB_TIMEOUT, GRAPHDB_RETRIES,
    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD, PG_TABLE,
    MATHLIB_BASES, INTERP, USES_EDGES, MCP_LAB_BASE,
)

log = logging.getLogger("rag")

# ═════════════════════════════════════════════════════════════════════════════
# HELPERS: SPARQL + PostgreSQL
# ═════════════════════════════════════════════════════════════════════════════

def is_clean_iri(u: str) -> bool:
    return (isinstance(u, str) and u.startswith("http")
            and " " not in u and "\n" not in u
            and "<" not in u and ">" not in u)


def sparql_select(query: str, timeout: float = GRAPHDB_TIMEOUT) -> List[Dict[str, str]]:
    """Execute SPARQL SELECT and return list of binding dicts."""
    for attempt in range(GRAPHDB_RETRIES + 1):
        try:
            r = requests.post(
                GRAPHDB_URL,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=timeout,
            )
            if r.status_code != 200:
                log.warning("SPARQL HTTP %s (attempt %d)", r.status_code, attempt)
                if attempt < GRAPHDB_RETRIES:
                    time.sleep(0.3 * (2 ** attempt))
                    continue
                return []
            data = r.json()
            rows = data.get("results", {}).get("bindings", []) or []
            return [{k: v.get("value") for k, v in b.items()} for b in rows]
        except Exception as e:
            log.warning("SPARQL error (attempt %d): %s", attempt, e)
            if attempt < GRAPHDB_RETRIES:
                time.sleep(0.3 * (2 ** attempt))
    return []


def _pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, database=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
    )


# ═════════════════════════════════════════════════════════════════════════════
# GRAPH: SHARED (find candidates, expand neighbors)
# ═════════════════════════════════════════════════════════════════════════════

def graph_find_candidates(
    names: Iterable[str],
    bases: List[str] = MATHLIB_BASES,
    limit_per_name: int = 3,
) -> Dict[str, List[str]]:
    """Lookup names in GraphDB, return {name: [existing_uris]}."""
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


@dataclass
class StatementRow:
    uri: str
    name: str
    lean_code: str


def pg_fetch_statements_by_uri(uris: List[str]) -> List[StatementRow]:
    """Fetch basic statement info by URI."""
    uris = [u for u in uris if isinstance(u, str) and u]
    if not uris:
        return []
    ph = ",".join(["%s"] * len(uris))
    sql = f"SELECT uri, name, lean_code FROM {PG_TABLE} WHERE uri IN ({ph})"
    conn = None
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(sql, uris)
        return [StatementRow(str(r[0] or ""), str(r[1] or ""), str(r[2] or ""))
                for r in (cur.fetchall() or [])]
    finally:
        if conn:
            conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# C1: OLD GRAPH RAG (model tokens → graph → PG)
# ═════════════════════════════════════════════════════════════════════════════

_RE_UNDERSCORE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*_[A-Za-z0-9_]*")


def _extract_underscore_tokens(lines: List[str]) -> Set[str]:
    out = set()
    for s in lines:
        for tok in _RE_UNDERSCORE.findall(s):
            out.add(tok)
    return out


def _grag_request_gen(task: str) -> List[str]:
    """Ask model for MathLib identifiers (used by C1)."""
    from . import model

    prompt = (
        "You are given a Lean 4 theorem statement.\n"
        "Task:\n"
        "- Output ONLY a list of EXACT Lean MathLib identifiers "
        "(lemma or theorem or tactic name) that would likely be used to solve the theorem.\n"
        "- Output 3 to 8 identifiers.\n"
        "REQUESTED OUTPUT: [name1, name2, name3, ...]\n"
        f"Statement:\n{task}"
    )
    raw = model.generate(prompt, max_new_tokens=256, temperature=0.8, do_sample=True)
    text = raw.split("<｜end▁of▁sentence｜>")[0].strip()
    lines = text.split("\n")
    deny = ("lean", "```", "mathlib", "theorem")
    return [
        s.strip().split("--", 1)[0].rstrip()
        for s in lines
        if len(s.strip()) >= 3 and not any(d in s.lower() for d in deny)
    ]


def _collect_tokens(task: str, target_size: int = 7, max_iters: int = 30) -> Set[str]:
    acc: Set[str] = set()
    for _ in range(max_iters):
        lines = _grag_request_gen(task)
        acc |= _extract_underscore_tokens(lines)
        if len(acc) >= target_size:
            break
    return acc


def graph_expand_neighbors(seed_uris: List[str], per_seed: int = 30) -> Dict[str, Set[str]]:
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return {}
    values = " ".join([f"<{u}>" for u in seed_uris])
    edges_list = ", ".join([f"<{p}>" for p in USES_EDGES])
    q = f"""SELECT ?a ?b WHERE {{
  VALUES ?s {{ {values} }}
  {{ ?s ?p ?b . FILTER(?p IN ({edges_list})) BIND(?s as ?a) }}
  UNION
  {{ ?a ?p ?s . FILTER(?p IN ({edges_list})) }}
}} LIMIT {per_seed * len(seed_uris)}"""
    rows = sparql_select(q)
    adj: Dict[str, Set[str]] = {}
    for r in rows:
        a, b = r.get("a"), r.get("b")
        if not a or not b or a == b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _pick_top_connected(seed_uris: List[str], adj: Dict[str, Set[str]], k_max: int = 5) -> List[str]:
    seed_set = set(seed_uris)

    def score(u: str) -> float:
        neigh = adj.get(u, set())
        return len(neigh & seed_set) + 0.25 * len(neigh) + (0.2 if u in seed_set else 0)

    ranked = sorted(set(adj.keys()) | seed_set, key=score, reverse=True)
    return ranked[:k_max]


def graph_hints(stmt: str, target_size: int = 7) -> List[str]:
    """C1: model-generated tokens → graph lookup → PG fetch → flat hint list."""
    tokens = _collect_tokens(stmt, target_size=target_size)
    raw = list(tokens)

    mapping = graph_find_candidates(raw[:20], limit_per_name=2)
    seed_uris = []
    for h in raw[:20]:
        for uri in mapping.get(h, []):
            if uri not in seed_uris:
                seed_uris.append(uri)
    seed_uris = seed_uris[:10]
    if not seed_uris:
        return []

    adj = graph_expand_neighbors(seed_uris, per_seed=40)
    picked = list(seed_uris)
    extra = _pick_top_connected(seed_uris, adj, k_max=5)
    for u in extra:
        if u not in picked:
            picked.append(u)
    picked = picked[:5]

    rows = pg_fetch_statements_by_uri(picked)
    return [f"{r.name}\n{r.lean_code}" for r in rows if r.name and r.lean_code][:5]


# ═════════════════════════════════════════════════════════════════════════════
# C11: STRUCTURE-AWARE GRAPH RAG (pattern seeds → typed expansion)
# ═════════════════════════════════════════════════════════════════════════════

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


# ── Typed expansion (C11) ────────────────────────────────────────────────────

def graph_expand_by_type(seed_uris: List[str], limit: int = 20) -> List[str]:
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return []
    values = " ".join([f"<{u}>" for u in seed_uris])
    q = f"""SELECT ?neighbor (COUNT(DISTINCT ?seed) AS ?shared) WHERE {{
  VALUES ?seed {{ {values} }}
  ?seed <{INTERP}usesInType> ?type_entity .
  ?neighbor <{INTERP}usesInType> ?type_entity .
  FILTER(?neighbor != ?seed)
}} GROUP BY ?neighbor ORDER BY DESC(?shared) LIMIT {limit}"""
    return [r["neighbor"] for r in sparql_select(q) if r.get("neighbor")]


def graph_expand_by_value(seed_uris: List[str], limit: int = 15) -> List[str]:
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return []
    values = " ".join([f"<{u}>" for u in seed_uris])
    q = f"""SELECT ?neighbor (COUNT(DISTINCT ?seed) AS ?co_use) WHERE {{
  VALUES ?seed {{ {values} }}
  {{ ?neighbor <{INTERP}usesInValue> ?seed . }}
  UNION
  {{ ?seed <{INTERP}usesInValue> ?neighbor . }}
  FILTER(?neighbor != ?seed)
}} GROUP BY ?neighbor ORDER BY DESC(?co_use) LIMIT {limit}"""
    return [r["neighbor"] for r in sparql_select(q) if r.get("neighbor")]


# ── Enriched PG fetch (C11+) ─────────────────────────────────────────────────

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
FROM {PG_TABLE} WHERE uri IN ({ph})"""
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


def pg_fetch_simp_lemmas(domain_filter: str, keywords: List[str], limit: int = 8) -> List[EnrichedRow]:
    if not keywords:
        return []
    kw_cond = " OR ".join([f"lean_code LIKE '%%{kw}%%'" for kw in keywords])
    sql = f"""SELECT uri, name, lean_code, module,
       source_jixia::jsonb->'decl'->>'kind',
       source_jixia::jsonb->'decl'->'modifiers'->'attrs'
FROM {PG_TABLE}
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


# ── Candidate classification + formatting ─────────────────────────────────────

def classify_candidate(row: EnrichedRow) -> str:
    if 'simp' in row.attrs:
        return 'simp'
    if row.kind in ('definition', 'abbrev'):
        return 'def'
    head = row.lean_code.split(':=')[0] if row.lean_code else ""
    if '↔' in head or (' = ' in head and '≤' not in head and '≥' not in head):
        return 'rw'
    return 'apply'


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


def _get_seed_uris(stmt: str) -> Tuple[List[Dict], List[str], List[str]]:
    """Shared seed resolution for C11/C22/C23. Returns (patterns, seed_names, seed_uris)."""
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


def graph_hints_v2(stmt: str) -> Dict[str, str]:
    """C11: structure-aware graph RAG."""
    patterns, seed_names, seed_uris = _get_seed_uris(stmt)
    if not seed_uris:
        return {}

    type_neighbors = graph_expand_by_type(seed_uris, limit=20)
    value_neighbors = graph_expand_by_value(seed_uris, limit=15)

    all_uris = list(dict.fromkeys(seed_uris + type_neighbors[:10] + value_neighbors[:8]))
    all_rows = pg_fetch_enriched(all_uris[:25])
    rows_by_uri = {r.uri: r for r in all_rows}

    simp_rows = []
    for pat in patterns[:2]:
        simp_rows.extend(pg_fetch_simp_lemmas(pat['domain_filter'], pat['simp_kw'], limit=5))

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

    return format_structured_hints(candidates)


# ═════════════════════════════════════════════════════════════════════════════
# C22: GRAPH TRACING (reverse deps + bridges)
# ═════════════════════════════════════════════════════════════════════════════

def graph_reverse_deps(seed_uris: List[str], limit: int = 15) -> List[str]:
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return []
    values = " ".join([f"<{u}>" for u in seed_uris])
    q = f"""SELECT ?dep (COUNT(DISTINCT ?seed) AS ?cnt) WHERE {{
  VALUES ?seed {{ {values} }}
  ?seed <{INTERP}usesInValue> ?dep .
  FILTER(?dep != ?seed)
}} GROUP BY ?dep ORDER BY DESC(?cnt) LIMIT {limit}"""
    return [r["dep"] for r in sparql_select(q) if r.get("dep")]


def graph_bridge_filtered(seed_uris: List[str], limit: int = 15) -> List[str]:
    seed_uris = [u for u in seed_uris if is_clean_iri(u)]
    if not seed_uris:
        return []
    values = " ".join([f"<{u}>" for u in seed_uris])
    q = f"""SELECT ?bridge (COUNT(DISTINCT ?seed) AS ?shared) WHERE {{
  VALUES ?seed {{ {values} }}
  ?bridge <{INTERP}usesInValue> ?seed .
  FILTER(?bridge != ?seed)
}} GROUP BY ?bridge HAVING (COUNT(DISTINCT ?seed) >= 2)
ORDER BY DESC(?shared) LIMIT {limit}"""
    return [r["bridge"] for r in sparql_select(q) if r.get("bridge")]


def graph_hints_c22(stmt: str) -> Dict[str, str]:
    """C22: graph tracing — reverse deps + size-filtered bridges."""
    patterns, _, seed_uris = _get_seed_uris(stmt)
    if not seed_uris:
        return {}

    rev_deps = graph_reverse_deps(seed_uris, limit=15)
    bridges = graph_bridge_filtered(seed_uris, limit=15)

    all_uris = list(dict.fromkeys(seed_uris + rev_deps[:12] + bridges[:10]))
    all_rows = pg_fetch_enriched(all_uris[:35])
    bridge_set = set(bridges)
    rows_by_uri = {r.uri: r for r in all_rows
                   if r.uri not in bridge_set or len(r.lean_code or "") <= 500}

    simp_rows = []
    for pat in patterns[:2]:
        simp_rows.extend(pg_fetch_simp_lemmas(pat['domain_filter'], pat['simp_kw'], limit=5))

    seen, candidates = set(), []
    for uri in seed_uris:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            candidates.append((row, classify_candidate(row)))
    for uri in rev_deps[:12]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            candidates.append((row, classify_candidate(row)))
    for uri in bridges[:10]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            candidates.append((row, 'apply'))
    for row in simp_rows:
        if row.name not in seen:
            seen.add(row.name)
            candidates.append((row, 'simp'))

    return format_structured_hints(candidates)


# ═════════════════════════════════════════════════════════════════════════════
# C23: GRAPH + MODEL RE-RANKING
# ═════════════════════════════════════════════════════════════════════════════

def model_rerank_candidates(
    candidates: List[Tuple[EnrichedRow, str]],
    stmt: str,
    max_show: int = 25,
    max_select: int = 8,
) -> List[Tuple[EnrichedRow, str]]:
    """Give numbered list to LLM, ask it to pick useful candidates."""
    from . import model

    if not candidates:
        return []
    show = candidates[:max_show]
    numbered = []
    for i, (row, role) in enumerate(show):
        sig = (row.lean_code or "").split(":=")[0].split("\n  by")[0].strip()
        if len(sig) > 200:
            sig = sig[:200] + "..."
        numbered.append(f"{i+1}. [{role}] {row.name}: {sig}")

    rerank_prompt = (
        "You are selecting Lean 4 lemmas for a proof.\n\n"
        f"Statement to prove:\n{stmt}\n\n"
        f"Available lemmas (numbered):\n" + "\n".join(numbered) + "\n\n"
        "Task: Return ONLY the numbers of lemmas useful for this proof.\n"
        "Output: comma-separated numbers, nothing else.\n"
        "Example: 1, 3, 5, 8"
    )
    raw = model.generate(rerank_prompt, max_new_tokens=64, do_sample=False,
                         temperature=1.0, repetition_penalty=1.0)
    text = raw.split("<｜end▁of▁sentence｜>")[0].strip()

    selected = set()
    for tok in re.findall(r"\d+", text):
        idx = int(tok) - 1
        if 0 <= idx < len(show):
            selected.add(idx)
    if not selected:
        return show[:max_select]
    return [show[i] for i in sorted(selected)][:max_select]


def graph_hints_c23(stmt: str) -> Dict[str, str]:
    """C23: all graph strategies → model re-ranking → structured hints."""
    patterns, _, seed_uris = _get_seed_uris(stmt)
    if not seed_uris:
        return {}

    rev_deps = graph_reverse_deps(seed_uris, limit=12)
    bridges = graph_bridge_filtered(seed_uris, limit=10)
    type_nb = graph_expand_by_type(seed_uris, limit=10)
    value_nb = graph_expand_by_value(seed_uris, limit=10)

    all_uris = list(dict.fromkeys(
        seed_uris + rev_deps + bridges[:8] + type_nb[:8] + value_nb[:8]))
    all_rows = pg_fetch_enriched(all_uris[:40])
    rows_by_uri = {r.uri: r for r in all_rows}

    seen, raw = set(), []
    for uri in seed_uris:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            raw.append((row, classify_candidate(row)))
    for uri in rev_deps:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            raw.append((row, classify_candidate(row)))
    for uri in bridges[:8]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen and len(row.lean_code or "") < 500:
            seen.add(row.name)
            raw.append((row, 'apply'))
    for uri in type_nb[:8]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            raw.append((row, 'apply'))
    for uri in value_nb[:8]:
        row = rows_by_uri.get(uri)
        if row and row.name not in seen:
            seen.add(row.name)
            raw.append((row, 'simp' if 'simp' in row.attrs else 'rw'))

    selected = model_rerank_candidates(raw, stmt)
    return format_structured_hints(selected)


# ═════════════════════════════════════════════════════════════════════════════
# B1/A1: SEMANTIC RAG (MCP semantic search)
# ═════════════════════════════════════════════════════════════════════════════

def _mcp_semantic_search(query: str, target_modality: str = "avg") -> Tuple[Optional[str], Optional[str], float]:
    """Single semantic search call via MCP."""
    payload = {
        "name": "semantic_search",
        "arguments": {
            "query": query,
            "content_modality": "lean",
            "search_modality": target_modality,
            "top_k": 5,
            "score_threshold": 0.3,
            "model_id": "SciLibMath_v1",
            "collection": "scilib_mathlib_v1",
        },
    }
    try:
        r = requests.post(MCP_LAB_BASE + "/tools/call", json=payload, timeout=20)
        resp = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if not isinstance(resp, dict) or resp.get("error"):
            return None, None, 0.0
        result = resp.get("result")
        if not isinstance(result, dict):
            return None, None, 0.0
        results = result.get("results") or []
        if not results:
            return None, None, 0.0
        top = results[0]
        return top.get("name"), top.get("lean_code"), float(top.get("score", 0.0))
    except Exception:
        return None, None, 0.0


def _hints_gen(stmt: str) -> Tuple[List[str], str, List[str]]:
    """Generate hint identifiers with model, then search MCP for each."""
    from . import model

    prompt = (
        "You are given a Lean 4 theorem statement. Do NOT provide a proof.\n"
        "Provide lemmas, theorems and statements that can be used in final proof.\n"
        "Task:\n"
        "- Output ONLY Lean 4 code.\n"
        "- Output at most 5 lines, each: `#check <identifier>`.\n"
        "- Prefer generic rewriting/inequality/algebra lemmas.\n"
        "- Do not output natural language or the proof.\n\n"
        f"Theorem statement:\n{stmt}"
    )
    raw = model.generate(prompt, max_new_tokens=1048, temperature=0.4, do_sample=True)
    text = raw.split("<｜end▁of▁sentence｜>")[0].strip()
    lines = text.split("\n")
    deny = ("lean", "```", "mathlib", "theorem")
    filtered_list = [
        s.strip().split("--", 1)[0].rstrip()
        for s in lines
        if len(s.strip()) >= 20 and not any(d in s.lower() for d in deny)
    ]

    semantic_hints = []
    for hint in filtered_list:
        name, code, score = _mcp_semantic_search(hint)
        if name and score > 0.3:
            semantic_hints.append(f"{name}\n{code}")

    one_line = re.sub(r"\s+", " ", "\n".join(lines)
                      .replace("<｜end▁of▁sentence｜>", "")).strip()
    one_line = re.sub(r"```+\s*lean4\s*```+", " ", one_line, flags=re.IGNORECASE)
    one_line = re.sub(r"```+", " ", one_line)
    one_line = re.sub(r"\blean4\b", " ", one_line, flags=re.IGNORECASE)
    one_line = re.sub(r"\s+", " ", one_line).strip()

    return filtered_list, one_line, semantic_hints


def start_hints(stmt: str, min_results: int = 2, max_iters: int = 20) -> Tuple[List[str], str, List[str]]:
    """Retry hint generation until we get enough semantic results."""
    for attempt in range(max_iters):
        filtered_list, one_line, semantic_hints = _hints_gen(stmt)
        if len(semantic_hints) >= min_results:
            return filtered_list, one_line, semantic_hints
    return filtered_list, one_line, semantic_hints
