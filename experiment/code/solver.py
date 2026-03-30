"""Mode dispatch: build prompts, call model, check with Lean.

Prompt layout (tested on DeepSeek-Prover-V2-7B):
- Hints/context BEFORE the code fence (model stays in code-gen mode)
- Code fence with full formal statement is ALWAYS last in the prompt
- CoT (A1): two-step — first generate reasoning (2048 tok), then use it as context

solve_task() is the high-level API combining prompt building + generation + checking.
"""

import re
import time
import logging
from typing import Dict, Tuple, Optional

from .config import Mode, LEAN_CHECK_TIMEOUT
from . import model
from . import rag
from .lean import LeanChecker

log = logging.getLogger("solver")

# Singleton Lean checker (connect once, reuse)
_lean_checker: Optional[LeanChecker] = None


def get_lean_checker() -> LeanChecker:
    global _lean_checker
    if _lean_checker is None:
        _lean_checker = LeanChecker()
    if not _lean_checker.connected:
        _lean_checker.connect()
    return _lean_checker


# ═════════════════════════════════════════════════════════════════════════════
# PROMPT BUILDING
# ═════════════════════════════════════════════════════════════════════════════

# Code fence block — ALWAYS last in prompt (model continues generating code)
_CODE_FENCE = "Complete the following Lean 4 code:\n\n```lean4\n{formal_statement}\n```"

# Hints placed BEFORE the code fence
_HINTS_PREFIX = "You may find the following Mathlib lemmas useful:\n{hints}\n\n"

# CoT step 1: ask for reasoning plan (short, focused)
_COT_PLAN_PROMPT = (
    "Analyze the following Lean 4 theorem and provide a brief proof strategy.\n"
    "List the key Mathlib tactics and lemmas needed. Be concise.\n\n"
    "```lean4\n{formal_statement}\n```"
)

# CoT step 2: reasoning as context before fence
_COT_CONTEXT_PREFIX = "Proof strategy:\n{reasoning}\n\n"


def _format_structured_sections(hints: Dict[str, str]) -> str:
    sections = []
    for key in ('apply_hints', 'rw_hints', 'simp_hints'):
        if key in hints:
            sections.append(hints[key])
    return "\n\n".join(sections) if sections else ""


def _extract_theorem_stmt(formal_statement: str) -> str:
    """Extract just the theorem part from full formal statement (for RAG seed resolution)."""
    match = re.search(r'(theorem\s+\w+[\s\S]*?:=\s*(?:by\s+)?sorry)', formal_statement)
    return match.group(1) if match else formal_statement


def _build_hints_prompt(hints_text: str, formal_statement: str) -> str:
    """Build prompt: hints BEFORE code fence."""
    prefix = _HINTS_PREFIX.format(hints=hints_text)
    fence = _CODE_FENCE.format(formal_statement=formal_statement)
    return prefix + fence


