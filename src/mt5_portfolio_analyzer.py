"""
MT5 Portfolio Analyzer
======================
Reconstructs a combined portfolio from separate MT5 Strategy Tester backtests.

Input files (per pair):
  - ReportTester-*.xlsx   -- full tester report; deals table extracted automatically.
  - testergraph.report.*.csv -- UTF-16 tab-delimited balance/equity curve.

Dynamic lot scaling:
  scale = clamp((combined_balance / initial_balance) ^ scale_exponent,
                min_scale, max_scale)

Per-pair PnL is multiplied by:
  total_scale = balance_scale * (configured_base_lot / median_deal_volume)

Usage:
  python src/mt5_portfolio_analyzer.py --auto --data-dir data --output-dir output
  python src/mt5_portfolio_analyzer.py --config config/example_config.json --output-dir output
"""

import argparse
import bisect
import csv
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from mt5_readers import (
    RawDeal, RawCurvePoint,
    load_xlsx_deals, load_graph_csv, discover_files,
)


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


@dataclass
class CurvePoint:
    time: datetime
    balance: float
    equity: float


@dataclass
class PairConfig:
    name: str
    risk_percent: float
    base_lot: Optional[float]


@dataclass
class PairData:
    config: PairConfig
    deals: List[DealEvent]
    trades: List[TradeEvent]
    curve: List[CurvePoint]
    baseline_volume_median: Optional[float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _median_volume(deals):
    vols = [d.volume for d in deals if d.volume > 0]
    return median(vols) if vols else None


def _interpolate_floating(curve, when):
    if not curve:
        return 0.0
    times = [p.time for p in curve]
    idx = bisect.bisect_left(times, when)
    if idx <= 0:
        p = curve[0]; return p.equity - p.balance
    if idx >= len(curve):
        p = curve[-1]; return p.equity - p.balance
    left, right = curve[idx - 1], curve[idx]
    lf = left.equity - left.balance
    rf = right.equity - right.balance
    dt = (right.time - left.time).total_seconds()
    if dt <= 0:
        return lf
    ratio = max(0.0, min(1.0, (when - left.time).total_seconds() / dt))
    return lf + (rf - lf) * ratio


def _balance_scale(balance, initial, exp, lo, hi):
    raw = (balance / initial) ** exp
    return max(lo, min(hi, raw))


def _max_drawdown(values):
    if not values:
        return 0.0, 0.0
    peak = values[0]
    max_abs = max_pct = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = peak - v
        max_abs = max(max_abs, dd)
        if peak > 0:
            max_pct = max(max_pct, dd / peak)
    return max_abs, max_pct


def _normalize_pair_key(pair_name: str) -> str:
    return "".join(ch for ch in str(pair_name).upper() if ch.isalpha())


def _write_csv(path, rows):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Pair loading
# ---------------------------------------------------------------------------

def load_pair(name, risk_percent, base_lot, xlsx_path, csv_path=None):
    raw_deals, _initial = load_xlsx_deals(xlsx_path, include_open=True)
    trades = [
        TradeEvent(
            time=d.time,
            pair=name,
            direction=d.direction,
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
        curve = [CurvePoint(time=p.time, balance=p.balance, equity=p.equity)
                 for p in raw_curve]
    else:
        curve = []
    return PairData(
        config=PairConfig(name=name, risk_percent=risk_percent, base_lot=base_lot),
        deals=deals,
        trades=trades,
        curve=curve,
        baseline_volume_median=_median_volume(deals),
    )


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------

def run_simulation(
    pairs_data,
    initial_balance,
    scale_exponent,
    min_scale,
    max_scale,
    margin_requirements=None,
    contract_size=100000.0,
):
    if initial_balance <= 0:
        raise ValueError("initial_balance must be > 0")
    if not pairs_data:
        raise ValueError("No pair data provided")

    all_events = []
    for pd in pairs_data:
        all_events.extend(pd.deals)
    all_events.sort(key=lambda e: e.time)

    if not all_events:
        raise ValueError("No deal events loaded from any pair")

    all_curve_times = set()
    for pd in pairs_data:
        for p in pd.curve:
            all_curve_times.add(p.time)

    pairs_by_name = {pd.config.name: pd for pd in pairs_data}
    pair_pnl = {pd.config.name: 0.0 for pd in pairs_data}
    margin_requirements = margin_requirements or {}
    margin_req_norm = {
        _normalize_pair_key(k): float(v)
        for k, v in margin_requirements.items()
    }

    start_time = min(all_events[0].time,
                     min(all_curve_times) if all_curve_times else all_events[0].time)

    balance = initial_balance
    balance_checkpoints = [(start_time, balance)]
    event_rows = []

    for event in all_events:
        pd = pairs_by_name[event.pair]
        bscale = _balance_scale(balance, initial_balance, scale_exponent, min_scale, max_scale)
        cfg = pd.config
        if cfg.base_lot is not None and pd.baseline_volume_median and pd.baseline_volume_median > 0:
            pair_mult = cfg.base_lot / pd.baseline_volume_median
        else:
            pair_mult = 1.0
        total_scale = bscale * pair_mult
        scaled_pnl = event.net_profit * total_scale
        balance += scaled_pnl
        pair_pnl[event.pair] += scaled_pnl
        balance_checkpoints.append((event.time, balance))
        event_rows.append({
            "time": event.time.strftime("%Y.%m.%d %H:%M:%S"),
            "pair": event.pair,
            "baseline_net_profit": round(event.net_profit, 6),
            "baseline_volume": round(event.volume, 4),
            "balance_scale": round(bscale, 6),
            "pair_multiplier": round(pair_mult, 6),
            "total_scale": round(total_scale, 6),
            "scaled_net_profit": round(scaled_pnl, 4),
            "running_balance": round(balance, 4),
            "scaled_volume": round(event.volume * total_scale, 4),
        })

    # Build combined curve on union timeline
    timeline = sorted(all_curve_times.union({e.time for e in all_events}))
    if not timeline:
        timeline = [start_time, balance_checkpoints[-1][0]]

    checkpoint_times = [ts for ts, _ in balance_checkpoints]
    all_trade_events = []
    for pd in pairs_data:
        all_trade_events.extend(pd.trades)
    all_trade_events.sort(key=lambda e: e.time)
    trade_idx = 0

    open_lots = {pd.config.name: 0.0 for pd in pairs_data}
    last_price = {pd.config.name: None for pd in pairs_data}

    curve_rows = []
    equity_values = []
    margin_level_values = []
    max_used_margin = 0.0
    min_free_margin = None

    for ts in timeline:
        idx = bisect.bisect_right(checkpoint_times, ts) - 1
        cur_bal = balance_checkpoints[max(idx, 0)][1]
        bscale = _balance_scale(cur_bal, initial_balance, scale_exponent, min_scale, max_scale)

        while trade_idx < len(all_trade_events) and all_trade_events[trade_idx].time <= ts:
            tr = all_trade_events[trade_idx]
            tr_pd = pairs_by_name[tr.pair]
            tr_idx = bisect.bisect_right(checkpoint_times, tr.time) - 1
            tr_bal = balance_checkpoints[max(tr_idx, 0)][1]
            tr_bscale = _balance_scale(tr_bal, initial_balance, scale_exponent, min_scale, max_scale)
            if tr_pd.config.base_lot is not None and tr_pd.baseline_volume_median and tr_pd.baseline_volume_median > 0:
                tr_pm = tr_pd.config.base_lot / tr_pd.baseline_volume_median
            else:
                tr_pm = 1.0
            scaled_volume = tr.volume * tr_bscale * tr_pm
            if tr.direction == "in":
                open_lots[tr.pair] += scaled_volume
            elif tr.direction == "out":
                open_lots[tr.pair] = max(0.0, open_lots[tr.pair] - scaled_volume)
            if tr.price > 0:
                last_price[tr.pair] = tr.price
            trade_idx += 1

        floating = 0.0
        for pd in pairs_data:
            cfg = pd.config
            if cfg.base_lot is not None and pd.baseline_volume_median and pd.baseline_volume_median > 0:
                pm = cfg.base_lot / pd.baseline_volume_median
            else:
                pm = 1.0
            floating += _interpolate_floating(pd.curve, ts) * bscale * pm
        equity = cur_bal + floating  # Allow negative equity to show full floating PnL impact

        used_margin = 0.0
        for pd in pairs_data:
            pair = pd.config.name
            pair_key = _normalize_pair_key(pair)
            mmr_percent = margin_req_norm.get(pair_key, 0.0)
            price = last_price.get(pair)
            lots = max(0.0, open_lots.get(pair, 0.0))
            if lots <= 0 or not price or mmr_percent <= 0:
                continue
            used_margin += (lots * contract_size * price * mmr_percent) / 100.0

        free_margin = equity - used_margin
        margin_level = (equity / used_margin) * 100.0 if used_margin > 0 else None

        equity_values.append(equity)
        if margin_level is not None:
            margin_level_values.append(margin_level)
        max_used_margin = max(max_used_margin, used_margin)
        min_free_margin = free_margin if min_free_margin is None else min(min_free_margin, free_margin)

        curve_rows.append({
            "time": ts.strftime("%Y.%m.%d %H:%M"),
            "balance": round(cur_bal, 4),
            "floating_pnl": round(floating, 4),
            "equity": round(equity, 4),
            "used_margin": round(used_margin, 4),
            "free_margin": round(free_margin, 4),
            "margin_level_percent": round(margin_level, 4) if margin_level is not None else "",
        })

    final_balance = balance
    final_equity = equity_values[-1] if equity_values else final_balance
    total_return_pct = (final_balance / initial_balance - 1.0) * 100.0
    abs_dd, pct_dd = _max_drawdown(equity_values)
    end_time = balance_checkpoints[-1][0]
    span_days = max((end_time - start_time).total_seconds() / 86400.0, 0.0)
    years = span_days / 365.25
    cagr = ((final_balance / initial_balance) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0

    summary = {
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
        "total_deals": len(event_rows),
        "period_start": start_time.strftime("%Y.%m.%d %H:%M:%S"),
        "period_end": end_time.strftime("%Y.%m.%d %H:%M:%S"),
        "pairs": {
            pd.config.name: {
                "risk_percent": pd.config.risk_percent,
                "configured_base_lot": pd.config.base_lot,
                "baseline_volume_median": round(pd.baseline_volume_median, 4) if pd.baseline_volume_median is not None else None,
                "scaled_pnl_contribution": round(pair_pnl[pd.config.name], 4),
                "deals_count": len(pd.deals),
            }
            for pd in pairs_data
        },
    }

    return {"event_rows": event_rows, "curve_rows": curve_rows, "summary": summary}


# ---------------------------------------------------------------------------
# Default per-pair settings (used in --auto mode)
# ---------------------------------------------------------------------------

_PAIR_RISK = {"EURUSD": 3.3, "EURGBP": 1.2, "GBPUSD": 2.0, "USDCHF": 1.5}
_PAIR_BASE_LOT = {"EURUSD": None, "EURGBP": None, "GBPUSD": None, "USDCHF": None}


def build_pairs_from_auto(data_dir, pair_risk, pair_base_lot):
    discovered = discover_files(data_dir)
    pairs_data = []
    for pair, files in discovered.items():
        xlsx = files.get("xlsx")
        csv_path = files.get("csv")
        if not xlsx:
            print(f"  WARNING: no XLSX found for {pair}, skipping.")
            continue
        print(f"  {pair}: xlsx={os.path.basename(xlsx)}"
              f"  csv={os.path.basename(csv_path) if csv_path else 'none'}")
        pairs_data.append(load_pair(
            name=pair,
            risk_percent=pair_risk.get(pair, 0.0),
            base_lot=pair_base_lot.get(pair),
            xlsx_path=xlsx,
            csv_path=csv_path,
        ))
    return pairs_data


def build_pairs_from_config(config, config_dir):
    def abs_path(rel):
        if rel is None: return None
        return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(config_dir, rel))
    pairs_data = []
    for p in config.get("pairs", []):
        name = str(p["name"]).strip()
        xlsx = abs_path(p.get("xlsx_file") or p.get("deals_file"))
        csv_path = abs_path(p.get("curve_file") or p.get("csv_file"))
        if not xlsx or not os.path.exists(xlsx):
            raise FileNotFoundError(f"xlsx_file not found for {name}: {xlsx}")
        print(f"  {name}: xlsx={os.path.basename(xlsx)}"
              f"  csv={os.path.basename(csv_path) if csv_path else 'none'}")
        pairs_data.append(load_pair(
            name=name,
            risk_percent=float(p.get("risk_percent", 0.0)),
            base_lot=float(p["base_lot"]) if p.get("base_lot") is not None else None,
            xlsx_path=xlsx,
            csv_path=csv_path,
        ))
    return pairs_data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MT5 portfolio analyzer -- reconstructs combined portfolio from "
                    "separate pair backtests with dynamic lot scaling."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--auto", action="store_true",
                      help="Auto-discover XLSX + CSV files in --data-dir")
    mode.add_argument("--config", metavar="CONFIG_JSON",
                      help="Path to JSON config file")
    parser.add_argument("--data-dir", default="data",
                        help="Data directory (used with --auto)")
    parser.add_argument("--output-dir", required=True,
                        help="Directory for output files")
    parser.add_argument("--initial-balance", type=float, default=None)
    parser.add_argument("--scale-exponent", type=float, default=1.0)
    parser.add_argument("--min-scale", type=float, default=0.1)
    parser.add_argument("--max-scale", type=float, default=5.0)
    args = parser.parse_args()

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print("Loading pair data...")
    if args.auto:
        data_dir = os.path.abspath(args.data_dir)
        pairs_data = build_pairs_from_auto(data_dir, _PAIR_RISK, _PAIR_BASE_LOT)
    else:
        config_path = os.path.abspath(args.config)
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
        pairs_data = build_pairs_from_config(config, os.path.dirname(config_path))

    if not pairs_data:
        print("ERROR: No pair data loaded.", file=sys.stderr)
        sys.exit(1)

    # Resolve initial balance
    if args.initial_balance is not None:
        initial_balance = args.initial_balance
    elif not args.auto and args.config:
        with open(os.path.abspath(args.config), encoding="utf-8") as fh:
            cfg = json.load(fh)
        initial_balance = float(cfg.get("initial_balance", 0.0))
        if initial_balance <= 0:
            raise ValueError("initial_balance in config must be > 0")
    else:
        data_dir = os.path.abspath(args.data_dir)
        first_xlsx = next(
            (v["xlsx"] for v in discover_files(data_dir).values() if "xlsx" in v), None
        )
        _, initial_balance = load_xlsx_deals(first_xlsx)
        print(f"  initial_balance from XLSX: {initial_balance}")

    scale_exp = args.scale_exponent
    min_sc    = args.min_scale
    max_sc    = args.max_scale
    if not args.auto and args.config:
        with open(os.path.abspath(args.config), encoding="utf-8") as fh:
            cfg = json.load(fh)
        scale_exp = float(cfg.get("scale_exponent", scale_exp))
        min_sc    = float(cfg.get("min_scale",     min_sc))
        max_sc    = float(cfg.get("max_scale",     max_sc))

    print(f"\nRunning simulation: initial_balance={initial_balance} "
          f"scale_exp={scale_exp} min={min_sc} max={max_sc}")

    result = run_simulation(pairs_data, initial_balance, scale_exp, min_sc, max_sc)

    events_path  = os.path.join(output_dir, "combined_events.csv")
    curve_path   = os.path.join(output_dir, "combined_curve.csv")
    summary_path = os.path.join(output_dir, "summary.json")

    _write_csv(events_path, result["event_rows"])
    _write_csv(curve_path,  result["curve_rows"])
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(result["summary"], fh, indent=2)

    s = result["summary"]
    print(f"\n--- Results ---")
    print(f"  Period:          {s['period_start']}  to  {s['period_end']}")
    print(f"  Deals replayed:  {s['total_deals']}")
    print(f"  Final balance:   {s['final_balance']:,.2f}")
    print(f"  Total return:    {s['total_return_percent']:.2f}%")
    print(f"  Max DD (equity): {s['max_drawdown_percent']:.2f}%  ({s['max_drawdown_abs']:,.2f})")
    print(f"  CAGR:            {s['cagr_percent']:.2f}%")
    print(f"\n  Per-pair contribution:")
    for pair, info in s["pairs"].items():
        print(f"    {pair}: {info['scaled_pnl_contribution']:+,.2f}  "
              f"({info['deals_count']} deals, median_vol={info['baseline_volume_median']})")
    print(f"\nOutput written to: {output_dir}")


if __name__ == "__main__":
    main()
