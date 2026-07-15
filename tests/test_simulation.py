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
from scenario import apply_scenario_overrides  # noqa: E402
from swap_engine import SwapEngine, load_swap_rates_yaml  # noqa: E402


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


class ReplayOrderingDeterminismTests(unittest.TestCase):
    def _pair_with_deals(self, name: str, deals):
        return PairData(
            name=name,
            baseline_config=BaselineConfig(
                risk_percent=1.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=len(deals),
            ),
            deals=deals,
            trades=[],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

    def _run(self, pairs):
        sim = PortfolioSimulator(
            pairs_data=pairs,
            initial_balance=1000.0,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={pair.name: 1.0 for pair in pairs},
        )
        return sim.run()

    def test_two_pairs_same_timestamp_are_deterministic(self):
        ts = datetime(2026, 1, 1, 0, 0, 0)
        eurusd = self._pair_with_deals(
            "EURUSD",
            [DealEvent(time=ts, pair="EURUSD", net_profit=10.0, volume=1.0, sequence_in_pair=0)],
        )
        gbpusd = self._pair_with_deals(
            "GBPUSD",
            [DealEvent(time=ts, pair="GBPUSD", net_profit=20.0, volume=1.0, sequence_in_pair=0)],
        )

        result_a = self._run([eurusd, gbpusd])
        result_b = self._run([gbpusd, eurusd])

        self.assertEqual(result_a, result_b)
        self.assertEqual([row["pair"] for row in result_a["event_rows"]], ["EURUSD", "GBPUSD"])

    def test_three_or_more_pairs_same_timestamp_are_deterministic(self):
        ts = datetime(2026, 1, 1, 0, 0, 0)
        eurgbp = self._pair_with_deals(
            "EURGBP",
            [DealEvent(time=ts, pair="EURGBP", net_profit=5.0, volume=1.0, sequence_in_pair=0)],
        )
        eurusd = self._pair_with_deals(
            "EURUSD",
            [DealEvent(time=ts, pair="EURUSD", net_profit=10.0, volume=1.0, sequence_in_pair=0)],
        )
        usdcad = self._pair_with_deals(
            "USDCAD",
            [DealEvent(time=ts, pair="USDCAD", net_profit=-2.0, volume=1.0, sequence_in_pair=0)],
        )

        base = self._run([usdcad, eurusd, eurgbp])
        alt = self._run([eurusd, eurgbp, usdcad])
        alt2 = self._run([eurgbp, usdcad, eurusd])

        self.assertEqual(base, alt)
        self.assertEqual(base, alt2)
        self.assertEqual([row["pair"] for row in base["event_rows"]], ["EURGBP", "EURUSD", "USDCAD"])

    def test_same_pair_same_timestamp_preserves_mt5_sequence(self):
        ts = datetime(2026, 1, 1, 0, 0, 0)
        eurusd = self._pair_with_deals(
            "EURUSD",
            [
                DealEvent(time=ts, pair="EURUSD", net_profit=11.0, volume=1.0, sequence_in_pair=0),
                DealEvent(time=ts, pair="EURUSD", net_profit=22.0, volume=1.0, sequence_in_pair=1),
                DealEvent(time=ts, pair="EURUSD", net_profit=33.0, volume=1.0, sequence_in_pair=2),
            ],
        )
        gbpusd = self._pair_with_deals(
            "GBPUSD",
            [DealEvent(time=ts, pair="GBPUSD", net_profit=44.0, volume=1.0, sequence_in_pair=0)],
        )

        result = self._run([gbpusd, eurusd])
        eur_rows = [row for row in result["event_rows"] if row["pair"] == "EURUSD"]

        self.assertEqual([row["baseline_net_profit"] for row in eur_rows], [11.0, 22.0, 33.0])
        self.assertEqual([row["pair"] for row in result["event_rows"]], ["EURUSD", "EURUSD", "EURUSD", "GBPUSD"])

    def test_replay_results_are_identical_for_different_discovery_orders(self):
        ts = datetime(2026, 1, 1, 0, 0, 0)
        pairs = [
            self._pair_with_deals("EURUSD", [DealEvent(time=ts, pair="EURUSD", net_profit=1.0, volume=1.0, sequence_in_pair=0)]),
            self._pair_with_deals("GBPUSD", [DealEvent(time=ts, pair="GBPUSD", net_profit=2.0, volume=1.0, sequence_in_pair=0)]),
            self._pair_with_deals("USDCHF", [DealEvent(time=ts, pair="USDCHF", net_profit=3.0, volume=1.0, sequence_in_pair=0)]),
            self._pair_with_deals("EURGBP", [DealEvent(time=ts, pair="EURGBP", net_profit=4.0, volume=1.0, sequence_in_pair=0)]),
        ]

        result_a = self._run([pairs[0], pairs[1], pairs[2], pairs[3]])
        result_b = self._run([pairs[3], pairs[2], pairs[1], pairs[0]])
        result_c = self._run([pairs[1], pairs[3], pairs[0], pairs[2]])

        self.assertEqual(result_a, result_b)
        self.assertEqual(result_a, result_c)


class ReconstructedLotReplayTests(unittest.TestCase):
    def _run(self, pairs, initial_balance=100.0):
        sim = PortfolioSimulator(
            pairs_data=pairs,
            initial_balance=initial_balance,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={pair.name: 1.0 for pair in pairs},
        )
        return sim.run()

    def test_multiple_positions_close_with_lifo_lots(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        pair = PairData(
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=3,
                initial_balance=150.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=3,
            ),
            deals=[
                DealEvent(time=start, pair="EURUSD", net_profit=66.6667, volume=1.0, direction="in", sequence_in_pair=0),
                DealEvent(time=start + timedelta(minutes=1), pair="EURUSD", net_profit=62.5, volume=1.0, direction="in", sequence_in_pair=1),
                DealEvent(time=start + timedelta(minutes=2), pair="EURUSD", net_profit=0.0, volume=1.0, direction="in", sequence_in_pair=2),
                DealEvent(time=start + timedelta(minutes=10), pair="EURUSD", net_profit=10.0, volume=1.0, direction="out", sequence_in_pair=3),
                DealEvent(time=start + timedelta(minutes=11), pair="EURUSD", net_profit=10.0, volume=1.0, direction="out", sequence_in_pair=4),
                DealEvent(time=start + timedelta(minutes=12), pair="EURUSD", net_profit=10.0, volume=1.0, direction="out", sequence_in_pair=5),
            ],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(minutes=1), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=start + timedelta(minutes=2), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1002, sequence_in_pair=2),
                TradeEvent(time=start + timedelta(minutes=10), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1003, sequence_in_pair=3),
                TradeEvent(time=start + timedelta(minutes=11), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1004, sequence_in_pair=4),
                TradeEvent(time=start + timedelta(minutes=12), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1005, sequence_in_pair=5),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        result = self._run([pair], initial_balance=150.0)
        out_rows = [row for row in result["event_rows"] if row["baseline_net_profit"] == 10.0]

        self.assertEqual([row["scaled_volume"] for row in out_rows], [0.17, 0.16, 0.15])

    def test_out_uses_entry_lot_even_after_other_pair_changes_balance(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        eurusd = PairData(
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=100.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=1,
            ),
            deals=[
                DealEvent(time=start + timedelta(minutes=2), pair="EURUSD", net_profit=100.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(minutes=2), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1002, sequence_in_pair=1),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )
        gbpusd = PairData(
            name="GBPUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=1,
                initial_balance=100.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=1,
            ),
            deals=[
                DealEvent(time=start + timedelta(minutes=1), pair="GBPUSD", net_profit=100.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
            trades=[
                TradeEvent(time=start, pair="GBPUSD", direction="in", side="buy", volume=1.0, price=1.2000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(minutes=1), pair="GBPUSD", direction="out", side="buy", volume=1.0, price=1.2002, sequence_in_pair=1),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        result = self._run([eurusd, gbpusd], initial_balance=100.0)
        eur_row = next(row for row in result["event_rows"] if row["pair"] == "EURUSD")

        self.assertEqual(eur_row["scaled_volume"], 0.1)
        self.assertEqual(eur_row["scaled_net_profit"], 10.0)

    def test_same_timestamp_out_deals_use_lifo(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        close_ts = start + timedelta(minutes=10)
        pair = PairData(
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=3,
                initial_balance=150.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=3,
            ),
            deals=[
                DealEvent(time=start, pair="EURUSD", net_profit=66.6667, volume=1.0, direction="in", sequence_in_pair=0),
                DealEvent(time=start + timedelta(minutes=1), pair="EURUSD", net_profit=62.5, volume=1.0, direction="in", sequence_in_pair=1),
                DealEvent(time=start + timedelta(minutes=2), pair="EURUSD", net_profit=0.0, volume=1.0, direction="in", sequence_in_pair=2),
                DealEvent(time=close_ts, pair="EURUSD", net_profit=10.0, volume=1.0, direction="out", sequence_in_pair=3),
                DealEvent(time=close_ts, pair="EURUSD", net_profit=10.0, volume=1.0, direction="out", sequence_in_pair=4),
                DealEvent(time=close_ts, pair="EURUSD", net_profit=10.0, volume=1.0, direction="out", sequence_in_pair=5),
            ],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(minutes=1), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=start + timedelta(minutes=2), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1002, sequence_in_pair=2),
                TradeEvent(time=close_ts, pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1003, sequence_in_pair=3),
                TradeEvent(time=close_ts, pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1004, sequence_in_pair=4),
                TradeEvent(time=close_ts, pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1005, sequence_in_pair=5),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        result = self._run([pair], initial_balance=150.0)
        out_rows = [row for row in result["event_rows"] if row["time"] == close_ts.strftime("%Y.%m.%d %H:%M:%S")]

        self.assertEqual([row["scaled_volume"] for row in out_rows], [0.17, 0.16, 0.15])

    def test_replay_is_invariant_across_repeated_runs(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        pair = PairData(
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=100.0,
                take_profit=None,
                grid_size=None,
                max_trades=2,
                initial_balance=100.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=2,
            ),
            deals=[
                DealEvent(time=start, pair="EURUSD", net_profit=0.0, volume=1.0, direction="in", sequence_in_pair=0),
                DealEvent(time=start + timedelta(minutes=1), pair="EURUSD", net_profit=0.0, volume=1.0, direction="in", sequence_in_pair=1),
                DealEvent(time=start + timedelta(minutes=2), pair="EURUSD", net_profit=50.0, volume=1.0, direction="out", sequence_in_pair=2),
                DealEvent(time=start + timedelta(minutes=3), pair="EURUSD", net_profit=50.0, volume=1.0, direction="out", sequence_in_pair=3),
            ],
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(minutes=1), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=start + timedelta(minutes=2), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1002, sequence_in_pair=2),
                TradeEvent(time=start + timedelta(minutes=3), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1003, sequence_in_pair=3),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        result_a = self._run([pair], initial_balance=100.0)
        result_b = self._run([pair], initial_balance=100.0)

        out_a = [row["scaled_volume"] for row in result_a["event_rows"] if row["baseline_net_profit"] == 50.0]
        out_b = [row["scaled_volume"] for row in result_b["event_rows"] if row["baseline_net_profit"] == 50.0]

        self.assertEqual(out_a, out_b)
        self.assertEqual(result_a["summary"]["final_balance"], result_b["summary"]["final_balance"])
        self.assertEqual(result_a, result_b)


class ScenarioFilteringTests(unittest.TestCase):
    def test_max_trades_filter_matches_out_events_with_side_aware_fifo(self):
        start = datetime(2026, 1, 1, 0, 0, 0)
        pair = PairData(
            name="EURUSD",
            baseline_config=BaselineConfig(
                risk_percent=1.0,
                take_profit=None,
                grid_size=None,
                max_trades=2,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=2,
            ),
            deals=[
                DealEvent(time=start + timedelta(minutes=2), pair="EURUSD", net_profit=10.0, volume=1.0, sequence_in_pair=2),
                DealEvent(time=start + timedelta(minutes=3), pair="EURUSD", net_profit=20.0, volume=1.0, sequence_in_pair=3),
            ],
            trades=[
                TradeEvent(time=start + timedelta(minutes=0), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(minutes=1), pair="EURUSD", direction="in", side="sell", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=start + timedelta(minutes=2), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1002, sequence_in_pair=2),
                TradeEvent(time=start + timedelta(minutes=3), pair="EURUSD", direction="out", side="sell", volume=1.0, price=1.1003, sequence_in_pair=3),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        scen = apply_scenario_overrides([pair], {"EURUSD": {"max_trades": 1}})[0]

        kept_trade_seq = [int(t.sequence_in_pair) for t in scen.trades]
        kept_deal_seq = [int(d.sequence_in_pair) for d in scen.deals]

        self.assertEqual(kept_trade_seq, [0, 3])
        self.assertEqual(kept_deal_seq, [3])


class ReplayLedgerContractTest(unittest.TestCase):
    def test_explicit_event_ordering_contract_with_ties(self):
        """Ledger ordering contract regression guard.

        Expected ordering key:
        1) Timestamp
        2) Pair
        3) MT5 sequence
        4) Event type rank (swap before deal)
        """
        start = datetime(2026, 1, 5, 10, 0, 0)
        tied_ts = datetime(2026, 1, 6, 0, 0, 0)

        # Commission is represented as deal_in with non-zero net on entry.
        eurusd = PairData(
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
            deals=[
                DealEvent(
                    time=start,
                    pair="EURUSD",
                    net_profit=-10.0,
                    volume=1.0,
                    direction="in",
                    sequence_in_pair=0,
                ),
                # Same timestamp/pair/sequence as swap -> event type rank decides.
                DealEvent(
                    time=tied_ts,
                    pair="EURUSD",
                    net_profit=50.0,
                    volume=1.0,
                    direction="out",
                    sequence_in_pair=0,
                ),
            ],
            trades=[
                TradeEvent(
                    time=start,
                    pair="EURUSD",
                    direction="in",
                    side="buy",
                    volume=1.0,
                    price=1.1000,
                    sequence_in_pair=0,
                ),
                TradeEvent(
                    time=tied_ts,
                    pair="EURUSD",
                    direction="out",
                    side="buy",
                    volume=1.0,
                    price=1.1001,
                    sequence_in_pair=0,
                ),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        gbpusd = PairData(
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
            deals=[
                DealEvent(
                    time=tied_ts,
                    pair="GBPUSD",
                    net_profit=20.0,
                    volume=1.0,
                    direction="out",
                    sequence_in_pair=0,
                ),
            ],
            trades=[
                TradeEvent(
                    time=start,
                    pair="GBPUSD",
                    direction="in",
                    side="buy",
                    volume=1.0,
                    price=1.2000,
                    sequence_in_pair=0,
                ),
                TradeEvent(
                    time=tied_ts,
                    pair="GBPUSD",
                    direction="out",
                    side="buy",
                    volume=1.0,
                    price=1.2002,
                    sequence_in_pair=0,
                ),
            ],
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

        sim = PortfolioSimulator(
            pairs_data=[gbpusd, eurusd],
            initial_balance=1000.0,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={"EURUSD": 1.0, "GBPUSD": 1.0},
            swap_engine=SwapEngine({
                "EURUSD": {"buy": -0.1, "sell": -0.1},
                "GBPUSD": {"buy": -0.1, "sell": -0.1},
            }),
        )
        result = sim.run()

        rows = [
            (row["time"], row["pair"], row["EventType"]) for row in result["event_rows"]
        ]

        expected = [
            ("2026.01.05 10:00:00", "EURUSD", "deal_in"),
            ("2026.01.06 00:00:00", "EURUSD", "swap"),
            ("2026.01.06 00:00:00", "EURUSD", "deal_out"),
            ("2026.01.06 00:00:00", "GBPUSD", "swap"),
            ("2026.01.06 00:00:00", "GBPUSD", "deal_out"),
        ]
        self.assertEqual(rows, expected)


class SwapReplayTests(unittest.TestCase):
    def _make_pair(
        self,
        name: str,
        risk_percent: float,
        trades,
        deals,
    ):
        return PairData(
            name=name,
            baseline_config=BaselineConfig(
                risk_percent=risk_percent,
                take_profit=None,
                grid_size=None,
                max_trades=10,
                initial_balance=1000.0,
                first_lot=1.0,
                median_lot=1.0,
                trade_count=len([t for t in trades if t.direction == "out"]),
            ),
            deals=deals,
            trades=trades,
            curve=[],
            baseline_volume_median=1.0,
            market_times=[],
            market_close=[],
        )

    def _run_with_swap(self, pairs, rates):
        sim = PortfolioSimulator(
            pairs_data=pairs,
            initial_balance=1000.0,
            scaling=ScalingConfig(1.0, 0.1, 5.0),
            margin_requirements={pair.name: 1.0 for pair in pairs},
            swap_engine=SwapEngine(rates),
        )
        return sim.run()

    def test_load_swap_rates_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "swap_rates.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "broker: TestBroker\n"
                    "symbols:\n"
                    "  EURUSD:\n"
                    "    buy:\n"
                    "      daily_swap_per_001_lot: -0.100\n"
                    "    sell:\n"
                    "      daily_swap_per_001_lot: -0.200\n"
                )

            loaded = load_swap_rates_yaml(path)
            self.assertAlmostEqual(loaded["EURUSD"]["buy"], -0.1, places=8)
            self.assertAlmostEqual(loaded["EURUSD"]["sell"], -0.2, places=8)

    def test_one_day_position_generates_one_swap_event(self):
        start = datetime(2026, 1, 5, 10, 0, 0)  # Monday
        pair = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(days=1), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
            ],
            deals=[
                DealEvent(time=start + timedelta(days=1), pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
        )

        result = self._run_with_swap(
            [pair],
            {"EURUSD": {"buy": -0.1, "sell": -0.1}},
        )
        swap_rows = [r for r in result["event_rows"] if r["EventType"] == "swap"]
        self.assertEqual(len(swap_rows), 1)
        self.assertAlmostEqual(float(swap_rows[0]["ModeledSwap"]), -10.0, places=6)
        self.assertIn("2026-01-05", swap_rows[0]["description"])

    def test_three_day_and_weekend_positions_generate_expected_swap_days(self):
        friday = datetime(2026, 1, 9, 10, 0, 0)  # Friday
        monday = datetime(2026, 1, 12, 10, 0, 0)  # Monday
        pair = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=friday, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=monday, pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
            ],
            deals=[
                DealEvent(time=monday, pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
        )

        result = self._run_with_swap(
            [pair],
            {"EURUSD": {"buy": -0.1, "sell": -0.1}},
        )
        swap_rows = [r for r in result["event_rows"] if r["EventType"] == "swap"]
        self.assertEqual(len(swap_rows), 3)
        self.assertAlmostEqual(sum(float(r["ModeledSwap"]) for r in swap_rows), -30.0, places=6)
        accrual_days = [r["description"].rsplit(" ", 1)[-1] for r in swap_rows]
        self.assertEqual(accrual_days, ["2026-01-09", "2026-01-10", "2026-01-11"])
        self.assertNotIn("2026-01-12", accrual_days)

    def test_monday_to_thursday_generates_exactly_monday_to_wednesday_swaps(self):
        monday = datetime(2026, 1, 5, 10, 0, 0)
        thursday = datetime(2026, 1, 8, 10, 0, 0)
        pair = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=monday, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=thursday, pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
            ],
            deals=[
                DealEvent(time=thursday, pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
        )

        result = self._run_with_swap(
            [pair],
            {"EURUSD": {"buy": -0.1, "sell": -0.1}},
        )
        swap_rows = [r for r in result["event_rows"] if r["EventType"] == "swap"]
        self.assertEqual(len(swap_rows), 3)
        accrual_days = [r["description"].rsplit(" ", 1)[-1] for r in swap_rows]
        self.assertEqual(accrual_days, ["2026-01-05", "2026-01-06", "2026-01-07"])
        self.assertNotIn("2026-01-08", accrual_days)

    def test_buy_and_sell_rates_are_applied(self):
        start = datetime(2026, 1, 5, 10, 0, 0)
        pair = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(days=1), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=start + timedelta(days=1, hours=1), pair="EURUSD", direction="in", side="sell", volume=1.0, price=1.1001, sequence_in_pair=2),
                TradeEvent(time=start + timedelta(days=2, hours=1), pair="EURUSD", direction="out", side="sell", volume=1.0, price=1.1000, sequence_in_pair=3),
            ],
            deals=[
                DealEvent(time=start + timedelta(days=1), pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
                DealEvent(time=start + timedelta(days=2, hours=1), pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=3),
            ],
        )

        result = self._run_with_swap(
            [pair],
            {"EURUSD": {"buy": -0.1, "sell": -0.2}},
        )

        swap_rows = [r for r in result["event_rows"] if r["EventType"] == "swap"]
        self.assertEqual(len(swap_rows), 2)
        by_direction = {r["direction"]: float(r["ModeledSwap"]) for r in swap_rows}
        self.assertAlmostEqual(by_direction["buy"], -10.0, places=6)
        self.assertAlmostEqual(by_direction["sell"], -19.8, places=6)

    def test_multiple_positions_and_pairs_generate_swap_events(self):
        start = datetime(2026, 1, 5, 10, 0, 0)
        close = start + timedelta(days=2)
        eurusd = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(hours=1), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=close, pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1002, sequence_in_pair=2),
                TradeEvent(time=close + timedelta(hours=1), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1003, sequence_in_pair=3),
            ],
            deals=[
                DealEvent(time=close, pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=2),
                DealEvent(time=close + timedelta(hours=1), pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=3),
            ],
        )
        gbpusd = self._make_pair(
            "GBPUSD",
            100.0,
            trades=[
                TradeEvent(time=start, pair="GBPUSD", direction="in", side="sell", volume=1.0, price=1.2500, sequence_in_pair=0),
                TradeEvent(time=close, pair="GBPUSD", direction="out", side="sell", volume=1.0, price=1.2490, sequence_in_pair=1),
            ],
            deals=[
                DealEvent(time=close, pair="GBPUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
        )

        result = self._run_with_swap(
            [eurusd, gbpusd],
            {
                "EURUSD": {"buy": -0.1, "sell": -0.1},
                "GBPUSD": {"buy": -0.1, "sell": -0.2},
            },
        )
        swap_rows = [r for r in result["event_rows"] if r["EventType"] == "swap"]
        self.assertGreaterEqual(len(swap_rows), 5)
        self.assertTrue(any(r["pair"] == "EURUSD" for r in swap_rows))
        self.assertTrue(any(r["pair"] == "GBPUSD" for r in swap_rows))

    def test_swap_updates_balance_before_subsequent_in_lot_sizing(self):
        start = datetime(2026, 1, 5, 10, 0, 0)
        pair = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(days=1, hours=10), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
                TradeEvent(time=start + timedelta(days=1, hours=12), pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1002, sequence_in_pair=2),
                TradeEvent(time=start + timedelta(days=1, hours=13), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1003, sequence_in_pair=3),
            ],
            deals=[
                DealEvent(time=start + timedelta(days=1, hours=10), pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
                DealEvent(time=start + timedelta(days=1, hours=13), pair="EURUSD", net_profit=100.0, volume=1.0, direction="out", sequence_in_pair=3),
            ],
        )

        result = self._run_with_swap(
            [pair],
            {"EURUSD": {"buy": -0.1, "sell": -0.1}},
        )

        first_swap = next(r for r in result["event_rows"] if r["EventType"] == "swap")
        self.assertAlmostEqual(float(first_swap["BalanceAfterEvent"]), 990.0, places=6)

        second_out = [r for r in result["event_rows"] if r["EventType"] == "deal_out" and float(r["baseline_net_profit"]) == 100.0][0]
        # Balance after swap is 990, so risk 100% produces lot round(990/1000, 2)=0.99.
        self.assertAlmostEqual(float(second_out["scaled_volume"]), 0.99, places=6)

    def test_swap_events_are_deterministic(self):
        start = datetime(2026, 1, 5, 10, 0, 0)
        pair = self._make_pair(
            "EURUSD",
            100.0,
            trades=[
                TradeEvent(time=start, pair="EURUSD", direction="in", side="buy", volume=1.0, price=1.1000, sequence_in_pair=0),
                TradeEvent(time=start + timedelta(days=2), pair="EURUSD", direction="out", side="buy", volume=1.0, price=1.1001, sequence_in_pair=1),
            ],
            deals=[
                DealEvent(time=start + timedelta(days=2), pair="EURUSD", net_profit=0.0, volume=1.0, direction="out", sequence_in_pair=1),
            ],
        )
        rates = {"EURUSD": {"buy": -0.1, "sell": -0.1}}

        a = self._run_with_swap([pair], rates)
        b = self._run_with_swap([pair], rates)
        self.assertEqual(a, b)


class SwapConfigValidationTests(unittest.TestCase):
    def test_missing_buy_rate_fails_with_symbol_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "swap_rates.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "symbols:\n"
                    "  EURUSD:\n"
                    "    sell:\n"
                    "      daily_swap_per_001_lot: -0.1\n"
                )

            with self.assertRaisesRegex(ValueError, "EURUSD"):
                load_swap_rates_yaml(path)

    def test_missing_sell_rate_fails_with_symbol_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "swap_rates.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "symbols:\n"
                    "  GBPUSD:\n"
                    "    buy:\n"
                    "      daily_swap_per_001_lot: -0.1\n"
                )

            with self.assertRaisesRegex(ValueError, "GBPUSD"):
                load_swap_rates_yaml(path)

    def test_invalid_numeric_fails_with_symbol_and_side_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "swap_rates.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "symbols:\n"
                    "  EURUSD:\n"
                    "    buy:\n"
                    "      daily_swap_per_001_lot: bad\n"
                    "    sell:\n"
                    "      daily_swap_per_001_lot: -0.1\n"
                )

            with self.assertRaisesRegex(ValueError, "EURUSD\\.buy"):
                load_swap_rates_yaml(path)

    def test_malformed_yaml_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "swap_rates.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "symbols:\n"
                    "  EURUSD\n"  # malformed: missing ':'
                    "    buy:\n"
                    "      daily_swap_per_001_lot: -0.1\n"
                )

            with self.assertRaisesRegex(ValueError, "Malformed"):
                load_swap_rates_yaml(path)

    def test_reserved_effective_window_fields_are_accepted_and_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "swap_rates.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "symbols:\n"
                    "  EURUSD:\n"
                    "    effective_from: 2024-01-01\n"
                    "    effective_to: 2024-12-31\n"
                    "    buy:\n"
                    "      daily_swap_per_001_lot: -0.1\n"
                    "    sell:\n"
                    "      daily_swap_per_001_lot: -0.2\n"
                )

            loaded = load_swap_rates_yaml(path)
            self.assertAlmostEqual(loaded["EURUSD"]["buy"], -0.1, places=8)
            self.assertAlmostEqual(loaded["EURUSD"]["sell"], -0.2, places=8)


if __name__ == "__main__":
    unittest.main()
