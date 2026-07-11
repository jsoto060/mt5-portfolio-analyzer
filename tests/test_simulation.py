import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("src"))

from mt5_portfolio_analyzer import (  # noqa: E402
    BaselineConfig,
    CurvePoint,
    DealEvent,
    load_pair,
    infer_baseline_config,
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
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=1.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=1,
            ),
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
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=1,
            ),
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
            name="GBPUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=1,
            ),
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

    def test_margin_uses_market_price_and_closes_to_zero(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        pair = PairData(
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=1,
            ),
            deals=[DealEvent(time=start + timedelta(minutes=10), pair="EURUSD", net_profit=5000.0, volume=1.0)],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.0),
                TradeEvent(time=start + timedelta(minutes=10), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.05),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[start, start + timedelta(minutes=5), start + timedelta(minutes=10)],
            market_close=[1.0, 1.1, 1.05],
        )

        sim = PortfolioSimulator(
            pairs_data=[pair],
            initial_balance=1000.0,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={"EURUSD": 2.0},
        )
        result = sim.run()

        row_open = next(r for r in result["curve_rows"] if r["time"] == "2026.01.01 00:00")
        row_mid = next(r for r in result["curve_rows"] if r["time"] == "2026.01.01 00:05")
        row_close = next(r for r in result["curve_rows"] if r["time"] == "2026.01.01 00:10")

        self.assertAlmostEqual(float(row_open["used_margin"]), 2000.0, places=4)
        self.assertAlmostEqual(float(row_mid["used_margin"]), 2200.0, places=4)
        self.assertEqual(float(row_close["used_margin"]), 0.0)
        self.assertAlmostEqual(float(row_mid["free_margin"]), float(row_mid["equity"]) - 2200.0, places=4)
        self.assertAlmostEqual(float(row_mid["margin_level_percent"]), float(row_mid["equity"]) / 2200.0 * 100.0, places=4)

    def test_missing_margin_requirements_fail_fast(self):
        pair = self._single_pair()
        with self.assertRaises(ValueError):
            PortfolioSimulator(
                pairs_data=[pair],
                initial_balance=1000.0,
                scaling=ScalingConfig(1.0, 0.1, 5.0),
                margin_requirements={},
            )


class ReaderDiscoveryTests(unittest.TestCase):
    def test_duplicate_file_match_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "testergraph.report.1_eurusd.csv"), "w", encoding="utf-8").close()
            open(os.path.join(tmp, "testergraph.report.2_eurusd.csv"), "w", encoding="utf-8").close()
            with self.assertRaises(ValueError):
                discover_files(tmp)


class PairLoadingTests(unittest.TestCase):
    def test_load_pair_includes_non_zero_in_commission_events(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        raw_deals = [
            SimpleNamespace(
                time=start,
                direction="in",
                side="buy",
                volume=1.0,
                price=1.1000,
                profit=0.0,
                commission=-2.5,
                swap=0.0,
                balance=0.0,
            ),
            SimpleNamespace(
                time=start + timedelta(minutes=5),
                direction="out",
                side="sell",
                volume=1.0,
                price=1.1010,
                profit=10.0,
                commission=-2.5,
                swap=0.0,
                balance=10007.5,
            ),
        ]
        baseline = BaselineConfig(
            risk_percent=1.0,
            take_profit=None,
            grid_size=None,
            max_trades=1,
            initial_balance=1000.0,
            first_lot=1.0,
            median_lot=1.0,
            trade_count=1,
        )

        with patch("mt5_portfolio_analyzer.load_xlsx_deals", return_value=(raw_deals, 1000.0)):
            with patch("mt5_portfolio_analyzer.infer_baseline_config", return_value=(baseline, None)):
                pair = load_pair(name="EURUSD", xlsx_path="dummy.xlsx")

        self.assertEqual(len(pair.deals), 2)
        self.assertAlmostEqual(pair.deals[0].net_profit, -2.5, places=8)
        self.assertAlmostEqual(pair.deals[1].net_profit, 7.5, places=8)
        self.assertAlmostEqual(pair.baseline_volume_median or 0.0, 1.0, places=8)


class BaselineInferenceTests(unittest.TestCase):
    def test_infers_risk_tp_max_trades_and_lot_stats(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        raw_deals = [
            SimpleNamespace(time=start, direction="in", side="buy", volume=0.10, price=1.1000, profit=0.0, commission=0.0, swap=0.0, balance=0.0),
            SimpleNamespace(time=start + timedelta(minutes=1), direction="in", side="buy", volume=0.10, price=1.0950, profit=0.0, commission=0.0, swap=0.0, balance=0.0),
            SimpleNamespace(time=start + timedelta(minutes=5), direction="out", side="sell", volume=0.10, price=1.1015, profit=15.0, commission=0.0, swap=0.0, balance=10015.0),
            SimpleNamespace(time=start + timedelta(minutes=6), direction="out", side="sell", volume=0.10, price=1.1015, profit=15.0, commission=0.0, swap=0.0, balance=10030.0),
        ]
        baseline, risk_std = infer_baseline_config(raw_deals, initial_balance=10000.0, pair="EURUSD")

        self.assertAlmostEqual(baseline.risk_percent, 1.0, places=6)
        self.assertEqual(baseline.max_trades, 2)
        self.assertEqual(baseline.first_lot, 0.10)
        self.assertEqual(baseline.median_lot, 0.10)
        self.assertEqual(baseline.trade_count, 2)
        self.assertEqual(baseline.grid_size, 50)
        self.assertTrue(baseline.take_profit is not None and baseline.take_profit > 0)
        self.assertAlmostEqual(risk_std or 0.0, 0.0, places=8)

    def test_infers_risk_std_for_variable_position_sizing(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        raw_deals = [
            SimpleNamespace(time=start, direction="in", side="buy", volume=0.10, price=1.1000, profit=0.0, commission=0.0, swap=0.0, balance=0.0),
            SimpleNamespace(time=start + timedelta(minutes=1), direction="out", side="sell", volume=0.10, price=1.1010, profit=10.0, commission=0.0, swap=0.0, balance=10010.0),
            SimpleNamespace(time=start + timedelta(minutes=2), direction="in", side="buy", volume=0.30, price=1.1000, profit=0.0, commission=0.0, swap=0.0, balance=0.0),
            SimpleNamespace(time=start + timedelta(minutes=3), direction="out", side="sell", volume=0.30, price=1.1010, profit=30.0, commission=0.0, swap=0.0, balance=10040.0),
        ]
        _, risk_std = infer_baseline_config(raw_deals, initial_balance=10000.0, pair="EURUSD")
        self.assertTrue(risk_std is not None and risk_std > 0.05)


if __name__ == "__main__":
    unittest.main()
