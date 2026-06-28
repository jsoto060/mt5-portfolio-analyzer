import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath("src"))

from mt5_portfolio_analyzer import (  # noqa: E402
    CurvePoint,
    DealEvent,
    PairConfig,
    PairData,
    PortfolioSimulator,
    ScalingConfig,
    TradeEvent,
)
from mt5_readers import discover_files  # noqa: E402


class SimulationTests(unittest.TestCase):
    def _single_pair(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        return PairData(
            config=PairConfig(name="EURUSD", risk_percent=1.0, base_lot=None),
            deals=[DealEvent(time=start + timedelta(minutes=5), pair="EURUSD", net_profit=100.0, volume=1.0)],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1),
                TradeEvent(time=start + timedelta(minutes=5), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1),
            ],
            curve=[
                CurvePoint(time=start, balance=1000.0, equity=1000.0),
                CurvePoint(time=start + timedelta(minutes=5), balance=1100.0, equity=1100.0),
            ],
            baseline_volume_median=1.0,
            market_times=[start, start + timedelta(minutes=5)],
            market_close=[1.1, 1.1],
        )

    def test_pairdata_interpolation_uses_cached_arrays(self):
        pair = self._single_pair()
        mid = pair.curve[0].time + timedelta(minutes=2, seconds=30)
        val = pair.interpolate_floating(mid)
        self.assertAlmostEqual(val, 0.0, places=8)
        self.assertEqual(len(pair.curve_times), 2)
        self.assertEqual(len(pair.curve_floating), 2)

    def test_duplicate_pair_names_raise(self):
        pair = self._single_pair()
        with self.assertRaises(ValueError):
            PortfolioSimulator(
                pairs_data=[pair, pair],
                initial_balance=1000.0,
                scaling=ScalingConfig(1.0, 0.1, 5.0),
            )

    def test_smoke_run_simulation(self):
        pair = self._single_pair()
        sim = PortfolioSimulator(
            pairs_data=[pair],
            initial_balance=1000.0,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={"EURUSD": 1.0},
        )
        result = sim.run()
        self.assertIn("summary", result)
        self.assertIn("event_rows", result)
        self.assertIn("curve_rows", result)
        self.assertGreater(result["summary"]["final_balance"], 1000.0)

    def test_floating_uses_entry_scale_not_current_balance_scale(self):
        start = datetime(2026, 1, 1, 0, 0, 0)

        # Pair A: one open position; standalone curve has constant floating -100 while open.
        pair_a = PairData(
            config=PairConfig(name="EURUSD", risk_percent=100.0, base_lot=None),
            deals=[DealEvent(time=start + timedelta(minutes=15), pair="EURUSD", net_profit=-100.0, volume=1.0)],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1),
                TradeEvent(time=start + timedelta(minutes=15), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1),
            ],
            curve=[
                CurvePoint(time=start, balance=1000.0, equity=900.0),
                CurvePoint(time=start + timedelta(minutes=10), balance=1000.0, equity=900.0),
                CurvePoint(time=start + timedelta(minutes=15), balance=900.0, equity=900.0),
            ],
            baseline_volume_median=1.0,
            market_times=[start, start + timedelta(minutes=10), start + timedelta(minutes=15)],
            market_close=[1.1, 1.0990, 1.1],
        )

        # Pair B: closes a profitable trade while Pair A remains open, raising portfolio balance.
        pair_b = PairData(
            config=PairConfig(name="GBPUSD", risk_percent=100.0, base_lot=None),
            deals=[DealEvent(time=start + timedelta(minutes=5), pair="GBPUSD", net_profit=500.0, volume=1.0)],
            trades=[
                TradeEvent(time=start + timedelta(minutes=1), pair="GBPUSD", direction="in", side="buy", volume=1.0, price=1.2),
                TradeEvent(time=start + timedelta(minutes=5), pair="GBPUSD", direction="out", side="buy", volume=1.0, price=1.2),
            ],
            curve=[
                CurvePoint(time=start, balance=1000.0, equity=1000.0),
                CurvePoint(time=start + timedelta(minutes=10), balance=1500.0, equity=1500.0),
                CurvePoint(time=start + timedelta(minutes=15), balance=1500.0, equity=1500.0),
            ],
            baseline_volume_median=1.0,
            market_times=[start, start + timedelta(minutes=10), start + timedelta(minutes=15)],
            market_close=[1.2, 1.2, 1.2],
        )

        sim = PortfolioSimulator(
            pairs_data=[pair_a, pair_b],
            initial_balance=1000.0,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={"EURUSD": 1.0, "GBPUSD": 1.0},
        )
        result = sim.run()

        # At t+10m, Pair A is still open and baseline floating remains -100.
        # Correct behavior: floating should stay around -100 (entry scale frozen at 1x).
        row_10m = next(r for r in result["curve_rows"] if r["time"] == "2026.01.01 00:10")
        # Price moved from 1.1000 to 1.0990 on a 1-lot long => about -100 USD floating.
        self.assertAlmostEqual(float(row_10m["floating_pnl"]), -100.0, places=4)


class ReaderDiscoveryTests(unittest.TestCase):
    def test_duplicate_file_match_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "testergraph.report.1_eurusd.csv"), "w", encoding="utf-8").close()
            open(os.path.join(tmp, "testergraph.report.2_eurusd.csv"), "w", encoding="utf-8").close()
            with self.assertRaises(ValueError):
                discover_files(tmp)


if __name__ == "__main__":
    unittest.main()
