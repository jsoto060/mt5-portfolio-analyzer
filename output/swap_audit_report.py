import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from replay_analyzer import ReplayAnalyzer, default_analyzer_for_repo
from mt5_portfolio_analyzer import load_forex_com_margin_requirements

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "output")
BASELINE_DIR = os.path.join(REPO, "data", "baseline")
PROPOSED_DIR = os.path.join(REPO, "data", "proposed")


def parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y.%m.%d %H:%M:%S")


def parse_accrual_date(desc: str) -> datetime.date:
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", str(desc or ""))
    if not m:
        raise ValueError(f"Cannot parse accrual date from description: {desc!r}")
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def daterange_days(start_date, end_date_exclusive):
    d = start_date
    while d < end_date_exclusive:
        yield d
        d = d + timedelta(days=1)


@dataclass
class Position:
    pair: str
    direction: str
    open_time: datetime
    close_time: datetime
    reconstructed_lot: float
    expected_days: list
    swap_values: list


def build_positions_from_baseline_run(baseline_result, replay_df: pd.DataFrame):
    trade_rows = []
    for pair_data in baseline_result.run.pairs_data:
        for t in pair_data.trades:
            trade_rows.append(
                {
                    "pair": t.pair,
                    "time": t.time,
                    "seq": int(t.sequence_in_pair),
                    "direction": t.direction,
                    "side": t.side,
                }
            )

    trades_df = pd.DataFrame(trade_rows).sort_values(["time", "pair", "seq"]).reset_index(drop=True)

    # Map entry-side reconstructed lot from deal_in rows by pair/time ordinal.
    deal_in = replay_df[replay_df["EventType"] == "deal_in"].copy()
    deal_in["time_dt"] = pd.to_datetime(deal_in["time"], format="%Y.%m.%d %H:%M:%S")
    deal_in["ord"] = deal_in.groupby(["pair", "time_dt"]).cumcount()

    in_trades = trades_df[trades_df["direction"] == "in"].copy()
    in_trades["ord"] = in_trades.groupby(["pair", "time"]).cumcount()

    mapped = in_trades.merge(
        deal_in[["pair", "time_dt", "ord", "new_lot"]],
        left_on=["pair", "time", "ord"],
        right_on=["pair", "time_dt", "ord"],
        how="left",
    )

    if mapped["new_lot"].isna().any():
        missing = mapped[mapped["new_lot"].isna()][["pair", "time", "ord"]].head(10)
        raise ValueError(f"Could not map reconstructed lot to all IN trades. Sample missing: {missing.to_dict('records')}")

    # Build open/close lifecycle per pair with the same LIFO behavior as simulator.
    pair_open = defaultdict(list)
    positions = []

    mapped_by_key = {
        (row.pair, row.time, int(row.seq)): float(row.new_lot)
        for row in mapped.itertuples()
    }

    for row in trades_df.itertuples():
        key = (row.pair, row.time, int(row.seq))
        if row.direction == "in":
            p = {
                "pair": row.pair,
                "direction": row.side,
                "open_time": row.time,
                "close_time": None,
                "reconstructed_lot": round(mapped_by_key[key], 4),
            }
            pair_open[row.pair].append(p)
        else:
            if not pair_open[row.pair]:
                raise ValueError(f"Close without open in trade stream for pair {row.pair} at {row.time}")
            p = pair_open[row.pair].pop()
            p["close_time"] = row.time
            expected_days = list(daterange_days(p["open_time"].date(), p["close_time"].date()))
            positions.append(
                Position(
                    pair=p["pair"],
                    direction=p["direction"],
                    open_time=p["open_time"],
                    close_time=p["close_time"],
                    reconstructed_lot=round(float(p["reconstructed_lot"]), 4),
                    expected_days=expected_days,
                    swap_values=[],
                )
            )

    for pair, stack in pair_open.items():
        if stack:
            raise ValueError(f"Open positions left unclosed for {pair}: {len(stack)}")

    return positions


