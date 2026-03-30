#!/usr/bin/env python3
"""Experiment 142 — Combined runner: SciLib modes + external baselines.

Runs both internal SciLib RAG modes (A0, B1, C21, C23) and external baselines
(BL_LS, BL_LF) in a single pass with identical conditions.

Usage:
    python baselines_paper/run_combined.py --exp_id 142 --split Valid --pass_k 8
    python baselines_paper/run_combined.py --exp_id 142 --split Valid --smoke
    python baselines_paper/run_combined.py --exp_id 142 --split Valid --modes A0 C21 BL_LS
"""

import argparse
import logging
import random
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.config import RANDOM_SEED, PASS_K, Mode
from modules.loader import load_task, get_all_tasks
from modules import model as model_module
from modules import solver
from modules import db

from retrieval import retrieve, RETRIEVERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("combined")

# ── Mode registry ──

# SciLib internal modes handled by solver.py
SCILIB_MODES = {
    'A0': Mode.A0,
    'B1': Mode.B1,
    'C21': Mode.C21,
    'C23': Mode.C23,
}

# External baselines handled by retrieval.py
BASELINE_MODES = list(RETRIEVERS.keys())  # ['BL_LS', 'BL_LE', 'BL_LF']

ALL_MODES = list(SCILIB_MODES.keys()) + BASELINE_MODES

# Prompt templates (same as solver.py)
_HINTS_PREFIX = "You may find the following Mathlib lemmas useful:\n{hints}\n\n"
_CODE_FENCE = "Complete the following Lean 4 code:\n\n```lean4\n{formal_statement}\n```"


class BaselineMode:
    """Lightweight Mode-like object for baselines (compatible with db.save_result)."""
    def __init__(self, name: str):
        self.name = name
        self.value = name


def build_baseline_prompt(hints: list, formal_statement: str) -> str:
    hints_text = "\n".join(hints) if hints else "(no hints available)"
    prefix = _HINTS_PREFIX.format(hints=hints_text)
    fence = _CODE_FENCE.format(formal_statement=formal_statement)
    return prefix + fence


def parse_args():
    p = argparse.ArgumentParser(description="Experiment 142 — Combined SciLib + Baselines")
    p.add_argument("--exp_id", type=int, default=142)
    p.add_argument("--pass_k", type=int, default=PASS_K)
    p.add_argument("--max_tasks", type=int, default=None)
    p.add_argument("--modes", nargs="+", default=None,
                   help=f"Modes to run (default: all). Available: {ALL_MODES}")
    p.add_argument("--split", default="Valid", help="Dataset split: Test or Valid")
    p.add_argument("--benchmark", default="minif2f", choices=["minif2f", "putnam"])
    p.add_argument("--no_resume", action="store_true")
    p.add_argument("--smoke", action="store_true",
                   help="Smoke test: 2 tasks, pass_k=1, all modes")
    p.add_argument("--hints_k", type=int, default=10,
                   help="Hints for baselines (default: 10)")
    return p.parse_args()


def run_one(mode_name: str, stmt: str, hints_k: int) -> dict:
    """Run a single (mode, statement) pair. Returns result dict."""
    t0 = time.time()

    if mode_name in SCILIB_MODES:
        # Internal SciLib mode — use solver.solve_task()
        mode_enum = SCILIB_MODES[mode_name]
        result = solver.solve_task(mode_enum, stmt)
    else:
        # External baseline — retrieve hints + generate + check
        hints = retrieve(mode_name, stmt, k=hints_k)
        prompt = build_baseline_prompt(hints, stmt)
        result = solver.generate_and_check(prompt)
        result["hints_count"] = len(hints)
        result["hints_source"] = mode_name

    result["mode"] = mode_name
    result["wall_ms"] = int((time.time() - t0) * 1000)
    return result


def run(args):
    # Resolve modes
    if args.modes:
        modes = []
        for name in args.modes:
            if name not in ALL_MODES:
                log.error("Unknown mode: %s. Available: %s", name, ALL_MODES)
                sys.exit(1)
            modes.append(name)
    else:
        modes = ALL_MODES

    # Filter out BL_LE if no API key
    if 'BL_LE' in modes:
        from retrieval import LEANEXPLORE_API_KEY
        if not LEANEXPLORE_API_KEY:
            log.warning("BL_LE skipped — LEANEXPLORE_API_KEY not set")
            modes.remove('BL_LE')

    # Task list
    if args.smoke:
        task_names = get_all_tasks(split=args.split, exclude_hard=False,
                                    benchmark=args.benchmark)
        random.seed(RANDOM_SEED)
        random.shuffle(task_names)
        task_names = task_names[:2]
        args.pass_k = 1
        log.info("SMOKE TEST: %d tasks, pass_k=1, modes=%s", len(task_names), modes)
    else:
        task_names = get_all_tasks(split=args.split, exclude_hard=False,
                                    benchmark=args.benchmark)
        random.seed(RANDOM_SEED)
        random.shuffle(task_names)
        if args.max_tasks:
            task_names = task_names[:args.max_tasks]

    log.info("Tasks: %d, Modes: %d (%s), Pass_K: %d, Exp_ID: %d, Split: %s",
             len(task_names), len(modes), modes, args.pass_k, args.exp_id, args.split)

    # Init
    log.info("Initializing model...")
    model_module.init()
    log.info("Model ready.")

    checker = solver.get_lean_checker()
    log.info("Lean checker connected.")

    done = set()
    if not args.no_resume:
        done = db.load_done_keys(args.exp_id)

    total_logged = 0
    total_skipped = 0
    total_success = 0

    for ti, task_name in enumerate(task_names):
        task_id, stmt = load_task(task_name, split=args.split,
                                   benchmark=args.benchmark)

        for mode_name in modes:
            for run_idx in range(args.pass_k):
                variant = f"pass4_run_{run_idx}"

                if (task_name, mode_name, variant) in done:
                    total_skipped += 1
                    continue

                try:
                    result = run_one(mode_name, stmt, args.hints_k)
                except Exception as e:
                    log.error("[%s][%s][%d] ERROR: %s", task_name, mode_name, run_idx, e)
                    result = {
                        "success": False, "error_class": "SOLVER_ERROR",
                        "error_message": str(e)[:500], "wall_ms": 0,
                        "gen_text": "", "for_check": "", "prompt": "",
                        "mode": mode_name,
                    }

                # DB save — use Mode enum if SciLib, BaselineMode if external
                mode_obj = SCILIB_MODES.get(mode_name) or BaselineMode(mode_name)
                db.save_result(
                    exp_id=args.exp_id, mode=mode_obj,
                    task_name=task_name, task_content=stmt,
                    result=result, run_idx=run_idx,
                    data_source="PutnamBench" if args.benchmark == "putnam" else "miniF2F-lean4",
                    data_part="all" if args.benchmark == "putnam" else args.split,
                )
                done.add((task_name, mode_name, variant))
                total_logged += 1

                ok = result.get("success", False)
                if ok:
                    total_success += 1
                wall = result.get("wall_ms", 0)
                log.info("[%d/%d][%s][%s][%d/%d] ok=%s wall=%dms",
                         ti + 1, len(task_names), task_name, mode_name,
                         run_idx + 1, args.pass_k, ok, wall)

    log.info("DONE. logged=%d skipped=%d success=%d", total_logged, total_skipped, total_success)


if __name__ == "__main__":
    run(parse_args())
