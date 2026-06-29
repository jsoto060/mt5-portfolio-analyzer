"""Scenario override utilities for replay analysis."""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List

from mt5_portfolio_analyzer import PairData, ScenarioConfig


OverrideMap = Dict[str, Dict[str, float]]


def apply_scenario_overrides(pairs_data: List[PairData], overrides: OverrideMap) -> List[PairData]:
    """Apply per-pair scenario overrides while preserving inferred baseline config.

    Only explicitly provided keys are overridden.
    """
    out: List[PairData] = []
    for pair in pairs_data:
        patch = overrides.get(pair.name, {})
        if not patch:
            out.append(pair)
            continue

        cfg = ScenarioConfig(
            risk_percent=patch.get("risk_percent"),
            take_profit=int(patch["take_profit"]) if patch.get("take_profit") is not None else None,
            grid_size=int(patch["grid_size"]) if patch.get("grid_size") is not None else None,
            max_trades=int(patch["max_trades"]) if patch.get("max_trades") is not None else None,
        )
        out.append(replace(pair, scenario_config=cfg))
    return out


def effective_config(pair: PairData) -> Dict[str, float]:
    """Return effective pair configuration (baseline + scenario overrides)."""
    base = pair.baseline_config
    scen = pair.scenario_config

    return {
        "risk_percent": float(scen.risk_percent) if scen and scen.risk_percent is not None else float(base.risk_percent),
        "take_profit": int(scen.take_profit) if scen and scen.take_profit is not None else base.take_profit,
        "grid_size": int(scen.grid_size) if scen and scen.grid_size is not None else base.grid_size,
        "max_trades": int(scen.max_trades) if scen and scen.max_trades is not None else int(base.max_trades),
        "initial_balance": float(base.initial_balance),
        "first_lot": base.first_lot,
        "median_lot": base.median_lot,
        "trade_count": int(base.trade_count),
    }
