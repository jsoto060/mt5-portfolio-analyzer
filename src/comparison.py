"""Comparison helpers for baseline vs scenario/proposed replay runs."""

from __future__ import annotations

from typing import Dict

import pandas as pd


def _metric_table(baseline_metrics: Dict[str, object], proposed_metrics: Dict[str, object], mapping):
    rows = []
    for label, key in mapping:
        b = baseline_metrics.get(key)
        p = proposed_metrics.get(key)
        delta = None
        if isinstance(b, (int, float)) and isinstance(p, (int, float)):
            delta = p - b
        rows.append({"Metric": label, "Baseline": b, "Proposed": p, "Delta": delta})
    return pd.DataFrame(rows)


def compare_results(baseline_result, proposed_result) -> Dict[str, pd.DataFrame]:
    """Return comparison tables for configuration, performance, drawdown, margin and pair stats."""
    config = baseline_result.configuration().merge(
        proposed_result.configuration(),
        on="Pair",
        suffixes=(" (Baseline)", " (Proposed)"),
        how="outer",
    )

    performance = _metric_table(
        baseline_result.metrics,
        proposed_result.metrics,
        [
            ("Initial Balance", "initial_balance"),
            ("Final Balance", "final_balance"),
            ("Profit", "profit"),
            ("Average Monthly Growth", "average_monthly_growth_percent"),
            ("CAGR", "cagr_percent"),
            ("Profit Factor", "profit_factor"),
            ("Recovery Factor", "recovery_factor"),
            ("Total Trades", "total_trades"),
            ("Winning Trades", "winning_trades"),
            ("Win Rate", "win_rate_percent"),
        ],
    )

    drawdown = _metric_table(
        baseline_result.metrics,
        proposed_result.metrics,
        [
            ("Max Floating Drawdown", "max_floating_drawdown_abs"),
            ("Max Floating Drawdown %", "max_floating_drawdown_percent"),
            ("Peak Equity Drawdown", "peak_equity_drawdown_abs"),
            ("Peak Equity Drawdown %", "peak_equity_drawdown_percent"),
        ],
    )

    margin = _metric_table(
        baseline_result.metrics,
        proposed_result.metrics,
        [
            ("Minimum Margin Level", "minimum_margin_level_percent"),
            ("Minimum Free Margin", "minimum_free_margin"),
            ("Maximum Used Margin", "maximum_used_margin"),
        ],
    )

    per_pair = baseline_result.pair_table().merge(
        proposed_result.pair_table(),
        on="Pair",
        suffixes=(" (Baseline)", " (Proposed)"),
        how="outer",
    )

    return {
        "configuration": config,
        "performance": performance,
        "drawdown": drawdown,
        "margin": margin,
        "per_pair": per_pair,
    }
