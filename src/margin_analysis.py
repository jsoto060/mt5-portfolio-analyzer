"""Margin analysis module for MT5 Portfolio Analyzer.

Provides the ``MarginAnalysis`` class, a pure consumer of ``TimelineSnapshot``
objects produced by the replay engine.  Contains no replay logic.

Architecture
------------
Replay engine  ->  TimelineSnapshot list  ->  MarginAnalysis  ->  CSV / JSON outputs
                                                              ->  DataFrames for notebooks

Design rules
------------
- No replay state is reconstructed here.
- No heuristic recommendations are generated.
- Per-event markdown files are not produced; structured CSVs are the output.
- ``used_margin_contribution_pct`` is named explicitly to avoid ambiguity.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import pandas as pd


DEFAULT_THRESHOLDS = [300.0, 200.0, 150.0, 100.0]


class MarginAnalysis:
    """Analyzes margin behaviour from replay timeline snapshots.

    Parameters
    ----------
    snapshots:
        List of ``TimelineSnapshot`` objects returned by the replay engine in
        ``result["timeline_snapshots"]``.  The list is consumed read-only.
    """

    def __init__(self, snapshots: list) -> None:
        self._snapshots = snapshots
        self.__curve_df: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pair_names(self) -> List[str]:
        for snap in self._snapshots:
            if snap.pair_snapshots:
                return sorted(snap.pair_snapshots.keys())
        return []

    def _normalize_thresholds(self, thresholds: Optional[List[float]]) -> List[float]:
        values = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
        return sorted({float(v) for v in values}, reverse=True)

    @staticmethod
    def _format_threshold_value(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    # ------------------------------------------------------------------
    # Primary DataFrames
    # ------------------------------------------------------------------

    def curve_df(self) -> pd.DataFrame:
        """Full margin timeline with per-pair breakdown.

        One row per replay timestamp.  Columns:

        timestamp, balance, equity, floating_pnl, used_margin,
        free_margin, margin_level,
        <PAIR>_open_positions, <PAIR>_floating_pnl,
        <PAIR>_used_margin, <PAIR>_used_margin_contribution_pct  (per pair)

        ``used_margin_contribution_pct`` is computed here (analytics layer),
        not in the replay engine.  Contributions sum to ~100% when any margin
        is in use.
        """
        if self.__curve_df is not None:
            return self.__curve_df

        rows = []
        for snap in self._snapshots:
            row: Dict = {
                "timestamp": snap.timestamp,
                "balance": snap.balance,
                "equity": snap.equity,
                "floating_pnl": snap.floating_pnl,
                "used_margin": snap.used_margin,
                "free_margin": snap.free_margin,
                "margin_level": snap.margin_level,
            }
            for pair_snap in snap.pair_snapshots.values():
                p = pair_snap.pair
                row[f"{p}_open_positions"] = pair_snap.open_positions
                row[f"{p}_floating_pnl"] = pair_snap.floating_pnl
                row[f"{p}_used_margin"] = pair_snap.used_margin
                row[f"{p}_used_margin_contribution_pct"] = (
                    round(pair_snap.used_margin / snap.used_margin * 100.0, 2)
                    if snap.used_margin > 0
                    else 0.0
                )
            rows.append(row)

        self.__curve_df = pd.DataFrame(rows)
        return self.__curve_df

    def events_df(self, thresholds: Optional[List[float]] = None) -> pd.DataFrame:
        """Detect canonical low-margin episodes below the highest threshold.

        One row per physical low-margin episode where margin_level drops below
        max(thresholds). No duplicate rows are created per threshold.

        Columns: event_id, start_time, end_time, duration_minutes,
        min_margin_level, min_margin_time, max_used_margin, min_free_margin,
        max_floating_loss, worst_threshold_crossed, thresholds_crossed,
        largest_used_margin_contributor,
        largest_used_margin_contribution_pct,
        <PAIR>_open_positions_at_min (per pair)
        """
        normalized_thresholds = self._normalize_thresholds(thresholds)
        if not normalized_thresholds:
            return pd.DataFrame()

        highest_threshold = normalized_thresholds[0]

        df = self.curve_df()
        if df.empty or "margin_level" not in df.columns:
            return pd.DataFrame()

        pair_names = self._pair_names()
        all_rows: List[Dict] = []

        below = df["margin_level"].notna() & (df["margin_level"] < highest_threshold)
        groups = (below != below.shift()).cumsum()

        episode_index = 0
        for _, grp in df[below].groupby(groups[below]):
            if grp.empty:
                continue

            episode_index += 1
            start_time = grp["timestamp"].iloc[0]
            end_time = grp["timestamp"].iloc[-1]
            duration_minutes = int((end_time - start_time).total_seconds() / 60)

            min_idx = grp["margin_level"].idxmin()
            min_row = grp.loc[min_idx]
            min_margin_level = float(min_row["margin_level"])

            crossed_thresholds = [
                threshold for threshold in normalized_thresholds if min_margin_level < threshold
            ]
            if not crossed_thresholds:
                continue
            worst_threshold = min(crossed_thresholds)

            largest_contributor = ""
            largest_contribution = 0.0
            for pair in pair_names:
                col = f"{pair}_used_margin_contribution_pct"
                if col in min_row and pd.notna(min_row[col]):
                    v = float(min_row[col])
                    if v > largest_contribution:
                        largest_contribution = v
                        largest_contributor = pair

            event_id = f"evt_{int(start_time.timestamp())}_{episode_index}"
            row = {
                "event_id": event_id,
                "start_time": start_time,
                "end_time": end_time,
                "duration_minutes": duration_minutes,
                "min_margin_level": round(min_margin_level, 2),
                "min_margin_time": min_row["timestamp"],
                "max_used_margin": round(float(grp["used_margin"].max()), 2),
                "min_free_margin": round(float(grp["free_margin"].min()), 2),
                "max_floating_loss": round(float(grp["floating_pnl"].min()), 2),
                "worst_threshold_crossed": worst_threshold,
                "thresholds_crossed": ",".join(self._format_threshold_value(v) for v in crossed_thresholds),
                "largest_used_margin_contributor": largest_contributor,
                "largest_used_margin_contribution_pct": round(largest_contribution, 2),
            }
            for pair in pair_names:
                col = f"{pair}_open_positions"
                row[f"{pair}_open_positions_at_min"] = (
                    int(min_row[col]) if col in min_row and pd.notna(min_row[col]) else 0
                )
            all_rows.append(row)

        return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

    def threshold_summary_df(
        self,
        events: Optional[pd.DataFrame] = None,
        thresholds: Optional[List[float]] = None,
    ) -> pd.DataFrame:
        """Aggregate threshold statistics from canonical events.

        One row per threshold. Event counts are unique canonical episodes where
        the threshold was crossed (based on episode minimum margin).
        """
        normalized_thresholds = self._normalize_thresholds(thresholds)
        if not normalized_thresholds:
            return pd.DataFrame(columns=[
                "threshold",
                "event_count",
                "total_minutes_below",
                "longest_event_minutes_below",
                "worst_margin_level",
            ])

        if events is None:
            events = self.events_df(normalized_thresholds)

        rows: List[Dict] = []
        for threshold in normalized_thresholds:
            if events.empty:
                crossed = pd.DataFrame()
            else:
                crossed = events[events["min_margin_level"] < threshold]

            rows.append({
                "threshold": threshold,
                "event_count": int(len(crossed)),
                "total_minutes_below": int(crossed["duration_minutes"].sum()) if not crossed.empty else 0,
                "longest_event_minutes_below": int(crossed["duration_minutes"].max()) if not crossed.empty else 0,
                "worst_margin_level": (
                    round(float(crossed["min_margin_level"].min()), 2) if not crossed.empty else None
                ),
            })

        return pd.DataFrame(rows)

    def event_baskets_df(self, events: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """Basket composition at each event margin minimum.

        One row per (event_id, pair).  Columns:
        event_id, worst_threshold_crossed, thresholds_crossed,
        min_margin_time, pair,
        open_positions, used_margin, floating_pnl,
        used_margin_contribution_pct,
        portfolio_used_margin, portfolio_margin_level
        """
        if events is None:
            events = self.events_df()
        if events.empty:
            return pd.DataFrame()

        df = self.curve_df()
        pair_names = self._pair_names()
        rows: List[Dict] = []

        for _, event in events.drop_duplicates(subset=["event_id"]).iterrows():
            min_time = event["min_margin_time"]
            closest_idx = (df["timestamp"] - min_time).abs().idxmin()
            snap = df.loc[closest_idx]

            for pair in pair_names:
                rows.append({
                    "event_id": event["event_id"],
                    "worst_threshold_crossed": event.get("worst_threshold_crossed"),
                    "thresholds_crossed": event.get("thresholds_crossed"),
                    "min_margin_time": min_time,
                    "pair": pair,
                    "open_positions": int(snap.get(f"{pair}_open_positions", 0)),
                    "used_margin": round(float(snap.get(f"{pair}_used_margin", 0.0)), 2),
                    "floating_pnl": round(float(snap.get(f"{pair}_floating_pnl", 0.0)), 2),
                    "used_margin_contribution_pct": round(
                        float(snap.get(f"{pair}_used_margin_contribution_pct", 0.0)), 2
                    ),
                    "portfolio_used_margin": round(float(snap.get("used_margin", 0.0)), 2),
                    "portfolio_margin_level": (
                        round(float(snap["margin_level"]), 2)
                        if pd.notna(snap.get("margin_level"))
                        else None
                    ),
                })

        return pd.DataFrame(rows)

    def pair_summary_df(self, events: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """Per-pair statistics over the entire replay period.

        One row per pair.  Columns:
        pair, avg_used_margin, max_used_margin,
        avg_used_margin_contribution_pct, peak_used_margin_contribution_pct,
        avg_floating_pnl, worst_floating_pnl,
        avg_open_positions, max_open_positions,
        margin_events_participated, margin_events_as_largest_contributor
        """
        if events is None:
            events = self.events_df()

        df = self.curve_df()
        if df.empty:
            return pd.DataFrame()

        pair_names = self._pair_names()
        rows: List[Dict] = []

        for pair in pair_names:
            used_m = pd.to_numeric(df.get(f"{pair}_used_margin", 0.0), errors="coerce").fillna(0.0)
            floating = pd.to_numeric(df.get(f"{pair}_floating_pnl", 0.0), errors="coerce").fillna(0.0)
            contrib = pd.to_numeric(
                df.get(f"{pair}_used_margin_contribution_pct", 0.0), errors="coerce"
            ).fillna(0.0)
            open_pos = pd.to_numeric(df.get(f"{pair}_open_positions", 0), errors="coerce").fillna(0.0)

            if not events.empty:
                open_col = f"{pair}_open_positions_at_min"
                participated = (
                    int(events[open_col].gt(0).sum()) if open_col in events.columns else 0
                )
                as_largest = int(
                    events["largest_used_margin_contributor"].eq(pair).sum()
                    if "largest_used_margin_contributor" in events.columns
                    else 0
                )
            else:
                participated = 0
                as_largest = 0

            rows.append({
                "pair": pair,
                "avg_used_margin": round(float(used_m.mean()), 2),
                "max_used_margin": round(float(used_m.max()), 2),
                "avg_used_margin_contribution_pct": round(float(contrib.mean()), 2),
                "peak_used_margin_contribution_pct": round(float(contrib.max()), 2),
                "avg_floating_pnl": round(float(floating.mean()), 2),
                "worst_floating_pnl": round(float(floating.min()), 2),
                "avg_open_positions": round(float(open_pos.mean()), 2),
                "max_open_positions": int(open_pos.max()),
                "margin_events_participated": participated,
                "margin_events_as_largest_contributor": as_largest,
            })

        return pd.DataFrame(rows)

    def portfolio_summary_dict(self, events: Optional[pd.DataFrame] = None) -> dict:
        """Portfolio-level margin statistics.

        Keys: min/avg/median margin_level, p95/max used_margin,
        max/min free_margin, total_minutes_below_<N>,
        event_count_below_<N>, longest_event_minutes_below_<N>
        for N in {300, 200, 150, 100}.
        """
        if events is None:
            events = self.events_df()

        df = self.curve_df()
        if df.empty:
            return {}

        margin = pd.to_numeric(df["margin_level"], errors="coerce").dropna()
        free_m = pd.to_numeric(df["free_margin"], errors="coerce").dropna()
        used_m = pd.to_numeric(df["used_margin"], errors="coerce").dropna()

        result: Dict = {
            "min_margin_level": round(float(margin.min()), 2) if len(margin) else None,
            "avg_margin_level": round(float(margin.mean()), 2) if len(margin) else None,
            "median_margin_level": round(float(margin.median()), 2) if len(margin) else None,
            "p95_used_margin": round(float(used_m.quantile(0.95)), 2) if len(used_m) else None,
            "max_used_margin": round(float(used_m.max()), 2) if len(used_m) else None,
            "max_free_margin": round(float(free_m.max()), 2) if len(free_m) else None,
            "min_free_margin": round(float(free_m.min()), 2) if len(free_m) else None,
        }

        summary_by_threshold = self.threshold_summary_df(events)
        for thr in DEFAULT_THRESHOLDS:
            key = int(thr)
            row = summary_by_threshold[summary_by_threshold["threshold"] == float(thr)]
            if row.empty:
                result[f"total_minutes_below_{key}"] = 0
                result[f"event_count_below_{key}"] = 0
                result[f"longest_event_minutes_below_{key}"] = 0
                continue

            result[f"total_minutes_below_{key}"] = int(row["total_minutes_below"].iloc[0])
            result[f"event_count_below_{key}"] = int(row["event_count"].iloc[0])
            result[f"longest_event_minutes_below_{key}"] = int(row["longest_event_minutes_below"].iloc[0])

        return result

    def portfolio_summary_df(self, events: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """Portfolio-level margin statistics as a two-column DataFrame (metric, value)."""
        d = self.portfolio_summary_dict(events)
        return pd.DataFrame({"metric": list(d.keys()), "value": list(d.values())})

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_all(
        self,
        output_dir: str,
        thresholds: Optional[List[float]] = None,
    ) -> None:
        """Write all margin analysis artifacts to *output_dir*.

        Files produced:
            margin_curve.csv
            margin_events.csv          (only when events exist)
            margin_event_baskets.csv   (only when events exist)
            margin_pair_summary.csv
            margin_summary.json
        """
        os.makedirs(output_dir, exist_ok=True)

        events = self.events_df(thresholds)

        self.curve_df().to_csv(os.path.join(output_dir, "margin_curve.csv"), index=False)

        if not events.empty:
            events.to_csv(os.path.join(output_dir, "margin_events.csv"), index=False)

        baskets = self.event_baskets_df(events)
        if not baskets.empty:
            baskets.to_csv(os.path.join(output_dir, "margin_event_baskets.csv"), index=False)

        pair_summary = self.pair_summary_df(events)
        if not pair_summary.empty:
            pair_summary.to_csv(os.path.join(output_dir, "margin_pair_summary.csv"), index=False)

        summary = self.portfolio_summary_dict(events)
        with open(os.path.join(output_dir, "margin_summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Notebook loaders — read persisted artifacts without touching replay state
# ---------------------------------------------------------------------------

def load_margin_curve(output_dir: str) -> Optional[pd.DataFrame]:
    """Load margin_curve.csv."""
    path = os.path.join(output_dir, "margin_curve.csv")
    return pd.read_csv(path) if os.path.exists(path) else None


def load_margin_events(output_dir: str) -> Optional[pd.DataFrame]:
    """Load margin_events.csv."""
    path = os.path.join(output_dir, "margin_events.csv")
    return pd.read_csv(path) if os.path.exists(path) else None


def load_margin_summary(output_dir: str) -> Optional[dict]:
    """Load margin_summary.json."""
    path = os.path.join(output_dir, "margin_summary.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_pair_summary(output_dir: str) -> Optional[pd.DataFrame]:
    """Load margin_pair_summary.csv."""
    path = os.path.join(output_dir, "margin_pair_summary.csv")
    return pd.read_csv(path) if os.path.exists(path) else None


def load_basket_composition(output_dir: str) -> Optional[pd.DataFrame]:
    """Load margin_event_baskets.csv."""
    path = os.path.join(output_dir, "margin_event_baskets.csv")
    return pd.read_csv(path) if os.path.exists(path) else None