def build_prompt(mode: Mode, formal_statement: str) -> str:
    """Build the LLM prompt for the given mode.

    Layout: [context/hints] + code fence (fence is always last).
    A1 (CoT) returns None here — handled separately in solve_task().
    """
    fence = _CODE_FENCE.format(formal_statement=formal_statement)

    # Extract theorem-only part for RAG seed resolution
    stmt = _extract_theorem_stmt(formal_statement)

    # ── A0, A0_R1: bare model ──
    if mode is Mode.A0 or mode is Mode.A0_R1:
        return fence

    # ── A1: CoT — handled in solve_task() (two-step) ──
    if mode is Mode.A1:
        return None  # sentinel: solve_task handles this

    # ── B1: vector RAG ──
    if mode is Mode.B1:
        _, _, semantic_hints = rag.start_hints(stmt)
        hints_text = "\n\n".join(semantic_hints)
        return _build_hints_prompt(hints_text, formal_statement)

    # ── C1: graph RAG baseline ──
    if mode is Mode.C1:
        res = rag.graph_hints(stmt, target_size=7)
        hints_text = "\n\n".join(res[:5])
        return _build_hints_prompt(hints_text, formal_statement)

    # ── C11: structure-aware graph RAG ──
    if mode is Mode.C11:
        hints = rag.graph_hints_v2(stmt)
        hints_text = _format_structured_sections(hints)
        return _build_hints_prompt(hints_text, formal_statement)

    # ── C2: graph + vector baseline ──
    if mode is Mode.C2:
        res = rag.graph_hints(stmt, target_size=7)
        graph_text = "\n\n".join(res[:3])
        _, _, semantic_hints = rag.start_hints(stmt)
        semantic_text = "\n\n".join(semantic_hints[:3])
        hints_text = f"{graph_text}\n\nAdditional similar lemmas:\n{semantic_text}"
        return _build_hints_prompt(hints_text, formal_statement)

    # ── C21: structure-aware graph + vector ──
    if mode is Mode.C21:
        hints = rag.graph_hints_v2(stmt)
        graph_text = _format_structured_sections(hints)
        _, _, semantic_hints = rag.start_hints(stmt)
        semantic_text = "\n\n".join(semantic_hints[:3])
        hints_text = f"{graph_text}\n\nSimilar Mathlib examples:\n{semantic_text}"
        return _build_hints_prompt(hints_text, formal_statement)

    # ── C22: graph tracing ──
    if mode is Mode.C22:
        hints = rag.graph_hints_c22(stmt)
        hints_text = _format_structured_sections(hints)
        return _build_hints_prompt(hints_text, formal_statement)

    # ── C23: graph + model re-ranking ──
    if mode is Mode.C23:
        hints = rag.graph_hints_c23(stmt)
        hints_text = _format_structured_sections(hints)
        return _build_hints_prompt(hints_text, formal_statement)

    # ── A1_B1 / A1_C11 / A1_C23 (and R1 variants): CoT + RAG — handled in solve_task() ──
    if mode in (Mode.A1_B1, Mode.A1_C11, Mode.A1_C23,
                Mode.A1_B1_R1, Mode.A1_C11_R1, Mode.A1_C23_R1):
        return None  # sentinel: solve_task handles these (two-step + RAG + optional retry)

    raise ValueError(f"Unknown mode: {mode}")


# ═════════════════════════════════════════════════════════════════════════════
# COT TWO-STEP GENERATION
# ═════════════════════════════════════════════════════════════════════════════

COT_MAX_REASONING_TOKENS = 2048
COT_REPETITION_PENALTY = 1.2


def _generate_cot_reasoning(formal_statement: str) -> str:
    """Step 1: generate proof reasoning/plan (limited tokens, high rep penalty)."""
    plan_prompt = _COT_PLAN_PROMPT.format(formal_statement=formal_statement)
    raw = model.generate(
        plan_prompt,
        max_new_tokens=COT_MAX_REASONING_TOKENS,
        repetition_penalty=COT_REPETITION_PENALTY,
    )
    # Clean: strip EOS, take text only (no fences)
    text = raw.split("<｜end▁of▁sentence｜>")[0].strip()
    # Remove any code fences from reasoning (we only want the NL plan)
    text = re.sub(r'```[\s\S]*?```', '', text).strip()
    # Truncate if still too long
    if len(text) > 3000:
        text = text[:3000] + "..."
    return text


def _build_cot_proof_prompt(formal_statement: str, reasoning: str) -> str:
    """Step 2: build proof prompt with reasoning as context BEFORE fence."""
    context = _COT_CONTEXT_PREFIX.format(reasoning=reasoning)
    fence = _CODE_FENCE.format(formal_statement=formal_statement)
    return context + fence


# ═════════════════════════════════════════════════════════════════════════════
# GENERATION + LEAN CHECK
# ═════════════════════════════════════════════════════════════════════════════

MAX_GEN_RETRIES = 8  # max retries when model returns empty/invalid output


def _is_empty_gen(code: str) -> bool:
    """Check if extracted code is empty/trivial."""
    stripped = code.strip()
    if not stripped:
        return True
    if len(stripped) < 3:
        return True
    return False


def _has_proof_content(code: str) -> bool:
    """Check that code contains actual Lean proof content, not just comments or NL text.

    Returns True if the output looks like real Lean code that Lean can meaningfully check.
    """
    stripped = code.strip()

    # Reject markdown-formatted NL text (model generated explanation instead of code)
    if re.match(r'\s*#{1,6}\s', stripped):
        return False
    if stripped.startswith('**') or stripped.startswith('The ') or stripped.startswith('We '):
        return False

    # Remove all Lean comments (-- single line and /- block -/)
    no_comments = re.sub(r'--[^\n]*', '', stripped)
    no_comments = re.sub(r'/\-[\s\S]*?\-/', '', no_comments)
    no_comments = no_comments.strip()

    # After removing comments, must have some code left
    if len(no_comments) < 5:
        return False

    # Must contain at least one Lean keyword
    lean_keywords = (
        r'\b(theorem|lemma|def|example|#check|by|sorry|simp|ring|omega|'
        r'nlinarith|linarith|norm_num|exact|apply|intro|have|let|calc|rfl|rw|'
        r'cases|induction|constructor|use|ext|funext|field_simp|push_neg|'
        r'contradiction|aesop|decide|trivial)\b'
    )
    if not re.search(lean_keywords, no_comments):
        return False

    return True


