import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("src"))

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


if __name__ == "__main__":
    unittest.main()
