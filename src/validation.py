"""Validation helpers for non-regression checks."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List


def _stable_hash(rows: List[Dict[str, object]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compare_replay_outputs(reference: Dict[str, object], candidate: Dict[str, object]) -> Dict[str, object]:
    """Compare two replay output payloads and return numeric/hash deltas."""
    keys = [
        "final_balance",
        "final_equity",
        "total_return_percent",
        "max_drawdown_abs",
        "max_drawdown_percent",
        "cagr_percent",
        "total_deals",
    ]

    ref_summary = reference["summary"]
    cand_summary = candidate["summary"]

    return {
        "summary_delta": {
            k: float(cand_summary[k]) - float(ref_summary[k])
            for k in keys
        },
        "event_rows_hash_equal": _stable_hash(reference["event_rows"]) == _stable_hash(candidate["event_rows"]),
        "curve_rows_hash_equal": _stable_hash(reference["curve_rows"]) == _stable_hash(candidate["curve_rows"]),
    }