def generate_and_check(prompt: str) -> Dict:
    """Generate from prompt, extract Lean code, validate, check with Lean.

    Retries up to MAX_GEN_RETRIES times if model returns empty output or
    output without real Lean proof content (e.g. only comments or NL text).
    """
    raw_gen = ""
    lean_code = ""
    empty_count = 0
    no_proof_count = 0

    for attempt in range(1 + MAX_GEN_RETRIES):
        raw_gen = model.generate(prompt)
        lean_code = model.extract_lean_code(raw_gen)

        if _is_empty_gen(lean_code):
            empty_count += 1
            log.warning("Empty generation (attempt %d/%d), retrying...",
                        attempt + 1, 1 + MAX_GEN_RETRIES)
            continue

        if not _has_proof_content(lean_code):
            no_proof_count += 1
            log.warning("No proof content (attempt %d/%d), retrying... "
                        "snippet: %.80s", attempt + 1, 1 + MAX_GEN_RETRIES,
                        lean_code.replace('\n', ' ')[:80])
            continue

        break

    # All retries exhausted?
    if _is_empty_gen(lean_code):
        log.warning("Model returned empty output after %d attempts", 1 + MAX_GEN_RETRIES)
        return {
            "success": False,
            "error_class": "EMPTY_GENERATION",
            "error_message": f"Empty output after {1 + MAX_GEN_RETRIES} attempts",
            "time_ms": 0,
            "gen_text": raw_gen,
            "for_check": "",
            "prompt": prompt,
            "empty_retries": empty_count,
            "no_proof_retries": no_proof_count,
        }

    if not _has_proof_content(lean_code):
        log.warning("No proof content after %d attempts", 1 + MAX_GEN_RETRIES)
        return {
            "success": False,
            "error_class": "NO_PROOF_CONTENT",
            "error_message": f"No theorem/tactic after {1 + MAX_GEN_RETRIES} attempts. Last: {lean_code[:200]}",
            "time_ms": 0,
            "gen_text": raw_gen,
            "for_check": "",
            "prompt": prompt,
            "empty_retries": empty_count,
            "no_proof_retries": no_proof_count,
        }

    for_check = model.ensure_lean_imports(lean_code)

    checker = get_lean_checker()
    lean_result = checker.check(for_check, timeout=LEAN_CHECK_TIMEOUT)

    lean_result["empty_retries"] = empty_count
    lean_result["no_proof_retries"] = no_proof_count

    return {
        **lean_result,
        "gen_text": raw_gen,
        "for_check": for_check,
        "prompt": prompt,
    }


def _build_retry_prompt(formal_statement: str, for_check: str, error_message: str) -> str:
    """Build repair prompt from failed attempt + Lean error."""
    err_text = error_message[:500] if error_message else "unknown error"
    return (
        f"The following Lean 4 proof attempt failed:\n\n"
        f"```lean4\n{for_check}\n```\n\n"
        f"Lean error:\n{err_text}\n\n"
        f"Please fix the proof. Here is the original statement:\n\n"
        f"```lean4\n{formal_statement}\n```"
    )


def _build_retry_prompt_with_context(
    formal_statement: str, for_check: str, error_message: str,
    reasoning: str, hints_text: str,
) -> str:
    """Build repair prompt preserving CoT reasoning + RAG hints + Lean error."""
    err_text = error_message[:500] if error_message else "unknown error"
    context = _COT_CONTEXT_PREFIX.format(reasoning=reasoning)
    prefix = _HINTS_PREFIX.format(hints=hints_text)
    return (
        f"{context}"
        f"{prefix}"
        f"The following Lean 4 proof attempt failed:\n\n"
        f"```lean4\n{for_check}\n```\n\n"
        f"Lean error:\n{err_text}\n\n"
        f"Please fix the proof. Here is the original statement:\n\n"
        f"```lean4\n{formal_statement}\n```"
    )


