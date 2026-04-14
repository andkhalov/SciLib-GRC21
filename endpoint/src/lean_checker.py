"""Lean 4 proof checker via Kafka.

Sends Lean code to lean-service for verification and returns the result.
Includes sanity checks to reject trivially valid but useless submissions
(sorry, bare imports, comment-only code).
"""

import re
import json
import time
import uuid
import logging
from typing import Dict, Optional

from kafka import KafkaProducer, KafkaConsumer, TopicPartition

import config as cfg

log = logging.getLogger("lean_checker")

# ── Kafka topics ──

LPC_INPUT_TOPIC = "scilib.commands.lean.check.run.v1"
LPC_OUTPUT_TOPIC = "scilib.events.lean.check.completed.v1"

# ── Sanity check ──

_LEAN_KEYWORDS = re.compile(
    r'\b(theorem|lemma|def|example|#check|by|simp|ring|omega|'
    r'nlinarith|linarith|norm_num|exact|apply|intro|have|let|calc|rfl|rw|'
    r'cases|induction|constructor|use|ext|funext|field_simp|push_neg|'
    r'contradiction|aesop|decide|trivial)\b'
)


def has_proof_content(code: str) -> tuple:
    """Check that code contains actual Lean proof content.

    Returns (is_valid, reason) tuple.
    Rejects: sorry-only, comment-only, NL text, bare imports.
    """
    stripped = code.strip()

    if not stripped:
        return False, "Empty code"

    # Reject sorry — it's a placeholder, not a proof
    # Remove comments first, then check
    no_comments = re.sub(r'--[^\n]*', '', stripped)
    no_comments = re.sub(r'/\-[\s\S]*?\-/', '', no_comments)

    # Check if sorry is the only tactic
    lines_no_imports = []
    for line in no_comments.split('\n'):
        s = line.strip()
        if s and not s.startswith(('import ', 'open ', 'set_option ', 'namespace ', 'end ', 'section ', 'variable ')):
            lines_no_imports.append(s)

    body = '\n'.join(lines_no_imports)

    # Check for sorry
    if 'sorry' in body:
        # Is sorry the ONLY tactic?
        body_no_decl = re.sub(r'(theorem|lemma|def|example)\s+[\s\S]*?:=\s*', '', body)
        body_tactics = body_no_decl.strip()
        if body_tactics == 'sorry' or body_tactics == 'by sorry' or body_tactics.endswith(':= by sorry') or re.fullmatch(r'by\s+sorry', body_tactics):
            return False, "Proof contains only 'sorry' — not a real proof"

    # Reject markdown / NL text
    if re.match(r'\s*#{1,6}\s', stripped):
        return False, "Output is markdown text, not Lean code"
    if stripped.startswith('**') or stripped.startswith('The ') or stripped.startswith('We '):
        return False, "Output is natural language text, not Lean code"

    # After removing comments, must have meaningful code
    no_comments_stripped = no_comments.strip()
    if len(no_comments_stripped) < 5:
        return False, "Code too short after removing comments"

    # Must contain at least one Lean keyword
    if not _LEAN_KEYWORDS.search(no_comments_stripped):
        return False, "No Lean keywords found — not valid Lean code"

    # Reject bare import (import Mathlib + nothing else)
    non_import_lines = [l for l in no_comments.split('\n')
                        if l.strip() and not l.strip().startswith(('import ', 'open ', 'set_option '))]
    if not non_import_lines:
        return False, "Only import/open/set_option statements — no actual proof"

    return True, "OK"


# ── Kafka checker ──

class LeanChecker:
    def __init__(self):
        self.producer = None
        self.consumer = None
        self.connected = False

    def connect(self):
        if self.connected:
            return

        self.producer = KafkaProducer(
            bootstrap_servers=cfg.KAFKA_BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            acks='all',
            max_block_ms=5000,
        )

        self.consumer = KafkaConsumer(
            bootstrap_servers=cfg.KAFKA_BOOTSTRAP,
            value_deserializer=lambda m: json.loads(m.decode('utf-8')),
            auto_offset_reset='latest',
        )

        tp = TopicPartition(LPC_OUTPUT_TOPIC, 0)
        self.consumer.assign([tp])
        self.consumer.seek_to_end(tp)
        self.consumer.poll(timeout_ms=500)
        self.connected = True
        log.info("Lean checker connected to Kafka")

    def close(self):
        try:
            if self.producer:
                self.producer.close(timeout=5)
        except Exception:
            pass
        try:
            if self.consumer:
                self.consumer.close()
        except Exception:
            pass
        self.producer = None
        self.consumer = None
        self.connected = False

    def check(self, lean_code: str, timeout: int = 30) -> Dict:
        """Send code to Lean service and wait for result."""
        if not self.connected:
            self.connect()

        run_id = str(uuid.uuid4())
        tp = TopicPartition(LPC_OUTPUT_TOPIC, 0)
        self.consumer.seek_to_end(tp)

        self.producer.send(LPC_INPUT_TOPIC, {
            'event_id': str(uuid.uuid4()),
            'event_type': 'lean.check.run',
            'run_id': run_id,
            'payload': {
                'task_id': 'api_check',
                'lean_code': lean_code,
            },
        })
        self.producer.flush()

        deadline = time.time() + timeout
        while time.time() < deadline:
            msg_pack = self.consumer.poll(timeout_ms=1000)
            for tp_key, msgs in msg_pack.items():
                for msg in msgs:
                    if msg.value.get('run_id') == run_id:
                        payload = msg.value.get('payload') or msg.value.get('result') or {}
                        status = payload.get('status', '')
                        success = status == 'SUCCESS' or payload.get('success', False)

                        error = payload.get('error') or {}
                        if isinstance(error, str):
                            error_class = 'ERROR'
                            error_message = error
                        else:
                            error_class = (error.get('class')
                                           or payload.get('error_class')
                                           or (None if success else status))
                            error_message = (error.get('message')
                                             or payload.get('error_message')
                                             or '')

                        return {
                            'success': success,
                            'error_class': error_class,
                            'error_message': str(error_message)[:2000] if error_message else '',
                            'time_ms': payload.get('time_ms', 0),
                        }

        return {
            'success': False,
            'error_class': 'TIMEOUT',
            'error_message': f'No response after {timeout}s',
            'time_ms': timeout * 1000,
        }


# Singleton
_checker = None


def get_checker() -> LeanChecker:
    global _checker
    if _checker is None:
        _checker = LeanChecker()
        _checker.connect()
    return _checker
