"""
MT5 Portfolio Analyzer
======================
Reconstructs a combined portfolio from separate MT5 Strategy Tester backtests.

Input files (per pair):
  - ReportTester-*.xlsx   -- full tester report; deals table extracted automatically.
  - testergraph.report.*.csv -- UTF-16 tab-delimited balance/equity curve.

Dynamic lot scaling:
    new_lot = round(combined_balance * pair_risk_percent / 100000, 2)

Per-pair PnL is multiplied by:
    scale_factor = new_lot / original_lot

Usage:
  python src/mt5_portfolio_analyzer.py --auto --data-dir data --output-dir output
  python src/mt5_portfolio_analyzer.py --config config/example_config.json --output-dir output
"""

import argparse
import bisect
import csv
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional, Protocol, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from mt5_readers import discover_files, load_graph_csv, load_xlsx_deals


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal domain types
# ---------------------------------------------------------------------------


@dataclass
class DealEvent:
    time: datetime
    pair: str
    net_profit: float
    volume: float


@dataclass
class TradeEvent:
    time: datetime
    pair: str
    direction: str
    volume: float
    price: float
    side: str = ""


@dataclass
class CurvePoint:
    time: datetime
    balance: float
    equity: float


@dataclass(frozen=True)
class BaselineConfig:
    risk_percent: float
    take_profit: Optional[int]
    grid_size: Optional[int]
    max_trades: int
    initial_balance: float
    first_lot: Optional[float]
    median_lot: Optional[float]
    trade_count: int


@dataclass(frozen=True)
class ScenarioConfig:
    risk_percent: Optional[float] = None
    take_profit: Optional[int] = None
    grid_size: Optional[int] = None
    max_trades: Optional[int] = None


@dataclass
class PairData:
    name: str
    baseline_config: BaselineConfig
    deals: List[DealEvent]
    trades: List[TradeEvent]
    curve: List[CurvePoint]
    baseline_volume_median: Optional[float]
    scenario_config: Optional[ScenarioConfig] = None
    curve_times: List[datetime] = field(default_factory=list)
    curve_floating: List[float] = field(default_factory=list)
    market_times: List[datetime] = field(default_factory=list)
    market_close: List[float] = field(default_factory=list)

    def effective_risk_percent(self) -> float:
        if self.scenario_config and self.scenario_config.risk_percent is not None:
            return float(self.scenario_config.risk_percent)
        return float(self.baseline_config.risk_percent)

    def __post_init__(self) -> None:
        if self.curve and not self.curve_times:
            self.curve_times = [p.time for p in self.curve]
            self.curve_floating = [p.equity - p.balance for p in self.curve]

    def interpolate_floating(self, when: datetime) -> float:
        if not self.curve:
            return 0.0

        idx = bisect.bisect_left(self.curve_times, when)
        if idx <= 0:
            return self.curve_floating[0]
        if idx >= len(self.curve_times):
            return self.curve_floating[-1]

        left_time = self.curve_times[idx - 1]
        right_time = self.curve_times[idx]
        left_float = self.curve_floating[idx - 1]
        right_float = self.curve_floating[idx]

        dt = (right_time - left_time).total_seconds()
        if dt <= 0:
            return left_float

        ratio = max(0.0, min(1.0, (when - left_time).total_seconds() / dt))
        return left_float + (right_float - left_float) * ratio

    def market_price_at(self, when: datetime) -> Optional[float]:
        if not self.market_times:
            return None
        idx = bisect.bisect_right(self.market_times, when) - 1
        if idx < 0:
            return None
        return self.market_close[idx]


@dataclass
class Position:
    pair: str
    side: str
    lots: float
    entry_price: float
    last_price: Optional[float] = None


# ---------------------------------------------------------------------------
# Configuration types
# ---------------------------------------------------------------------------


@dataclass
class ScalingConfig:
    exponent: float = 1.0
    min_scale: float = 0.1
    max_scale: float = 5.0


@dataclass
class PairInputConfig:
    name: str
    xlsx_file: str
    curve_file: Optional[str]
    market_file: Optional[str]


@dataclass
class PortfolioConfig:
    initial_balance: float
    scaling: ScalingConfig
    pairs: List[PairInputConfig]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _median_volume(deals: List[DealEvent]) -> Optional[float]:
    vols = [d.volume for d in deals if d.volume > 0]
    return median(vols) if vols else None