def solve_task(mode: Mode, stmt: str) -> Dict:
    """High-level: build prompt -> generate -> check -> optional retry.

    Args:
        mode: experiment mode
        stmt: full formal statement (complete .lean file content)

    Returns dict with: success, error_class, error_message, time_ms,
    gen_text, for_check, prompt, mode, retries_used.
    """
    t0 = time.time()

    # ── A1: two-step CoT ──
    if mode is Mode.A1:
        log.info("A1 CoT step 1: generating reasoning...")
        reasoning = _generate_cot_reasoning(stmt)
        log.info("A1 CoT step 1 done (%d chars). Step 2: generating proof...",
                 len(reasoning))
        prompt = _build_cot_proof_prompt(stmt, reasoning)
        result = generate_and_check(prompt)
        result["mode"] = mode.name
        result["retries_used"] = 0
        result["cot_reasoning"] = reasoning
        result["wall_ms"] = int((time.time() - t0) * 1000)
        return result

    # ── A1_B1 / A1_C11 / A1_C23 (and R1 variants): CoT + RAG (+ optional retry) ──
    _COT_RAG_MODES = {
        Mode.A1_B1: Mode.A1_B1, Mode.A1_B1_R1: Mode.A1_B1,
        Mode.A1_C11: Mode.A1_C11, Mode.A1_C11_R1: Mode.A1_C11,
        Mode.A1_C23: Mode.A1_C23, Mode.A1_C23_R1: Mode.A1_C23,
    }
    if mode in _COT_RAG_MODES:
        base = _COT_RAG_MODES[mode]  # base mode for RAG strategy
        has_retry = mode in (Mode.A1_B1_R1, Mode.A1_C11_R1, Mode.A1_C23_R1)

        log.info("%s CoT step 1: generating reasoning...", mode.name)
        reasoning = _generate_cot_reasoning(stmt)
        log.info("%s CoT step 1 done (%d chars). Getting RAG hints...",
                 mode.name, len(reasoning))

        stmt_for_rag = _extract_theorem_stmt(stmt)
        if base is Mode.A1_B1:
            _, _, semantic_hints = rag.start_hints(stmt_for_rag)
            hints_text = "\n\n".join(semantic_hints)
        elif base is Mode.A1_C11:
            hints = rag.graph_hints_v2(stmt_for_rag)
            hints_text = _format_structured_sections(hints)
        else:  # A1_C23
            hints = rag.graph_hints_c23(stmt_for_rag)
            hints_text = _format_structured_sections(hints)

        # Layout: reasoning → hints → fence (all context BEFORE fence)
        context = _COT_CONTEXT_PREFIX.format(reasoning=reasoning)
        prefix = _HINTS_PREFIX.format(hints=hints_text)
        fence = _CODE_FENCE.format(formal_statement=stmt)
        prompt = context + prefix + fence

        log.info("%s step 2: generating proof (reasoning=%d, hints=%d)...",
                 mode.name, len(reasoning), len(hints_text))
        result = generate_and_check(prompt)
        result["mode"] = mode.name
        result["retries_used"] = 0
        result["cot_reasoning"] = reasoning

        # ── Retry with same reasoning + hints + Lean error ──
        if has_retry and not result["success"]:
            log.info("%s retry: feeding back Lean error...", mode.name)
            retry_prompt = _build_retry_prompt_with_context(
                stmt, result["for_check"], result.get("error_message", ""),
                reasoning, hints_text,
            )
            retry_result = generate_and_check(retry_prompt)
            if retry_result["success"]:
                result = retry_result
                result["mode"] = mode.name
                result["cot_reasoning"] = reasoning
            result["retries_used"] = 1

        result["wall_ms"] = int((time.time() - t0) * 1000)
        return result

    prompt = build_prompt(mode, stmt)
    result = generate_and_check(prompt)
    result["mode"] = mode.name
    result["retries_used"] = 0

    # ── Retry logic for A0_R1 ──
    if mode is Mode.A0_R1 and not result["success"]:
        retry_prompt = _build_retry_prompt(stmt, result["for_check"], result.get("error_message", ""))
        retry_result = generate_and_check(retry_prompt)
        if retry_result["success"]:
            result = retry_result
            result["mode"] = mode.name
        result["retries_used"] = 1

    result["wall_ms"] = int((time.time() - t0) * 1000)
    return result
