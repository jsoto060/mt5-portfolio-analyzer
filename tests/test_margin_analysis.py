import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath("src"))

from margin_analysis import MarginAnalysis  # noqa: E402


class MarginAnalysisCanonicalEventsTests(unittest.TestCase):
    def _make_snapshots(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        margin_levels = [350.0, 280.0, 180.0, 120.0, 220.0, 310.0, 290.0, 260.0, 320.0]

        snapshots = []
        for idx, margin_level in enumerate(margin_levels):
            ts = start + timedelta(minutes=10 * idx)
            used_margin = 10000.0
            equity = margin_level * used_margin / 100.0
            floating = -100.0 * idx
            free_margin = equity - used_margin

            pair_snaps = {
                "EURUSD": SimpleNamespace(
                    pair="EURUSD",
                    open_positions=1 if margin_level < 300 else 0,
                    floating_pnl=floating * 0.6,
                    used_margin=used_margin * 0.6,
                ),
                "GBPUSD": SimpleNamespace(
                    pair="GBPUSD",
                    open_positions=1 if margin_level < 300 else 0,
                    floating_pnl=floating * 0.4,
                    used_margin=used_margin * 0.4,
                ),
            }

            snapshots.append(SimpleNamespace(
                timestamp=ts,
                balance=10000.0,
                equity=equity,
                floating_pnl=floating,
                used_margin=used_margin,
                free_margin=free_margin,
                margin_level=margin_level,
                pair_snapshots=pair_snaps,
            ))

        return snapshots

    def test_events_are_canonical_per_physical_episode(self):
        ma = MarginAnalysis(self._make_snapshots())
        events = ma.events_df([300.0, 200.0, 150.0, 100.0])

        self.assertEqual(len(events), 2)
        self.assertIn("worst_threshold_crossed", events.columns)
        self.assertIn("thresholds_crossed", events.columns)

        worst_values = sorted(events["worst_threshold_crossed"].tolist())
        self.assertEqual(worst_values, [150.0, 300.0])

        thresholds_crossed = set(events["thresholds_crossed"].tolist())
        self.assertEqual(thresholds_crossed, {"300", "300,200,150"})

    def test_threshold_summary_aggregates_from_canonical_events(self):
        ma = MarginAnalysis(self._make_snapshots())
        events = ma.events_df([300.0, 200.0, 150.0, 100.0])
        summary = ma.threshold_summary_df(events, [300.0, 200.0, 150.0, 100.0]).set_index("threshold")

        self.assertEqual(int(summary.loc[300.0, "event_count"]), 2)
        self.assertEqual(int(summary.loc[200.0, "event_count"]), 1)
        self.assertEqual(int(summary.loc[150.0, "event_count"]), 1)
        self.assertEqual(int(summary.loc[100.0, "event_count"]), 0)

        # Episode durations: first (00:10 -> 00:40) = 30 min, second (01:00 -> 01:10) = 10 min.
        self.assertEqual(int(summary.loc[300.0, "total_minutes_below"]), 40)
        self.assertEqual(int(summary.loc[200.0, "total_minutes_below"]), 30)
        self.assertEqual(int(summary.loc[150.0, "longest_event_minutes_below"]), 30)

    def test_portfolio_summary_uses_threshold_aggregates(self):
        ma = MarginAnalysis(self._make_snapshots())
        summary = ma.portfolio_summary_dict(ma.events_df([300.0, 200.0, 150.0, 100.0]))

        self.assertEqual(summary["event_count_below_300"], 2)
        self.assertEqual(summary["event_count_below_200"], 1)
        self.assertEqual(summary["event_count_below_150"], 1)
        self.assertEqual(summary["event_count_below_100"], 0)


if __name__ == "__main__":
    unittest.main()
