#!/usr/bin/env python3
"""MiniF2F Experiment Runner — pass@K across all tasks × all modes.

Usage:
    python run_experiment.py --exp_id 90 --pass_k 4 --modes A0 C11 C22 C23 A0_R1
    python run_experiment.py --exp_id 90 --max_tasks 10 --smoke  # smoke test
    python run_experiment.py --exp_id 90                         # full run, all modes

Resume: re-running with same exp_id skips already-completed (task, mode, variant) tuples.
"""

import argparse
import logging
import random
import sys
import time

from modules.config import Mode, RANDOM_SEED, PASS_K
from modules.loader import load_task, get_all_tasks
from modules import model as model_module
from modules import solver
from modules import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("experiment")

ALL_MODES = [
    Mode.A0, Mode.A1, Mode.B1,
    Mode.C1, Mode.C11, Mode.C2, Mode.C21,
    Mode.C22, Mode.C23, Mode.A0_R1,
    Mode.A1_B1, Mode.A1_C11, Mode.A1_C23,
    Mode.A1_B1_R1, Mode.A1_C11_R1, Mode.A1_C23_R1,
]


def parse_args():
    p = argparse.ArgumentParser(description="MiniF2F Experiment Runner")
    p.add_argument("--exp_id", type=int, default=90, help="Experiment ID (for DB logging)")
    p.add_argument("--pass_k", type=int, default=PASS_K, help="Number of passes per (task, mode)")
    p.add_argument("--max_tasks", type=int, default=None, help="Limit number of tasks")
    p.add_argument("--modes", nargs="+", default=None, help="Mode names (e.g. A0 C11 C22)")
    p.add_argument("--split", default="Test", help="Dataset split: Test or Valid")
    p.add_argument("--benchmark", default="minif2f", choices=["minif2f", "putnam"],
                   help="Benchmark: minif2f or putnam")
    p.add_argument("--no_resume", action="store_true", help="Do not skip already-done runs")
    p.add_argument("--smoke", action="store_true", help="Smoke test: 10 diverse tasks, pass_k=1")
    return p.parse_args()


def get_smoke_tasks(split: str = "Test") -> list:
    """Pick 10 diverse tasks covering different categories."""
    all_tasks = get_all_tasks(split=split, exclude_hard=False)
    buckets = {
        "mathd_algebra": [], "mathd_numbertheory": [],
        "amc12": [], "aime": [], "imo": [],
        "algebra": [], "numbertheory": [], "induction": [],
        "other": [],
    }
    for t in all_tasks:
        placed = False
        for key in buckets:
            if key != "other" and key in t:
                buckets[key].append(t)
                placed = True
                break
        if not placed:
            buckets["other"].append(t)

    random.seed(RANDOM_SEED)
    picks = []
    for key, tasks in buckets.items():
        if tasks:
            random.shuffle(tasks)
            picks.extend(tasks[:2])
    random.shuffle(picks)
    return picks[:10]


def run(args):
    # Resolve modes
    if args.modes:
        modes = []
        for name in args.modes:
            try:
                modes.append(Mode[name])
            except KeyError:
                log.error("Unknown mode: %s. Available: %s", name,
                          ", ".join(m.name for m in Mode))
                sys.exit(1)
    else:
        modes = ALL_MODES

    # Smoke test overrides
    if args.smoke:
        task_names = get_smoke_tasks(args.split)
        args.pass_k = 1
        log.info("SMOKE TEST: %d tasks, pass_k=1, modes=%s",
                 len(task_names), [m.name for m in modes])
    else:
        task_names = get_all_tasks(split=args.split, exclude_hard=False,
                                   benchmark=args.benchmark)
        random.seed(RANDOM_SEED)
        random.shuffle(task_names)
        if args.max_tasks:
            task_names = task_names[:args.max_tasks]

    log.info("Tasks: %d, Modes: %d, Pass_K: %d, Exp_ID: %d",
             len(task_names), len(modes), args.pass_k, args.exp_id)

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
        task_id, stmt = load_task(task_name, split=args.split,
                                   benchmark=args.benchmark)

        for mode in modes:
            for run_idx in range(args.pass_k):
                variant = f"pass4_run_{run_idx}"

                if (task_name, mode.name, variant) in done:
                    total_skipped += 1
                    continue

                try:
                    result = solver.solve_task(mode, stmt)
                except Exception as e:
                    log.error("[%s][%s][%d] ERROR: %s", task_name, mode.name, run_idx, e)
                    result = {
                        "success": False, "error_class": "SOLVER_ERROR",
                        "error_message": str(e)[:500], "wall_ms": 0,
                        "gen_text": "", "for_check": "", "prompt": "",
                    }

                db.save_result(
                    exp_id=args.exp_id, mode=mode,
                    task_name=task_name, task_content=stmt,
                    result=result, run_idx=run_idx,
                    data_source="PutnamBench" if args.benchmark == "putnam" else "miniF2F-lean4",
                    data_part="all" if args.benchmark == "putnam" else args.split,
                )
                done.add((task_name, mode.name, variant))
                total_logged += 1

                ok = result.get("success", False)
                if ok:
                    total_success += 1
                wall = result.get("wall_ms", 0)
                log.info("[%d/%d][%s][%s][%d/%d] ok=%s wall=%dms",
                         ti + 1, len(task_names), task_name, mode.name,
                         run_idx + 1, args.pass_k, ok, wall)

    log.info("DONE. logged=%d skipped=%d success=%d", total_logged, total_skipped, total_success)


if __name__ == "__main__":
    run(parse_args())
