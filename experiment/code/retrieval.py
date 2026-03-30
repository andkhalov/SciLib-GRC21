"""Premise retrieval from three external Lean search engines.

Each function takes a formal Lean statement and returns a list of hint strings
formatted for the experiment prompt (same layout as mode B1 in exp 140).
"""

import os
import re
import time
import logging
from typing import List

import requests

log = logging.getLogger("baselines.retrieval")

# ── Configuration ──

LEANSEARCH_URL = "https://leansearch.net/search"
LEANEXPLORE_URL = "https://www.leanexplore.com/api/v2/search"
LEANEXPLORE_API_KEY = os.getenv("LEANEXPLORE_API_KEY", "")

# Default retrieval count — same order as typical RAG hints in exp 140
DEFAULT_K = 10
REQUEST_TIMEOUT = 30  # seconds per API call
MAX_RETRIES = 2
RETRY_BACKOFF = 2.0  # seconds


def _extract_theorem_query(formal_statement: str) -> str:
    """Extract the theorem signature from a full .lean file for querying.

    Strips imports, set_option, open — keeps only the theorem/lemma/def line(s).
    """
    lines = formal_statement.strip().split('\n')
    result = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(('import ', 'set_option ', 'open ')):
            continue
        if re.match(r'^(theorem|lemma|def|example)\s+', stripped):
            capture = True
        if capture:
            result.append(stripped)
            if ':=' in stripped or 'sorry' in stripped:
                break
    query = ' '.join(result).strip()
    # Truncate if too long for API
    return query[:1500] if query else formal_statement[:1500]


def _retry_request(fn, retries=MAX_RETRIES):
    """Retry a request function with exponential backoff."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("Retry %d/%d after %.1fs: %s", attempt + 1, retries, wait, e)
                time.sleep(wait)
    log.error("All %d retries exhausted: %s", retries + 1, last_err)
    return None


# ═══════════════════════════════════════════════════════════════════════
# LEANSEARCH  (Gao et al., 2024)
# POST https://leansearch.net/search
# ═══════════════════════════════════════════════════════════════════════

def retrieve_leansearch(formal_statement: str, k: int = DEFAULT_K) -> List[str]:
    """Query LeanSearch API and return formatted hint strings."""
    query = _extract_theorem_query(formal_statement)

    def do_request():
        resp = requests.post(
            LEANSEARCH_URL,
            json={"query": [query], "num_results": k},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    data = _retry_request(do_request)
    if not data:
        return []

    hints = []
    try:
        # Response is [[result1, result2, ...]] (nested: one list per query)
        results = data[0] if isinstance(data, list) and data else []
        for item in results:
            r = item.get("result", {}) if isinstance(item, dict) else {}
            name_parts = r.get("name", [])
            name = ".".join(name_parts) if isinstance(name_parts, list) else str(name_parts)
            type_str = r.get("type", "")
            if name and type_str:
                hints.append(f"{name} : {type_str}")
            elif name:
                hints.append(name)
    except Exception as e:
        log.error("LeanSearch parse error: %s", e)

    log.info("LeanSearch returned %d hints", len(hints))
    return hints


# ═══════════════════════════════════════════════════════════════════════
# LEANEXPLORE  (Asher, 2025)
# GET https://www.leanexplore.com/api/v2/search?q=...&limit=...
# ═══════════════════════════════════════════════════════════════════════

_leanexplore_svc = None


def _get_leanexplore_service():
    """Lazy-init LeanExplore local service."""
    global _leanexplore_svc
    if _leanexplore_svc is None:
        from lean_explore.local.service import Service
        _leanexplore_svc = Service()
    return _leanexplore_svc


def retrieve_leanexplore(formal_statement: str, k: int = DEFAULT_K) -> List[str]:
    """Query LeanExplore local backend and return formatted hint strings."""
    query = _extract_theorem_query(formal_statement)

    def do_request():
        svc = _get_leanexplore_service()
        return svc.search(query, limit=k)

    result = _retry_request(do_request)
    if not result:
        return []

    hints = []
    try:
        for item in result.results[:k]:
            pd = item.primary_declaration
            name = pd.lean_name if pd else None
            stmt = item.display_statement_text or item.statement_text or ""
            if stmt:
                sig = stmt.split(":=")[0].strip()
                hints.append(sig[:300])
            elif name:
                hints.append(name)
    except Exception as e:
        log.error("LeanExplore parse error: %s", e)

    log.info("LeanExplore returned %d hints", len(hints))
    return hints


# ═══════════════════════════════════════════════════════════════════════
# LEANFINDER  (Lu et al., 2025)
# Gradio API on HuggingFace: delta-lab-ai/Lean-Finder
# ═══════════════════════════════════════════════════════════════════════

_leanfinder_client = None


def _get_leanfinder_client():
    """Lazy-init Gradio client (avoid import overhead if not needed)."""
    global _leanfinder_client
    if _leanfinder_client is None:
        from gradio_client import Client
        _leanfinder_client = Client("delta-lab-ai/Lean-Finder")
    return _leanfinder_client


def retrieve_leanfinder(formal_statement: str, k: int = DEFAULT_K) -> List[str]:
    """Query LeanFinder via Gradio API and return formatted hint strings."""
    query = _extract_theorem_query(formal_statement)

    def do_request():
        client = _get_leanfinder_client()
        result = client.predict(
            query=query,
            k=min(k, 50),  # LeanFinder max is 50
            mode="Normal",
            api_name="/retrieve",
        )
        return result

    data = _retry_request(do_request)
    if not data:
        return []

    hints = []
    try:
        # Gradio returns a tuple: (html_string, ...).
        # The HTML contains a table with <code> blocks holding formal statements.
        raw = data
        html = None
        if isinstance(raw, (list, tuple)):
            for element in raw:
                if isinstance(element, str) and '<code' in element:
                    html = element
                    break
        elif isinstance(raw, str):
            html = raw

        if html:
            # Extract content from <code ...>...</code> blocks
            code_blocks = re.findall(r'<code[^>]*>(.*?)</code>', html, re.DOTALL)
            for block in code_blocks:
                # Unescape HTML entities
                clean = block.replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&')
                clean = re.sub(r'<[^>]+>', '', clean)  # strip any nested HTML tags
                # Extract signature (before :=)
                sig = clean.split(":=")[0].strip()
                # Keep only lines with theorem/lemma/def keywords
                if re.search(r'\b(theorem|lemma|def|abbrev)\s+', sig):
                    # Collapse whitespace
                    sig = ' '.join(sig.split())[:300]
                    hints.append(sig)
    except Exception as e:
        log.error("LeanFinder parse error: %s", e)

    log.info("LeanFinder returned %d hints", len(hints))
    return hints[:k]


# ═══════════════════════════════════════════════════════════════════════
# UNIFIED INTERFACE
# ═══════════════════════════════════════════════════════════════════════

RETRIEVERS = {
    "BL_LS": retrieve_leansearch,
    "BL_LE": retrieve_leanexplore,
    "BL_LF": retrieve_leanfinder,
}


def retrieve(mode: str, formal_statement: str, k: int = DEFAULT_K) -> List[str]:
    """Dispatch retrieval by baseline mode name."""
    fn = RETRIEVERS.get(mode)
    if fn is None:
        raise ValueError(f"Unknown baseline mode: {mode}. Available: {list(RETRIEVERS.keys())}")
    return fn(formal_statement, k=k)