def main():
    analyzer = default_analyzer_for_repo(REPO)
    baseline = analyzer.replay_folder(BASELINE_DIR)
    baseline_replay = baseline.replay_table().copy()
    baseline_replay = baseline_replay.reset_index(drop=True)
    baseline_replay["time_dt"] = pd.to_datetime(baseline_replay["time"], format="%Y.%m.%d %H:%M:%S")

    swap_df = baseline_replay[baseline_replay["EventType"] == "swap"].copy()
    if swap_df.empty:
        raise ValueError("No swap events found in baseline replay; audit cannot proceed")

    swap_df["accrual_date"] = swap_df["description"].apply(parse_accrual_date)
    swap_df["post_date"] = swap_df["time_dt"].dt.date
    swap_df["year"] = swap_df["time_dt"].dt.year
    swap_df["month"] = swap_df["time_dt"].dt.to_period("M").astype(str)
    swap_df["weekday"] = swap_df["time_dt"].dt.day_name()

    # Part 1: Accounting summaries
    total_swap = float(swap_df["ModeledSwap"].sum())
    event_count = int(len(swap_df))
    avg_swap = float(total_swap / event_count)
    max_single = float(swap_df["ModeledSwap"].min())  # most negative contributor

    swap_by_pair = swap_df.groupby("pair", as_index=False)["ModeledSwap"].sum().sort_values("ModeledSwap")
    swap_by_direction = swap_df.groupby("direction", as_index=False)["ModeledSwap"].sum().sort_values("ModeledSwap")
    swap_by_year = swap_df.groupby("year", as_index=False)["ModeledSwap"].sum().sort_values("year")
    swap_by_month = swap_df.groupby("month", as_index=False)["ModeledSwap"].sum().sort_values("month")
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    swap_by_weekday = swap_df.groupby("weekday", as_index=False)["ModeledSwap"].sum()
    swap_by_weekday["weekday"] = pd.Categorical(swap_by_weekday["weekday"], categories=weekday_order, ordered=True)
    swap_by_weekday = swap_by_weekday.sort_values("weekday")

    # Part 2: position-level validation in aggregate
    positions = build_positions_from_baseline_run(baseline, baseline_replay)

    expected_counts = Counter()
    positions_by_key_and_day = defaultdict(list)
    for idx, p in enumerate(positions):
        for d in p.expected_days:
            k = (p.pair, p.direction, round(p.reconstructed_lot, 4), d)
            expected_counts[k] += 1
            positions_by_key_and_day[k].append(idx)

    actual_counts = Counter()
    swap_rows_by_key = defaultdict(list)
    for row in swap_df.itertuples():
        k = (row.pair, str(row.direction), round(float(row.new_lot), 4), row.accrual_date)
        actual_counts[k] += 1
        swap_rows_by_key[k].append(float(row.ModeledSwap))

    missing_events = []
    extra_events = []
    for k in sorted(set(expected_counts.keys()) | set(actual_counts.keys())):
        exp = expected_counts.get(k, 0)
        act = actual_counts.get(k, 0)
        if act < exp:
            missing_events.append({"key": k, "expected": exp, "actual": act})
        elif act > exp:
            extra_events.append({"key": k, "expected": exp, "actual": act})

    duplicate_same_key_day = [
        {"key": k, "count": c}
        for k, c in actual_counts.items()
        if c > 1
    ]

    # Part 3: Ensure swap application happens exactly once in balance replay.
    prev_balance_after = None
    row_delta_failures = []
    swap_scaled_profit_mismatches = []
    swap_doublecount_candidates = []

    delta_tolerance = 1e-3

    for i, row in baseline_replay.iterrows():
        bal_after = float(row["BalanceAfterEvent"])
        if prev_balance_after is None:
            bal_before = 5000.0
        else:
            bal_before = prev_balance_after

        observed_delta = round(bal_after - bal_before, 4)

        if row["EventType"] == "swap":
            modeled_swap = round(float(row["ModeledSwap"]), 4)
            scaled_profit = round(float(row["scaled_net_profit"]), 4)
            if abs(observed_delta - modeled_swap) > delta_tolerance:
                row_delta_failures.append({"index": int(i), "event_type": "swap", "observed_delta": observed_delta, "modeled_swap": modeled_swap})
            if scaled_profit != modeled_swap:
                swap_scaled_profit_mismatches.append({"index": int(i), "scaled_net_profit": scaled_profit, "modeled_swap": modeled_swap})
            if abs(float(row["baseline_net_profit"])) > 1e-9:
                swap_doublecount_candidates.append({"index": int(i), "baseline_net_profit": float(row["baseline_net_profit"])})
        else:
            expected_delta = round(float(row["scaled_net_profit"]), 4)
            if abs(observed_delta - expected_delta) > delta_tolerance:
                row_delta_failures.append({"index": int(i), "event_type": str(row["EventType"]), "observed_delta": observed_delta, "expected_scaled": expected_delta})
            if abs(float(row["ModeledSwap"])) > 1e-9:
                swap_doublecount_candidates.append({"index": int(i), "event_type": str(row["EventType"]), "modeled_swap": float(row["ModeledSwap"])})

        prev_balance_after = bal_after

    cumulative_matches_total = round(float(swap_df["CumulativeModeledSwap"].iloc[-1]), 4) == round(total_swap, 4)

    # Part 4: formula spot checks
    rates = analyzer.engine.swap_engine.rates_by_symbol if analyzer.engine.swap_engine is not None else {}
    sample_idx = list(swap_df.sample(n=min(12, len(swap_df)), random_state=42).index)
    sample_rows = []
    formula_failures = []
    for idx in sample_idx:
        r = swap_df.loc[idx]
        pair = r["pair"]
        direction = str(r["direction"]).lower()
        lot = float(r["new_lot"])
        actual = round(float(r["ModeledSwap"]), 2)
        rate = float(rates[pair][direction])
        expected = round(rate * (lot / 0.01), 2)
        row = {
            "Pair": pair,
            "Direction": direction,
            "Original lot": float(r["baseline_volume"]),
            "Reconstructed lot": lot,
            "Configured rate": rate,
            "Expected swap": expected,
            "Actual swap": actual,
            "Matches": expected == actual,
        }
        sample_rows.append(row)
        if expected != actual:
            formula_failures.append(row)

    sample_df = pd.DataFrame(sample_rows)

    # Allocate swap rows to positions for position-level totals
    for k, pos_indices in positions_by_key_and_day.items():
        vals = swap_rows_by_key.get(k, [])
        if len(vals) != len(pos_indices):
            continue
        for pos_i, v in zip(pos_indices, vals):
            positions[pos_i].swap_values.append(float(v))

    position_summary = []
    for p in positions:
        days = len(p.expected_days)
        total_p_swap = round(sum(p.swap_values), 4)
        avg_daily = round(total_p_swap / days, 4) if days > 0 else 0.0
        position_summary.append(
            {
                "Pair": p.pair,
                "Open Time": p.open_time.strftime("%Y.%m.%d %H:%M:%S"),
                "Close Time": p.close_time.strftime("%Y.%m.%d %H:%M:%S"),
                "Days Held": days,
                "Reconstructed Lot": round(float(p.reconstructed_lot), 4),
                "Total Swap": total_p_swap,
                "Average Daily Swap": avg_daily,
            }
        )

    position_df = pd.DataFrame(position_summary).sort_values("Total Swap")

    # Part 5: swap timeline CSV
    swap_only = baseline_replay[baseline_replay["EventType"] == "swap"].copy()
    swap_only["post_date"] = pd.to_datetime(swap_only["time"], format="%Y.%m.%d %H:%M:%S").dt.date

    # Balance before/after each swap date from ordered replay rows.
    before_after = []
    swap_indices = swap_only.index.tolist()
    by_date_idx = defaultdict(list)
    for i in swap_indices:
        d = pd.to_datetime(baseline_replay.loc[i, "time"], format="%Y.%m.%d %H:%M:%S").date()
        by_date_idx[d].append(i)

    cumulative = 0.0
    timeline_rows = []
    for d in sorted(by_date_idx.keys()):
        idxs = sorted(by_date_idx[d])
        daily_swap = round(float(baseline_replay.loc[idxs, "ModeledSwap"].sum()), 4)
        cumulative = round(cumulative + daily_swap, 4)
        first_idx = idxs[0]
        last_idx = idxs[-1]
        if first_idx == 0:
            bal_before = 5000.0
        else:
            bal_before = round(float(baseline_replay.loc[first_idx - 1, "BalanceAfterEvent"]), 4)
        bal_after = round(float(baseline_replay.loc[last_idx, "BalanceAfterEvent"]), 4)
        open_positions = int(len(idxs))

        timeline_rows.append(
            {
                "Date": d.strftime("%Y-%m-%d"),
                "DailySwap": daily_swap,
                "CumulativeSwap": cumulative,
                "OpenPositions": open_positions,
                "BalanceBefore": bal_before,
                "BalanceAfter": bal_after,
            }
        )

    timeline_df = pd.DataFrame(timeline_rows)

    # Baseline vs no-swap decomposition for final assessment
    margin_reqs = load_forex_com_margin_requirements(os.path.join(REPO, "data", "reference"))
    no_swap_analyzer = ReplayAnalyzer(
        initial_balance=5000.0,
        scale_exponent=1.0,
        min_scale=0.1,
        max_scale=5.0,
        margin_requirements=margin_reqs,
        swap_engine=None,
    )
    baseline_no_swap = no_swap_analyzer.replay_folder(BASELINE_DIR)

    with_swap_final = float(baseline.run.result["summary"]["final_balance"])
    no_swap_final = float(baseline_no_swap.run.result["summary"]["final_balance"])
    final_balance_delta = round(with_swap_final - no_swap_final, 4)
    direct_swap_component = round(total_swap, 4)
    indirect_sizing_component = round(final_balance_delta - direct_swap_component, 4)

    timeline_df.to_csv(os.path.join(OUT, "swap_timeline.csv"), index=False)
    position_df.to_csv(os.path.join(OUT, "swap_position_summary.csv"), index=False)

    swap_by_pair.to_csv(os.path.join(OUT, "swap_by_pair.csv"), index=False)
    swap_by_direction.to_csv(os.path.join(OUT, "swap_by_direction.csv"), index=False)
    swap_by_year.to_csv(os.path.join(OUT, "swap_by_year.csv"), index=False)
    swap_by_month.to_csv(os.path.join(OUT, "swap_by_month.csv"), index=False)
    swap_by_weekday.to_csv(os.path.join(OUT, "swap_by_weekday.csv"), index=False)
    sample_df.to_csv(os.path.join(OUT, "swap_formula_samples.csv"), index=False)

    report = {
        "part1": {
            "total_modeled_swap": round(total_swap, 4),
            "swap_event_count": event_count,
            "average_swap_per_event": round(avg_swap, 6),
            "max_single_swap_most_negative": round(max_single, 4),
        },
        "part2": {
            "positions_total": len(positions),
            "expected_position_day_swap_events": int(sum(expected_counts.values())),
            "actual_swap_events": int(sum(actual_counts.values())),
            "missing_events_count": len(missing_events),
            "extra_events_count": len(extra_events),
            "duplicate_same_pair_direction_lot_day_keys": len(duplicate_same_key_day),
            "missing_examples": [
                {
                    "pair": m["key"][0],
                    "direction": m["key"][1],
                    "lot": m["key"][2],
                    "day": str(m["key"][3]),
                    "expected": m["expected"],
                    "actual": m["actual"],
                }
                for m in missing_events[:20]
            ],
            "extra_examples": [
                {
                    "pair": m["key"][0],
                    "direction": m["key"][1],
                    "lot": m["key"][2],
                    "day": str(m["key"][3]),
                    "expected": m["expected"],
                    "actual": m["actual"],
                }
                for m in extra_events[:20]
            ],
        },
        "part3": {
            "row_delta_failures": len(row_delta_failures),
            "swap_scaled_profit_mismatches": len(swap_scaled_profit_mismatches),
            "swap_doublecount_candidates": len(swap_doublecount_candidates),
            "cumulative_swap_matches_total": cumulative_matches_total,
        },
        "part4": {
            "sampled_events": len(sample_rows),
            "formula_failures": len(formula_failures),
        },
        "part8_assessment": {
            "with_swap_final_balance": with_swap_final,
            "no_swap_final_balance": no_swap_final,
            "delta_with_minus_without": final_balance_delta,
            "direct_swap_component": direct_swap_component,
            "indirect_lot_sizing_component": indirect_sizing_component,
            "all_core_checks_passed": (
                len(missing_events) == 0
                and len(extra_events) == 0
                and len(row_delta_failures) == 0
                and len(swap_scaled_profit_mismatches) == 0
                and len(swap_doublecount_candidates) == 0
                and len(formula_failures) == 0
                and cumulative_matches_total
            ),
        },
    }

    with open(os.path.join(OUT, "swap_audit_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
