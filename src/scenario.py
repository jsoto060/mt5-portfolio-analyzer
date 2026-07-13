"""Scenario override utilities for replay analysis."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Dict, List

from mt5_portfolio_analyzer import PairData, ScenarioConfig


OverrideMap = Dict[str, Dict[str, float]]


def _apply_max_trades_cap(pair: PairData, cap: int) -> PairData:
    """Return a copy of pair data with trades/deals filtered by max concurrent opens.

    The MT5 exports are historical, so we emulate a stricter max-trades setting by
    rejecting opening events once the cap is reached and discarding the matching
    closing/deal events for those rejected opens.
    """
    if cap <= 0:
        cap = 1

    open_buy_flags = deque()
    open_sell_flags = deque()
    open_other_flags = deque()
    accepted_open_count = 0
    accepted_sequence = set()

    def _bucket_for_open(side: str):
        s = (side or "").lower()
        if s.startswith("buy"):
            return open_buy_flags
        if s.startswith("sell"):
            return open_sell_flags
        return open_other_flags

    def _bucket_for_close(side: str):
        s = (side or "").lower()
        # MT5 semantics used in replay: out "sell" closes prior buys, out "buy" closes prior sells.
        if s.startswith("sell"):
            return open_buy_flags
        if s.startswith("buy"):
            return open_sell_flags
        return None

    def _pop_oldest_any():
        candidates = []
        if open_buy_flags:
            candidates.append(open_buy_flags[0])
        if open_sell_flags:
            candidates.append(open_sell_flags[0])
        if open_other_flags:
            candidates.append(open_other_flags[0])
        if not candidates:
            return None

        oldest = min(candidates, key=lambda x: x[0])
        if open_buy_flags and open_buy_flags[0] == oldest:
            return open_buy_flags.popleft()
        if open_sell_flags and open_sell_flags[0] == oldest:
            return open_sell_flags.popleft()
        return open_other_flags.popleft()

    for trade in pair.trades:
        if trade.direction == "in":
            accepted = accepted_open_count < cap
            _bucket_for_open(trade.side).append((int(trade.sequence_in_pair), accepted))
            if accepted:
                accepted_open_count += 1
                accepted_sequence.add(int(trade.sequence_in_pair))
        elif trade.direction == "out":
            close_bucket = _bucket_for_close(trade.side)
            if close_bucket is not None and close_bucket:
                token = close_bucket.popleft()
            else:
                token = _pop_oldest_any()

            accepted = token[1] if token is not None else False
            if accepted:
                accepted_open_count = max(0, accepted_open_count - 1)
                accepted_sequence.add(int(trade.sequence_in_pair))

    filtered_trades = [
        trade for trade in pair.trades
        if int(trade.sequence_in_pair) in accepted_sequence
    ]
    filtered_deals = [
        deal for deal in pair.deals
        if int(deal.sequence_in_pair) in accepted_sequence
    ]

    return replace(pair, trades=filtered_trades, deals=filtered_deals)


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

        capped_pair = pair
        if cfg.max_trades is not None and cfg.max_trades < pair.baseline_config.max_trades:
            capped_pair = _apply_max_trades_cap(pair, int(cfg.max_trades))

        out.append(replace(capped_pair, scenario_config=cfg))
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
