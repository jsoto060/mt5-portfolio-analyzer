"""High-level facade for MT5 replay, scenario analysis, and folder comparison."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

import charts
import comparison
import metrics
import reports
from replay_engine import ReplayEngine, ReplayRun
from scenario import apply_scenario_overrides


@dataclass
class ReplayResult:
    """High-level replay result wrapper with reporting/chart/export helpers."""

    name: str
    run: ReplayRun
    metrics: Dict[str, object]

    def summary(self) -> pd.DataFrame:
        return reports.create_summary_table(self.metrics)

    def configuration(self) -> pd.DataFrame:
        return reports.create_configuration_table(self.run.pairs_data)

    def pair_table(self) -> pd.DataFrame:
        return reports.create_pair_table(self.run.result["summary"])

    def replay_table(self) -> pd.DataFrame:
        return pd.DataFrame(self.run.result["event_rows"])

    def curve_table(self) -> pd.DataFrame:
        return pd.DataFrame(self.run.result["curve_rows"])

    def plot_equity(self):
        return charts.plot_equity(self.run.result["curve_rows"])

    def plot_margin(self):
        return charts.plot_margin(self.run.result["curve_rows"])

    def plot_pair_contributions(self):
        return charts.plot_pair_balance(self.run.result["event_rows"])

    def plot_pair_floating(self):
        return charts.plot_pair_floating(self.run.pairs_data)

    def plot_pair_drawdown(self):
        return charts.plot_pair_drawdown(self.run.result["event_rows"])

    def export(self, output_dir: str) -> None:
        reports.export_summary(
            output_dir=output_dir,
            metrics_bundle=self.metrics,
            summary_df=self.summary(),
            replay_df=self.replay_table(),
        )


class ReplayAnalyzer:
    """Facade API for baseline replay, what-if scenarios, and folder comparison."""

    def __init__(
        self,
        initial_balance: float = 5000.0,
        scale_exponent: float = 1.0,
        min_scale: float = 0.1,
        max_scale: float = 5.0,
        margin_requirements: Optional[Dict[str, float]] = None,
    ):
        self.engine = ReplayEngine(
            initial_balance=initial_balance,
            scale_exponent=scale_exponent,
            min_scale=min_scale,
            max_scale=max_scale,
            margin_requirements=margin_requirements,
        )

    def _bundle(self, name: str, run: ReplayRun) -> ReplayResult:
        metric_bundle = metrics.build_metric_bundle(
            summary=run.result["summary"],
            curve_rows=run.result["curve_rows"],
            event_rows=run.result["event_rows"],
        )
        return ReplayResult(name=name, run=run, metrics=metric_bundle)

    def replay_folder(self, folder: str, export_dir: Optional[str] = None, name: str = "baseline") -> ReplayResult:
        """Replay one folder and optionally export summary.json/summary.csv/replay.csv."""
        run = self.engine.replay_folder(folder)
        result = self._bundle(name, run)
        if export_dir:
            result.export(export_dir)
        return result

    def replay_scenario(
        self,
        folder: str,
        overrides: Dict[str, Dict[str, float]],
        export_dir: Optional[str] = None,
    ) -> ReplayResult:
        """Replay a folder using inferred baseline with explicit scenario overrides only."""
        base_pairs = self.engine.load_folder(folder)
        scen_pairs = apply_scenario_overrides(base_pairs, overrides)
        run = self.engine.replay_pairs(folder, scen_pairs)
        result = self._bundle("scenario", run)
        if export_dir:
            result.export(export_dir)
        return result

    def compare_folders(self, baseline_folder: str, proposed_folder: str) -> Dict[str, object]:
        """Replay and compare baseline and proposed folders."""
        baseline = self.replay_folder(baseline_folder, name="baseline")
        proposed = self.replay_folder(proposed_folder, name="proposed")

        tables = comparison.compare_results(baseline, proposed)
        charts_payload = {
            "equity_comparison": charts.plot_comparison(
                baseline.run.result["curve_rows"],
                proposed.run.result["curve_rows"],
            )
        }
        return {
            "baseline": baseline,
            "proposed": proposed,
            "tables": tables,
            "charts": charts_payload,
        }


def default_analyzer_for_repo(repo_root: str) -> ReplayAnalyzer:
    """Create analyzer with default settings and optional margin requirements file."""
    mmr_csv = os.path.join(repo_root, "data", "reference", "forex_com_margin_requirements.csv")
    margin_requirements: Dict[str, float] = {}
    if os.path.exists(mmr_csv):
        df = pd.read_csv(mmr_csv)
        for _, row in df.iterrows():
            pair_key = "".join(ch for ch in str(row["currency_pair"]).upper() if ch.isalpha())
            margin_requirements[pair_key] = float(row["mmr_percent"])

    return ReplayAnalyzer(
        initial_balance=5000.0,
        scale_exponent=1.0,
        min_scale=0.1,
        max_scale=5.0,
        margin_requirements=margin_requirements,
    )
