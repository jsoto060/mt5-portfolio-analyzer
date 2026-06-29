"""Reporting tables and export helpers."""

from __future__ import annotations

import json
import os
from typing import Dict, List

import pandas as pd

from scenario import effective_config


def create_configuration_table(pairs_data) -> pd.DataFrame:
    """Create pair configuration table from inferred baseline and overrides."""
    rows: List[Dict[str, object]] = []
    for pair in sorted(pairs_data, key=lambda p: p.name):
        cfg = effective_config(pair)
        rows.append({
            "Pair": pair.name,
            "Risk": cfg["risk_percent"],
            "TP": cfg["take_profit"],
            "Grid": cfg["grid_size"],
            "Max Trades": cfg["max_trades"],
            "Trades": cfg["trade_count"],
            "Initial Balance": cfg["initial_balance"],
            "Median Lot": cfg["median_lot"],
        })
    return pd.DataFrame(rows)


def create_summary_table(metrics_bundle: Dict[str, object]) -> pd.DataFrame:
    """Create one-row summary table with key portfolio metrics."""
    row = {
        "Initial Balance": metrics_bundle.get("initial_balance"),
        "Final Balance": metrics_bundle.get("final_balance"),
        "Profit": metrics_bundle.get("profit"),
        "Average Monthly Growth": metrics_bundle.get("average_monthly_growth_percent"),
        "CAGR": metrics_bundle.get("cagr_percent"),
        "Max Floating Drawdown": metrics_bundle.get("max_floating_drawdown_abs"),
        "Peak Equity Drawdown": metrics_bundle.get("peak_equity_drawdown_abs"),
        "Minimum Margin Level": metrics_bundle.get("minimum_margin_level_percent"),
        "Minimum Free Margin": metrics_bundle.get("minimum_free_margin"),
        "Profit Factor": metrics_bundle.get("profit_factor"),
        "Recovery Factor": metrics_bundle.get("recovery_factor"),
        "Total Trades": metrics_bundle.get("total_trades"),
        "Winning Trades": metrics_bundle.get("winning_trades"),
        "Win Rate": metrics_bundle.get("win_rate_percent"),
    }
    return pd.DataFrame([row])


def create_pair_table(summary: Dict[str, object]) -> pd.DataFrame:
    """Create per-pair summary table from simulator summary payload."""
    rows: List[Dict[str, object]] = []
    for pair, info in sorted((summary.get("pairs") or {}).items()):
        rows.append({
            "Pair": pair,
            "Risk": info.get("risk_percent"),
            "Baseline Risk": info.get("baseline_risk_percent"),
            "Take Profit": info.get("baseline_take_profit"),
            "Grid": info.get("baseline_grid_size"),
            "Max Trades": info.get("baseline_max_trades"),
            "Trades": info.get("deals_count"),
            "Scaled PnL": info.get("scaled_pnl_contribution"),
        })
    return pd.DataFrame(rows)


def export_summary(output_dir: str, metrics_bundle: Dict[str, object], summary_df: pd.DataFrame, replay_df: pd.DataFrame) -> None:
    """Export summary.json, summary.csv and replay.csv."""
    os.makedirs(output_dir, exist_ok=True)

    with open(os.path.join(output_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics_bundle, fh, indent=2)

    summary_df.to_csv(os.path.join(output_dir, "summary.csv"), index=False)
    replay_df.to_csv(os.path.join(output_dir, "replay.csv"), index=False)