def _safe_int(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return int(round(float(value)))


def infer_baseline_config(raw_deals: List[object], initial_balance: float, pair: str) -> Tuple[BaselineConfig, Optional[float]]:
    """Infer immutable baseline strategy settings from MT5 deal flow."""
    deals_sorted = sorted(raw_deals, key=lambda d: (d.time, 0 if d.direction == "out" else 1))
    in_deals = [d for d in deals_sorted if d.direction == "in"]
    out_deals = [d for d in deals_sorted if d.direction == "out"]

    first_lot = next((float(d.volume) for d in in_deals if float(d.volume) > 0), None)
    median_lot = median([float(d.volume) for d in in_deals if float(d.volume) > 0]) if in_deals else None

    balance_before = float(initial_balance) if initial_balance and initial_balance > 0 else None
    risk_samples: List[float] = []
    tp_samples_pips: List[float] = []
    grid_samples_pips: List[float] = []

    open_buys: List[Dict[str, float]] = []
    open_sells: List[Dict[str, float]] = []
    open_total = 0
    max_open_total = 0

    pip_factor = 100 if pair.upper().endswith("JPY") else 10000
    previous_in_price_by_side: Dict[str, float] = {}

    def weighted_avg_price(legs: List[Dict[str, float]]) -> Optional[float]:
        total_lot = sum(x["volume"] for x in legs if x["volume"] > 0)
        if total_lot <= 0:
            return None
        return sum(x["price"] * x["volume"] for x in legs if x["volume"] > 0) / total_lot

    for deal in deals_sorted:
        if deal.direction == "in":
            side = (deal.side or "").lower()
            lot = float(deal.volume)
            price = float(deal.price)

            if side == "buy":
                open_buys.append({"volume": lot, "price": price})
            elif side == "sell":
                open_sells.append({"volume": lot, "price": price})

            if side in previous_in_price_by_side and price > 0 and previous_in_price_by_side[side] > 0:
                grid_samples_pips.append(abs(price - previous_in_price_by_side[side]) * pip_factor)
            if side:
                previous_in_price_by_side[side] = price

            open_total += 1
            max_open_total = max(max_open_total, open_total)

            if balance_before and balance_before > 0 and lot > 0:
                risk_samples.append((lot * 100000.0) / balance_before)

        elif deal.direction == "out":
            side = (deal.side or "").lower()
            basket = None
            if side == "sell":
                basket = open_buys
            elif side == "buy":
                basket = open_sells
            elif open_buys or open_sells:
                basket = open_buys if len(open_buys) >= len(open_sells) else open_sells

            if basket:
                be_price = weighted_avg_price(basket)
                if be_price is not None and float(deal.price) > 0:
                    tp_samples_pips.append(abs(float(deal.price) - be_price) * pip_factor)
                basket.pop(0)

            open_total = max(0, open_total - 1)

            if deal.balance and deal.balance > 0:
                balance_before = float(deal.balance)
            elif balance_before is not None:
                balance_before += float(deal.profit) + float(deal.commission) + float(deal.swap)

    # Baseline risk is represented at one-decimal precision in legacy backtest setup.
    risk_percent = round(float(median(risk_samples)), 1) if risk_samples else 0.0
    risk_std = float((sum((x - risk_percent) ** 2 for x in risk_samples) / len(risk_samples)) ** 0.5) if risk_samples else None

    baseline = BaselineConfig(
        risk_percent=risk_percent,
        take_profit=_safe_int(median(tp_samples_pips)) if tp_samples_pips else None,
        grid_size=_safe_int(median(grid_samples_pips)) if grid_samples_pips else None,
        max_trades=max(1, int(max_open_total)),
        initial_balance=float(initial_balance or 0.0),
        first_lot=first_lot,
        median_lot=median_lot,
        trade_count=len(out_deals),
    )
    return baseline, risk_std


def _max_drawdown(values: List[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0

    peak = values[0]
    max_abs = 0.0
    max_pct = 0.0
    for value in values:
        if value > peak:
            peak = value
        dd = peak - value
        max_abs = max(max_abs, dd)
        if peak > 0:
            max_pct = max(max_pct, dd / peak)
    return max_abs, max_pct


def _interpolate_floating(curve: List[CurvePoint], when: datetime) -> float:
    """Backward-compatible interpolation helper retained for notebook usage."""
    if not curve:
        return 0.0

    times = [point.time for point in curve]
    values = [point.equity - point.balance for point in curve]

    idx = bisect.bisect_left(times, when)
    if idx <= 0:
        return values[0]
    if idx >= len(times):
        return values[-1]

    left_time = times[idx - 1]
    right_time = times[idx]
    left_value = values[idx - 1]
    right_value = values[idx]
    dt = (right_time - left_time).total_seconds()
    if dt <= 0:
        return left_value

    ratio = max(0.0, min(1.0, (when - left_time).total_seconds() / dt))
    return left_value + (right_value - left_value) * ratio


def _normalize_pair_key(pair_name: str) -> str:
    return "".join(ch for ch in str(pair_name).upper() if ch.isalpha())


def _write_csv(path: str, rows: List[Dict[str, object]]) -> None:
    if not rows:
        open(path, "w", encoding="utf-8").close()
        return

    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _resolve_abs_path(base_dir: str, rel_or_abs: Optional[str]) -> Optional[str]:
    if rel_or_abs is None:
        return None
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.normpath(os.path.join(base_dir, rel_or_abs))


def _load_m1_market_csv(path: str) -> Tuple[List[datetime], List[float]]:
    """Load MT5 candle export (<DATE> <TIME> <CLOSE>) as sorted series."""
    times: List[datetime] = []
    closes: List[float] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = None
        date_idx = time_idx = close_idx = None
        for row in reader:
            if not row:
                continue
            if header is None:
                header = [c.strip().strip("<>").upper() for c in row]
                idx = {name: i for i, name in enumerate(header)}
                date_idx = idx.get("DATE")
                time_idx = idx.get("TIME")
                close_idx = idx.get("CLOSE")
                if date_idx is None or time_idx is None or close_idx is None:
                    raise ValueError(f"Market CSV missing DATE/TIME/CLOSE columns: {path}")
                continue

            try:
                ts = datetime.strptime(f"{row[date_idx]} {row[time_idx]}", "%Y.%m.%d %H:%M:%S")
                close = float(row[close_idx])
            except (ValueError, IndexError):
                continue
            times.append(ts)
            closes.append(close)
    return times, closes


def discover_reference_price_files(reference_dir: str) -> Dict[str, str]:
    """Discover pair->market price file mapping (M1/M5/M15/etc.) from reference directory."""
    aliases = {
        "eurusd": "EURUSD",
        "eurgbp": "EURGBP",
        "gbpusd": "GBPUSD",
        "usdchf": "USDCHF",
    }
    found: Dict[str, str] = {}
    for fname in sorted(os.listdir(reference_dir)):
        lower = fname.lower()
        if not lower.endswith(".csv") or re.search(r"_m\d+_", lower) is None:
            continue
        for alias, pair in aliases.items():
            if alias in lower:
                if pair in found and found[pair] != os.path.join(reference_dir, fname):
                    raise ValueError(f"Multiple market timeframe files matched for {pair} in {reference_dir}")
                found[pair] = os.path.join(reference_dir, fname)
    return found


def _parse_portfolio_config(config_obj: Dict[str, object], config_dir: str) -> PortfolioConfig:
    scaling = ScalingConfig(
        exponent=float(config_obj.get("scale_exponent", 1.0)),
        min_scale=float(config_obj.get("min_scale", 0.1)),
        max_scale=float(config_obj.get("max_scale", 5.0)),
    )
    if scaling.min_scale <= 0 or scaling.max_scale <= 0:
        raise ValueError("min_scale and max_scale must be > 0")
    if scaling.min_scale > scaling.max_scale:
        raise ValueError("min_scale cannot exceed max_scale")

    pair_items = config_obj.get("pairs", [])
    if not isinstance(pair_items, list) or not pair_items:
        raise ValueError("config must include a non-empty 'pairs' list")

    seen = set()
    pairs: List[PairInputConfig] = []
    for raw_pair in pair_items:
        name = str(raw_pair["name"]).strip()
        if not name:
            raise ValueError("pair name cannot be empty")
        if name in seen:
            raise ValueError(f"Duplicate pair name in config: {name}")
        seen.add(name)

        xlsx = _resolve_abs_path(config_dir, raw_pair.get("xlsx_file") or raw_pair.get("deals_file"))
        curve = _resolve_abs_path(config_dir, raw_pair.get("curve_file") or raw_pair.get("csv_file"))
        if not xlsx or not os.path.exists(xlsx):
            raise FileNotFoundError(f"xlsx_file not found for {name}: {xlsx}")

        pairs.append(PairInputConfig(
            name=name,
            xlsx_file=xlsx,
            curve_file=curve,
            market_file=_resolve_abs_path(config_dir, raw_pair.get("market_file")),
        ))

    initial_balance = float(config_obj.get("initial_balance", 0.0))
    if initial_balance <= 0:
        raise ValueError("initial_balance in config must be > 0")

    return PortfolioConfig(initial_balance=initial_balance, scaling=scaling, pairs=pairs)


# ---------------------------------------------------------------------------
# Pair loading
# ---------------------------------------------------------------------------


def load_pair(
    name: str,
    xlsx_path: str,
    csv_path: Optional[str] = None,
    market_csv_path: Optional[str] = None,
    scenario_config: Optional[ScenarioConfig] = None,
) -> PairData:
    raw_deals, inferred_initial_balance = load_xlsx_deals(xlsx_path, include_open=True)
    baseline_config, risk_std = infer_baseline_config(raw_deals, inferred_initial_balance, name)
    if risk_std is not None and risk_std > 0.05:
        logger.warning(
            "%s inferred risk std deviation is %.4f%% (> 0.05%%). EA may not use fixed-risk position sizing.",
            name,
            risk_std,
        )

    trades = [
        TradeEvent(
            time=d.time,
            pair=name,
            direction=d.direction,
            side=d.side,
            volume=d.volume,
            price=d.price,
        )
        for d in raw_deals
    ]
    deals = [
        DealEvent(
            time=d.time,
            pair=name,
            net_profit=d.profit + d.commission + d.swap,
            volume=d.volume,
        )
        for d in raw_deals
        if d.direction == "out"
    ]

    if csv_path:
        raw_curve = load_graph_csv(csv_path)
        curve = [CurvePoint(time=p.time, balance=p.balance, equity=p.equity) for p in raw_curve]
    else:
        curve = []

    market_times: List[datetime] = []
    market_close: List[float] = []
    if market_csv_path and os.path.exists(market_csv_path):
        market_times, market_close = _load_m1_market_csv(market_csv_path)

    return PairData(
        name=name,
        baseline_config=baseline_config,
        deals=deals,
        trades=trades,
        curve=curve,
        baseline_volume_median=_median_volume(deals),
        scenario_config=scenario_config,
        market_times=market_times,
        market_close=market_close,
    )


# ---------------------------------------------------------------------------
# Position sizing, margin, statistics
# ---------------------------------------------------------------------------


class PositionSizer:
    """Computes combined-portfolio lot and per-trade scale factors."""

    def __init__(self, initial_balance: float, scaling: ScalingConfig):
        if initial_balance <= 0:
            raise ValueError("initial_balance must be > 0")
        self.initial_balance = initial_balance
        self.scaling = scaling

    @staticmethod
    def compute_new_lot(pair_data: PairData, combined_balance: float) -> float:
        """Lot sizing rule requested by user: round(balance * risk% / 100000, 2)."""
        safe_balance = max(0.0, combined_balance)
        risk_percent = max(0.0, float(pair_data.effective_risk_percent() or 0.0))
        return round(safe_balance * risk_percent / 100000.0, 2)

    def scale_factor(self, pair_data: PairData, combined_balance: float, original_lot: float) -> float:
        new_lot = self.compute_new_lot(pair_data, combined_balance)
        if original_lot <= 0:
            return 0.0
        return new_lot / original_lot

    def scale_volume(self, pair_data: PairData, combined_balance: float, original_lot: float) -> float:
        return original_lot * self.scale_factor(pair_data, combined_balance, original_lot)


class Broker(Protocol):
    def margin_requirement_percent(self, pair: str) -> float:
        ...

    def contract_size(self, pair: str) -> float:
        ...

    def stop_out_level_percent(self) -> Optional[float]:
        ...


@dataclass
class ForexBroker:
    margin_requirements: Dict[str, float]
    default_contract_size: float = 100000.0
    stop_out_percent: Optional[float] = None

    def __post_init__(self) -> None:
        self._norm_mmr = {
            _normalize_pair_key(pair): float(percent)
            for pair, percent in (self.margin_requirements or {}).items()
        }

    def margin_requirement_percent(self, pair: str) -> float:
        return self._norm_mmr.get(_normalize_pair_key(pair), 0.0)

    def contract_size(self, pair: str) -> float:
        return self.default_contract_size

    def stop_out_level_percent(self) -> Optional[float]:
        return self.stop_out_percent


class MarginCalculator:
    """Calculates margin-related portfolio metrics from open positions."""

    def __init__(self, broker: Broker):
        self.broker = broker

    def calculate_used_margin(self, positions: Dict[str, List[Position]]) -> float:
        used_margin = 0.0
        for pair, pair_positions in positions.items():
            lots = sum(max(0.0, p.lots) for p in pair_positions)
            if lots <= 0:
                continue
            last_price = next((p.last_price for p in reversed(pair_positions) if p.last_price), None)
            if not last_price:
                continue
            mmr = self.broker.margin_requirement_percent(pair)
            if mmr <= 0:
                continue
            used_margin += (
                lots
                * self.broker.contract_size(pair)
                * last_price
                * mmr
            ) / 100.0
        return used_margin

    @staticmethod
    def calculate_free_margin(equity: float, used_margin: float) -> float:
        return equity - used_margin

    @staticmethod
    def calculate_margin_level_percent(equity: float, used_margin: float) -> Optional[float]:
        if used_margin <= 0:
            return None
        return (equity / used_margin) * 100.0


class PortfolioStatistics:
    """Transforms simulation traces into summary metrics."""

    @staticmethod
    def summarize(
        initial_balance: float,
        final_balance: float,
        final_equity: float,
        equity_values: List[float],
        floating_values: List[float],
        margin_level_values: List[float],
        max_used_margin: float,
        min_free_margin: Optional[float],
        event_count: int,
        start_time: datetime,
        end_time: datetime,
        pairs_data: List[PairData],
        pair_pnl: Dict[str, float],
    ) -> Dict[str, object]:
        total_return_pct = (final_balance / initial_balance - 1.0) * 100.0
        if equity_values and floating_values and len(equity_values) == len(floating_values):
            balance_values = [eq - flt for eq, flt in zip(equity_values, floating_values)]
            dd_ratio = [
                (flt / bal) if bal != 0 else 0.0
                for flt, bal in zip(floating_values, balance_values)
            ]
            worst_idx = min(range(len(dd_ratio)), key=lambda i: dd_ratio[i])
            abs_dd = floating_values[worst_idx]
            pct_dd = dd_ratio[worst_idx]
        else:
            abs_dd = 0.0
            pct_dd = 0.0

        span_days = max((end_time - start_time).total_seconds() / 86400.0, 0.0)
        years = span_days / 365.25
        cagr = PortfolioStatistics.calculate_cagr_percent(initial_balance, final_balance, years)

        return {
            "initial_balance": initial_balance,
            "final_balance": round(final_balance, 4),
            "final_equity": round(final_equity, 4),
            "total_return_percent": round(total_return_pct, 4),
            "max_drawdown_abs": round(abs_dd, 4),
            "max_drawdown_percent": round(pct_dd * 100.0, 4),
            "max_used_margin": round(max_used_margin, 4),
            "min_free_margin": round(min_free_margin, 4) if min_free_margin is not None else None,
            "min_margin_level_percent": round(min(margin_level_values), 4) if margin_level_values else None,
            "cagr_percent": round(cagr, 4),
            "total_deals": event_count,
            "period_start": start_time.strftime("%Y.%m.%d %H:%M:%S"),
            "period_end": end_time.strftime("%Y.%m.%d %H:%M:%S"),
            "pairs": {
                pair_data.name: {
                    "risk_percent": pair_data.effective_risk_percent(),
                    "baseline_risk_percent": pair_data.baseline_config.risk_percent,
                    "baseline_take_profit": pair_data.baseline_config.take_profit,
                    "baseline_grid_size": pair_data.baseline_config.grid_size,
                    "baseline_max_trades": pair_data.baseline_config.max_trades,
                    "baseline_initial_balance": pair_data.baseline_config.initial_balance,
                    "baseline_first_lot": pair_data.baseline_config.first_lot,
                    "baseline_median_lot": pair_data.baseline_config.median_lot,
                    "baseline_trade_count": pair_data.baseline_config.trade_count,
                    "baseline_volume_median": (
                        round(pair_data.baseline_volume_median, 4)
                        if pair_data.baseline_volume_median is not None
                        else None
                    ),
                    "scaled_pnl_contribution": round(pair_pnl[pair_data.name], 4),
                    "deals_count": len(pair_data.deals),
                }
                for pair_data in pairs_data
            },
        }

    @staticmethod
    def calculate_cagr_percent(initial_balance: float, final_balance: float, years: float) -> float:
        if years <= 0 or initial_balance <= 0:
            return 0.0
        if final_balance <= 0:
            return -100.0

        growth_ratio = final_balance / initial_balance
        annual_log_growth = math.log(growth_ratio) / years
        if annual_log_growth > 700:
            return float("inf")
        return (math.exp(annual_log_growth) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


class PortfolioSimulator:
    """Coordinates event replay, equity/margin curve construction, and summary metrics."""

    def __init__(
        self,
        pairs_data: List[PairData],
        initial_balance: float,
        scaling: ScalingConfig,
        margin_requirements: Optional[Dict[str, float]] = None,
        contract_size: float = 100000.0,
    ):
        self.pairs_data = pairs_data
        self.initial_balance = initial_balance
        self.sizer = PositionSizer(initial_balance, scaling)
        self.margin_calculator = MarginCalculator(
            ForexBroker(margin_requirements or {}, default_contract_size=contract_size)
        )
        self.statistics = PortfolioStatistics()

        self._validate_inputs()
        self._log_detected_baseline_configuration()

    def _log_detected_baseline_configuration(self) -> None:
        logger.info("Detected Baseline Configuration")
        logger.info("Pair      Risk    TP    Grid   MaxTrades   Trades")
        for pair_data in sorted(self.pairs_data, key=lambda p: p.name):
            baseline = pair_data.baseline_config
            tp = baseline.take_profit if baseline.take_profit is not None else "-"
            grid = baseline.grid_size if baseline.grid_size is not None else "-"
            logger.info(
                "%-8s %5.2f %5s %6s %10d %8d",
                pair_data.name,
                baseline.risk_percent,
                tp,
                grid,
                baseline.max_trades,
                baseline.trade_count,
            )

    def _validate_inputs(self) -> None:
        if not self.pairs_data:
            raise ValueError("No pair data provided")

        seen = set()
        for pair_data in self.pairs_data:
            name = pair_data.name
            if name in seen:
                raise ValueError(f"Duplicate pair name in pairs_data: {name}")
            seen.add(name)

        any_deals = any(pair_data.deals for pair_data in self.pairs_data)
        if not any_deals:
            raise ValueError("No deal events loaded from any pair")

        for pair_data in self.pairs_data:
            if pair_data.effective_risk_percent() < 0:
                raise ValueError(f"risk_percent must be >= 0 for {pair_data.name}")

    def _build_sorted_deal_events(self) -> List[DealEvent]:
        all_events: List[DealEvent] = []
        for pair_data in self.pairs_data:
            all_events.extend(pair_data.deals)
        all_events.sort(key=lambda event: event.time)
        return all_events

    def _build_sorted_trade_events(self) -> List[TradeEvent]:
        all_trades: List[TradeEvent] = []
        for pair_data in self.pairs_data:
            all_trades.extend(pair_data.trades)
        all_trades.sort(key=lambda trade: trade.time)
        return all_trades

    def reconstruct_balance(
        self,
        pairs_by_name: Dict[str, PairData],
        all_events: List[DealEvent],
        start_time: datetime,
    ) -> Tuple[float, List[Tuple[datetime, float]], List[Dict[str, object]], Dict[str, float]]:
        balance = self.initial_balance
        balance_checkpoints: List[Tuple[datetime, float]] = [(start_time, balance)]
        pair_pnl = {pair_data.name: 0.0 for pair_data in self.pairs_data}
        event_rows: List[Dict[str, object]] = []

        for event in all_events:
            pair_data = pairs_by_name[event.pair]
            new_lot = self.sizer.compute_new_lot(pair_data, balance)
            total_scale = self.sizer.scale_factor(pair_data, balance, event.volume)
            scaled_pnl = event.net_profit * total_scale

            balance += scaled_pnl
            pair_pnl[event.pair] += scaled_pnl
            balance_checkpoints.append((event.time, balance))

            event_rows.append({
                "time": event.time.strftime("%Y.%m.%d %H:%M:%S"),
                "pair": event.pair,
                "baseline_net_profit": round(event.net_profit, 6),
                "baseline_volume": round(event.volume, 4),
                "new_lot": round(new_lot, 4),
                "total_scale": round(total_scale, 6),
                "scaled_net_profit": round(scaled_pnl, 4),
                "running_balance": round(balance, 4),
                "scaled_volume": round(new_lot, 4),
            })

        return balance, balance_checkpoints, event_rows, pair_pnl

    def reconstruct_curve(
        self,
        pairs_by_name: Dict[str, PairData],
        timeline: List[datetime],
        balance_checkpoints: List[Tuple[datetime, float]],
        all_trade_events: List[TradeEvent],
    ) -> Tuple[List[Dict[str, object]], List[float], List[float], List[float], float, Optional[float]]:
        checkpoint_times = [ts for ts, _ in balance_checkpoints]
        trade_idx = 0

        positions = {
            pair_data.name: []
            for pair_data in self.pairs_data
        }

        curve_rows: List[Dict[str, object]] = []
        equity_values: List[float] = []
        floating_values: List[float] = []
        margin_level_values: List[float] = []
        max_used_margin = 0.0
        min_free_margin: Optional[float] = None

        for ts in timeline:
            bal_idx = bisect.bisect_right(checkpoint_times, ts) - 1
            current_balance = balance_checkpoints[max(bal_idx, 0)][1]

            while trade_idx < len(all_trade_events) and all_trade_events[trade_idx].time <= ts:
                trade = all_trade_events[trade_idx]
                trade_pair = pairs_by_name[trade.pair]

                trade_bal_idx = bisect.bisect_right(checkpoint_times, trade.time) - 1
                trade_balance = balance_checkpoints[max(trade_bal_idx, 0)][1]
                scaled_volume = self.sizer.scale_volume(trade_pair, trade_balance, trade.volume)

                if trade.direction == "in":
                    positions[trade.pair].append(Position(
                        pair=trade.pair,
                        side=trade.side,
                        lots=scaled_volume,
                        entry_price=trade.price,
                        last_price=trade.price if trade.price > 0 else None,
                    ))
                elif trade.direction == "out":
                    remaining = scaled_volume
                    # FIFO close approximation using reconstructed scaled lots.
                    pair_positions = positions[trade.pair]
                    i = 0
                    while remaining > 0 and i < len(pair_positions):
                        open_pos = pair_positions[i]
                        close_lots = min(open_pos.lots, remaining)
                        open_pos.lots -= close_lots
                        remaining -= close_lots
                        if open_pos.lots <= 1e-12:
                            pair_positions.pop(i)
                            continue
                        i += 1

                if trade.price > 0:
                    for open_pos in positions[trade.pair]:
                        open_pos.last_price = trade.price

                trade_idx += 1

            floating = 0.0
            for pair_data in self.pairs_data:
                mkt_price = pair_data.market_price_at(ts)
                if mkt_price is None:
                    continue

                for open_pos in positions[pair_data.name]:
                    if open_pos.lots <= 0 or open_pos.entry_price <= 0:
                        continue
                    side = (open_pos.side or "").lower()
                    if side.startswith("sell"):
                        floating += (open_pos.entry_price - mkt_price) * open_pos.lots * self.margin_calculator.broker.contract_size(pair_data.name)
                    else:
                        floating += (mkt_price - open_pos.entry_price) * open_pos.lots * self.margin_calculator.broker.contract_size(pair_data.name)

            equity = current_balance + floating
            used_margin = self.margin_calculator.calculate_used_margin(positions)
            free_margin = self.margin_calculator.calculate_free_margin(equity, used_margin)
            margin_level = self.margin_calculator.calculate_margin_level_percent(equity, used_margin)

            equity_values.append(equity)
            floating_values.append(floating)
            if margin_level is not None:
                margin_level_values.append(margin_level)

            max_used_margin = max(max_used_margin, used_margin)
            min_free_margin = free_margin if min_free_margin is None else min(min_free_margin, free_margin)

            curve_rows.append({
                "time": ts.strftime("%Y.%m.%d %H:%M"),
                "balance": round(current_balance, 4),
                "floating_pnl": round(floating, 4),
                "equity": round(equity, 4),
                "used_margin": round(used_margin, 4),
                "free_margin": round(free_margin, 4),
                "margin_level_percent": round(margin_level, 4) if margin_level is not None else "",
            })

        return curve_rows, equity_values, floating_values, margin_level_values, max_used_margin, min_free_margin

    def run(self) -> Dict[str, object]:
        pairs_by_name = {pair_data.name: pair_data for pair_data in self.pairs_data}

        all_events = self._build_sorted_deal_events()
        all_trade_events = self._build_sorted_trade_events()

        all_curve_times = set()
        for pair_data in self.pairs_data:
            all_curve_times.update(pair_data.market_times)

        start_time = min(
            all_events[0].time,
            min(all_curve_times) if all_curve_times else all_events[0].time,
        )

        final_balance, balance_checkpoints, event_rows, pair_pnl = self.reconstruct_balance(
            pairs_by_name=pairs_by_name,
            all_events=all_events,
            start_time=start_time,
        )

        timeline = sorted(all_curve_times.union({event.time for event in all_events}).union({trade.time for trade in all_trade_events}))
        if not timeline:
            timeline = [start_time, balance_checkpoints[-1][0]]

        curve_rows, equity_values, floating_values, margin_levels, max_used_margin, min_free_margin = self.reconstruct_curve(
            pairs_by_name=pairs_by_name,
            timeline=timeline,
            balance_checkpoints=balance_checkpoints,
            all_trade_events=all_trade_events,
        )

        final_equity = equity_values[-1] if equity_values else final_balance
        end_time = balance_checkpoints[-1][0]

        summary = self.statistics.summarize(
            initial_balance=self.initial_balance,
            final_balance=final_balance,
            final_equity=final_equity,
            equity_values=equity_values,
            floating_values=floating_values,
            margin_level_values=margin_levels,
            max_used_margin=max_used_margin,
            min_free_margin=min_free_margin,
            event_count=len(event_rows),
            start_time=start_time,
            end_time=end_time,
            pairs_data=self.pairs_data,
            pair_pnl=pair_pnl,
        )

        return {
            "event_rows": event_rows,
            "curve_rows": curve_rows,
            "summary": summary,
        }


def run_simulation(
    pairs_data: List[PairData],
    initial_balance: float,
    scale_exponent: float,
    min_scale: float,
    max_scale: float,
    margin_requirements: Optional[Dict[str, float]] = None,
    contract_size: float = 100000.0,
) -> Dict[str, object]:
    """Backward-compatible API kept for notebook and CLI callers."""
    simulator = PortfolioSimulator(
        pairs_data=pairs_data,
        initial_balance=initial_balance,
        scaling=ScalingConfig(scale_exponent, min_scale, max_scale),
        margin_requirements=margin_requirements,
        contract_size=contract_size,
    )
    return simulator.run()


def build_pairs_from_auto(
    data_dir: str,
) -> List[PairData]:
    discovered = discover_files(data_dir)
    reference_dir = os.path.join(os.path.dirname(data_dir), "reference")
    market_files = discover_reference_price_files(reference_dir) if os.path.isdir(reference_dir) else {}
    pairs_data: List[PairData] = []

    for pair, files in discovered.items():
        xlsx = files.get("xlsx")
        csv_path = files.get("csv")
        if not xlsx:
            logger.warning("No XLSX found for %s, skipping", pair)
            continue

        logger.info(
            "%s: xlsx=%s  csv=%s  mkt=%s",
            pair,
            os.path.basename(xlsx),
            os.path.basename(csv_path) if csv_path else "none",
            os.path.basename(market_files[pair]) if pair in market_files else "none",
        )
        pairs_data.append(load_pair(
            name=pair,
            xlsx_path=xlsx,
            csv_path=csv_path,
            market_csv_path=market_files.get(pair),
        ))

    return pairs_data


def build_pairs_from_config(config: PortfolioConfig) -> List[PairData]:
    pairs_data: List[PairData] = []
    for pair in config.pairs:
        logger.info(
            "%s: xlsx=%s  csv=%s  mkt=%s",
            pair.name,
            os.path.basename(pair.xlsx_file),
            os.path.basename(pair.curve_file) if pair.curve_file else "none",
            os.path.basename(pair.market_file) if pair.market_file else "none",
        )
        pairs_data.append(load_pair(
            name=pair.name,
            xlsx_path=pair.xlsx_file,
            csv_path=pair.curve_file,
            market_csv_path=pair.market_file,
        ))
    return pairs_data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "MT5 portfolio analyzer -- reconstructs combined portfolio from "
            "separate pair backtests with dynamic lot scaling."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--auto", action="store_true", help="Auto-discover XLSX + CSV files in --data-dir")
    mode.add_argument("--config", metavar="CONFIG_JSON", help="Path to JSON config file")

    parser.add_argument("--data-dir", default="data", help="Data directory (used with --auto)")
    parser.add_argument("--output-dir", required=True, help="Directory for output files")
    parser.add_argument("--initial-balance", type=float, default=None)
    parser.add_argument("--scale-exponent", type=float, default=1.0)
    parser.add_argument("--min-scale", type=float, default=0.1)
    parser.add_argument("--max-scale", type=float, default=5.0)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    logger.info("Loading pair data...")

    if args.auto:
        data_dir = os.path.abspath(args.data_dir)
        pairs_data = build_pairs_from_auto(data_dir)

        if args.initial_balance is not None:
            initial_balance = args.initial_balance
        else:
            discovered = discover_files(data_dir)
            first_xlsx = next((item.get("xlsx") for item in discovered.values() if item.get("xlsx")), None)
            if not first_xlsx:
                raise FileNotFoundError(
                    f"No XLSX files found in {data_dir}. Cannot infer initial balance."
                )
            _, initial_balance = load_xlsx_deals(first_xlsx)
            logger.info("initial_balance from XLSX: %s", initial_balance)

        scaling = ScalingConfig(args.scale_exponent, args.min_scale, args.max_scale)
    else:
        config_path = os.path.abspath(args.config)
        with open(config_path, encoding="utf-8") as fh:
            cfg_obj = json.load(fh)
        parsed_config = _parse_portfolio_config(cfg_obj, os.path.dirname(config_path))

        pairs_data = build_pairs_from_config(parsed_config)
        initial_balance = args.initial_balance if args.initial_balance is not None else parsed_config.initial_balance

        scaling = ScalingConfig(
            args.scale_exponent if args.scale_exponent != 1.0 else parsed_config.scaling.exponent,
            args.min_scale if args.min_scale != 0.1 else parsed_config.scaling.min_scale,
            args.max_scale if args.max_scale != 5.0 else parsed_config.scaling.max_scale,
        )

    if not pairs_data:
        raise ValueError("No pair data loaded")

    logger.info(
        "Running simulation: initial_balance=%s scale_exp=%s min=%s max=%s",
        initial_balance,
        scaling.exponent,
        scaling.min_scale,
        scaling.max_scale,
    )

    result = run_simulation(
        pairs_data=pairs_data,
        initial_balance=initial_balance,
        scale_exponent=scaling.exponent,
        min_scale=scaling.min_scale,
        max_scale=scaling.max_scale,
    )

    # Import modular reporting stack lazily to avoid circular imports.
    import pandas as pd
    import metrics as metrics_module
    import reports as reports_module

    metric_bundle = metrics_module.build_metric_bundle(
        summary=result["summary"],
        curve_rows=result["curve_rows"],
        event_rows=result["event_rows"],
    )
    summary_table = reports_module.create_summary_table(metric_bundle)
    replay_table = pd.DataFrame(result["event_rows"])

    events_path = os.path.join(output_dir, "combined_events.csv")
    curve_path = os.path.join(output_dir, "combined_curve.csv")
    summary_path = os.path.join(output_dir, "summary.json")

    _write_csv(events_path, result["event_rows"])
    _write_csv(curve_path, result["curve_rows"])
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(result["summary"], fh, indent=2)

    # New modular exports used by thin notebooks and facade APIs.
    reports_module.export_summary(
        output_dir=output_dir,
        metrics_bundle=metric_bundle,
        summary_df=summary_table,
        replay_df=replay_table,
    )
    # Preserve legacy summary.json payload for backward compatibility.
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(result["summary"], fh, indent=2)

    summary = metric_bundle
    logger.info("--- Results ---")
    logger.info("Period:          %s  to  %s", summary["period_start"], summary["period_end"])
    logger.info("Deals replayed:  %s", summary["total_deals"])
    logger.info("Final balance:   %s", f"{summary['final_balance']:,.2f}")
    logger.info("Total return:    %s%%", f"{summary['total_return_percent']:.2f}")
    logger.info(
        "Max DD (equity): %s%%  (%s)",
        f"{summary['max_drawdown_percent']:.2f}",
        f"{summary['max_drawdown_abs']:,.2f}",
    )
    logger.info("CAGR:            %s%%", f"{summary['cagr_percent']:.2f}")
    logger.info("Output written to: %s", output_dir)


if __name__ == "__main__":
    main()
