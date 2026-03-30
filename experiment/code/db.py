"""Database logging for experiment results."""

import json
import logging
from typing import Dict, Any, Set, Tuple

import psycopg2
from psycopg2.extras import Json

from .config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD, Mode

log = logging.getLogger("db")


def load_done_keys(exp_id: int) -> Set[Tuple[str, str, str]]:
    """Load already-completed (task_name, mode, variant) tuples for resume."""
    done: Set[Tuple[str, str, str]] = set()
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, database=PG_DB,
            user=PG_USER, password=PG_PASSWORD,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT object_name, mode, variant FROM minif2f_result WHERE experiment_id = %s",
            (exp_id,),
        )
        for obj, mode_str, variant in cur.fetchall():
            if obj and mode_str and variant:
                done.add((str(obj), str(mode_str), str(variant)))
        cur.close()
        conn.close()
        log.info("[resume] loaded %d done rows for exp_id=%d", len(done), exp_id)
    except Exception as e:
        log.warning("[resume] cannot load done rows: %s", e)
    return done


def save_result(
    exp_id: int,
    mode: Mode,
    task_name: str,
    task_content: str,
    result: Dict[str, Any],
    run_idx: int,
    model_name: str = "DeepSeek-Prover-V2-7B",
    data_part: str = "Test",
    data_source: str = "miniF2F-lean4",
):
    """Save one experiment result to minif2f_result table."""
    check_passed = bool(result.get("success", False))
    error_class = None if check_passed else result.get("error_class")
    error_message = json.dumps(result, ensure_ascii=False, default=str)[:5000]
    final_proof = result.get("for_check", "")
    time_ms = result.get("wall_ms", 0)
    prompt = result.get("prompt", "")
    gen_text = result.get("gen_text", "")
    for_check = result.get("for_check", "")

    conn = None
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, database=PG_DB,
            user=PG_USER, password=PG_PASSWORD,
        )
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO minif2f_result (
                experiment_id, data_source, data_part, model,
                object_name, object_content, mode, variant,
                max_attempts, time_ms,
                check_passed, error_class, error_message, final_proof,
                raw_result
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s
            )""",
            (
                exp_id, data_source, data_part, model_name,
                task_name, task_content, mode.name, f"pass4_run_{run_idx}",
                0, time_ms,
                check_passed, error_class, error_message, final_proof,
                Json({"prompt": prompt, "gen_text": gen_text,
                      "for_check": for_check, "res": result}),
            ),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        log.error("[DB ERROR] %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
