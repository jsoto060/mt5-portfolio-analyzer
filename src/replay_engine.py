"""Replay engine orchestration helpers.

This module keeps replay behavior identical by delegating to mt5_portfolio_analyzer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from mt5_portfolio_analyzer import (
    PairData,
    ScalingConfig,
    build_pairs_from_auto,
    run_simulation,
)


@dataclass
class ReplayRun:
    """Container for raw replay outputs."""

    folder: str
    pairs_data: List[PairData]
    result: Dict[str, object]
    initial_balance: float
    scaling: ScalingConfig


class ReplayEngine:
    """Thin adapter around the legacy simulator preserving exact replay behavior."""

    def __init__(
        self,
        initial_balance: float = 5000.0,
        scale_exponent: float = 1.0,
        min_scale: float = 0.1,
        max_scale: float = 5.0,
        margin_requirements: Optional[Dict[str, float]] = None,
    ):
        self.initial_balance = initial_balance
        self.scaling = ScalingConfig(scale_exponent, min_scale, max_scale)
        self.margin_requirements = margin_requirements or {}

    def load_folder(self, folder: str) -> List[PairData]:
        """Load pair data from a folder using MT5 auto-discovery."""
        return build_pairs_from_auto(os.path.abspath(folder))

    def replay_pairs(self, folder: str, pairs_data: List[PairData]) -> ReplayRun:
        """Replay already-loaded pairs with configured scaling and margin settings."""
        result = run_simulation(
            pairs_data=pairs_data,
            initial_balance=self.initial_balance,
            scale_exponent=self.scaling.exponent,
            min_scale=self.scaling.min_scale,
            max_scale=self.scaling.max_scale,
            margin_requirements=self.margin_requirements,
        )
        return ReplayRun(
            folder=os.path.abspath(folder),
            pairs_data=pairs_data,
            result=result,
            initial_balance=self.initial_balance,
            scaling=self.scaling,
        )

    def replay_folder(self, folder: str) -> ReplayRun:
        """Load and replay a folder in one call."""
        pairs_data = self.load_folder(folder)
        return self.replay_pairs(folder, pairs_data)
