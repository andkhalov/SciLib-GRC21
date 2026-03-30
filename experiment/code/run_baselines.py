#!/usr/bin/env python3
"""Experiment 141 — External Baselines Runner.

Runs three external premise retrieval baselines (LeanSearch, LeanExplore, LeanFinder)
on MiniF2F Test with the same model and conditions as experiment 140.

Usage:
    python baselines_paper/run_baselines.py --exp_id 141 --pass_k 8
    python baselines_paper/run_baselines.py --exp_id 141 --smoke
    python baselines_paper/run_baselines.py --exp_id 141 --modes BL_LS BL_LE BL_LF

Resume: re-running with same exp_id skips already-completed (task, mode, variant) tuples.
"""

import argparse
import logging
import random
import sys
import os
import time

# Add parent directory to path so we can import modules/
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
log = logging.getLogger("baselines")

# Baseline mode names
BASELINE_MODES = list(RETRIEVERS.keys())  # ['BL_LS', 'BL_LE', 'BL_LF']

# Prompt templates — identical to solver.py mode B1
_HINTS_PREFIX = "You may find the following Mathlib lemmas useful:\n{hints}\n\n"
_CODE_FENCE = "Complete the following Lean 4 code:\n\n```lean4\n{formal_statement}\n```"


def build_baseline_prompt(hints: list, formal_statement: str) -> str:
    """Build prompt with hints BEFORE code fence (same layout as B1)."""
    hints_text = "\n".join(hints) if hints else "(no hints available)"
    prefix = _HINTS_PREFIX.format(hints=hints_text)
    fence = _CODE_FENCE.format(formal_statement=formal_statement)
    return prefix + fence


def parse_args():
    p = argparse.ArgumentParser(description="Experiment 141 — External Baselines")
    p.add_argument("--exp_id", type=int, default=141, help="Experiment ID (default: 141)")
    p.add_argument("--pass_k", type=int, default=PASS_K, help="Number of passes per (task, mode)")
    p.add_argument("--max_tasks", type=int, default=None, help="Limit number of tasks")
    p.add_argument("--modes", nargs="+", default=None,
                   help=f"Baseline modes (default: all). Available: {BASELINE_MODES}")
    p.add_argument("--split", default="Test", help="Dataset split: Test or Valid")
    p.add_argument("--no_resume", action="store_true", help="Do not skip already-done runs")
    p.add_argument("--smoke", action="store_true",
                   help="Smoke test: 3 tasks, pass_k=1, all baselines")
    p.add_argument("--hints_k", type=int, default=10,
                   help="Number of hints to retrieve from each API (default: 10)")
    return p.parse_args()


class BaselineMode:
    """Lightweight Mode-like object for baselines (compatible with db.save_result)."""
    def __init__(self, name: str):
        self.name = name
        self.value = name


def run(args):
    # Resolve modes
    if args.modes:
        modes = []
        for name in args.modes:
            if name not in RETRIEVERS:
                log.error("Unknown baseline mode: %s. Available: %s", name, BASELINE_MODES)
                sys.exit(1)
            modes.append(name)
    else:
        modes = BASELINE_MODES

    # Task list
    if args.smoke:
        task_names = get_all_tasks(split=args.split, exclude_hard=False)
        random.seed(RANDOM_SEED)
        random.shuffle(task_names)
        task_names = task_names[:3]
        args.pass_k = 1
        log.info("SMOKE TEST: %d tasks, pass_k=1, modes=%s", len(task_names), modes)
    else:
        task_names = get_all_tasks(split=args.split, exclude_hard=False)
        random.seed(RANDOM_SEED)
        random.shuffle(task_names)
        if args.max_tasks:
            task_names = task_names[:args.max_tasks]

    log.info("Tasks: %d, Modes: %d (%s), Pass_K: %d, Exp_ID: %d",
             len(task_names), len(modes), modes, args.pass_k, args.exp_id)

    # Init model
    log.info("Initializing model...")
    model_module.init()
    log.info("Model ready.")

    # Init Lean checker
    checker = solver.get_lean_checker()
    log.info("Lean checker connected.")

    # Load resume state
    done = set()
    if not args.no_resume:
        done = db.load_done_keys(args.exp_id)

    total_logged = 0
    total_skipped = 0
    total_success = 0

    for ti, task_name in enumerate(task_names):
        task_id, stmt = load_task(task_name, split=args.split)

        for mode_name in modes:
            for run_idx in range(args.pass_k):
                variant = f"pass4_run_{run_idx}"

                if (task_name, mode_name, variant) in done:
                    total_skipped += 1
                    continue

                t0 = time.time()

                try:
                    # Step 1: Retrieve hints from external API
                    hints = retrieve(mode_name, stmt, k=args.hints_k)

                    # Step 2: Build prompt (same layout as B1)
                    prompt = build_baseline_prompt(hints, stmt)

                    # Step 3: Generate and check (reuse solver infrastructure)
                    result = solver.generate_and_check(prompt)
                    result["mode"] = mode_name
                    result["retries_used"] = 0
                    result["hints_count"] = len(hints)
                    result["hints_source"] = mode_name

                except Exception as e:
                    log.error("[%s][%s][%d] ERROR: %s", task_name, mode_name, run_idx, e)
                    result = {
                        "success": False, "error_class": "SOLVER_ERROR",
                        "error_message": str(e)[:500], "wall_ms": 0,
                        "gen_text": "", "for_check": "", "prompt": "",
                        "mode": mode_name,
                    }

                result["wall_ms"] = int((time.time() - t0) * 1000)

                # Step 4: Save to DB
                mode_obj = BaselineMode(mode_name)
                db.save_result(
                    exp_id=args.exp_id, mode=mode_obj,
                    task_name=task_name, task_content=stmt,
                    result=result, run_idx=run_idx,
                    data_source="miniF2F-lean4",
                    data_part=args.split,
                )
                done.add((task_name, mode_name, variant))
                total_logged += 1

                ok = result.get("success", False)
                if ok:
                    total_success += 1
                wall = result.get("wall_ms", 0)
                log.info("[%d/%d][%s][%s][%d/%d] ok=%s wall=%dms hints=%d",
                         ti + 1, len(task_names), task_name, mode_name,
                         run_idx + 1, args.pass_k, ok, wall,
                         result.get("hints_count", 0))

    log.info("DONE. logged=%d skipped=%d success=%d", total_logged, total_skipped, total_success)


if __name__ == "__main__":
    run(parse_args())
