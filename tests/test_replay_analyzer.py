import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("src"))

import comparison  # noqa: E402
from replay_analyzer import default_analyzer_for_repo  # noqa: E402


class ReplayAnalyzerFacadeTests(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.abspath(".")
        self.analyzer = default_analyzer_for_repo(self.repo)

    def test_replay_folder_returns_tables_and_metrics(self):
        baseline_folder = os.path.join(self.repo, "data", "baseline")
        result = self.analyzer.replay_folder(baseline_folder)

        summary_df = result.summary()
        config_df = result.configuration()
        pair_df = result.pair_table()

        self.assertFalse(summary_df.empty)
        self.assertFalse(config_df.empty)
        self.assertFalse(pair_df.empty)
        self.assertIn("Final Balance", summary_df.columns)
        self.assertIn("Pair", config_df.columns)
        self.assertIn("Pair", pair_df.columns)

    def test_compare_folders_returns_expected_table_set(self):
        baseline_folder = os.path.join(self.repo, "data", "baseline")
        proposed_folder = os.path.join(self.repo, "data", "proposed")
        out = self.analyzer.compare_folders(baseline_folder, proposed_folder)

        self.assertIn("tables", out)
        self.assertIn("charts", out)
        self.assertIn("performance", out["tables"])
        self.assertIn("equity_comparison", out["charts"])

    def test_scenario_max_trades_overrides_flow_to_per_pair_comparison(self):
        baseline_folder = os.path.join(self.repo, "data", "baseline")
        overrides = {
            "GBPUSD": {"max_trades": 3},
            "EURUSD": {"max_trades": 4},
        }

        baseline = self.analyzer.replay_folder(baseline_folder, name="baseline")
        scenario = self.analyzer.replay_scenario(baseline_folder, overrides)
        per_pair = comparison.compare_results(baseline, scenario)["per_pair"].set_index("Pair")

        self.assertEqual(int(per_pair.loc["GBPUSD", "Max Trades (Proposed)"]), 3)
        self.assertEqual(int(per_pair.loc["EURUSD", "Max Trades (Proposed)"]), 4)

    def test_lower_max_trades_reduces_proposed_trade_count(self):
        baseline_folder = os.path.join(self.repo, "data", "baseline")
        overrides = {
            "GBPUSD": {"max_trades": 1},
        }

        baseline = self.analyzer.replay_folder(baseline_folder, name="baseline")
        scenario = self.analyzer.replay_scenario(baseline_folder, overrides)
        per_pair = comparison.compare_results(baseline, scenario)["per_pair"].set_index("Pair")

        baseline_trades = int(per_pair.loc["GBPUSD", "Trades (Baseline)"])
        proposed_trades = int(per_pair.loc["GBPUSD", "Trades (Proposed)"])

        self.assertLess(proposed_trades, baseline_trades)


if __name__ == "__main__":
    unittest.main()
