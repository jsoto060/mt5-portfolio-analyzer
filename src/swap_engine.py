"""Modeled swap event generation for replay ledger integration.

Swap rates are loaded from YAML and no broker-specific values are hardcoded.
"""

from __future__ import annotations

import heapq
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SwapEvent:
    """Explicit modeled swap event applied during balance replay."""

    timestamp: datetime
    pair: str
    direction: str
    original_lot: float
    reconstructed_lot: float
    daily_swap: float
    description: str
    sequence_in_pair: int


@dataclass
class _OpenSwapPosition:
    pair: str
    direction: str
    original_lot: float
    reconstructed_lot: float
    sequence_in_pair: int
    accrual_date: date
    next_event_time: datetime
    active: bool = True


@dataclass(frozen=True)
class _SwapEventPreview:
    timestamp: datetime
    pair: str
    sequence_in_pair: int


def _start_of_day(d: date) -> datetime:
    return datetime.combine(d, time.min)


def _split_mapping_token(token: str, path: str, line_no: int) -> Tuple[str, str]:
    if ":" not in token:
        raise ValueError(f"Malformed YAML mapping at {path}:{line_no}: {token!r}")
    key, value = token.split(":", 1)
    return key.strip(), value.strip()


def load_swap_rates_yaml(path: str) -> Dict[str, Dict[str, float]]:
    """Load minimal swap rates YAML structure used by this project.

    Expected structure:

    broker: ...
    symbols:
      EURUSD:
        buy:
          daily_swap_per_001_lot: -0.035
        sell:
          daily_swap_per_001_lot: -0.035
    metadata:
      ...
    """

    if not os.path.exists(path):
        raise FileNotFoundError(f"Swap rates file not found: {path}")

    symbols: Dict[str, Dict[str, float]] = {}
    _reserved_effective_window: Dict[str, Dict[str, str]] = {}
    in_symbols = False
    current_pair: Optional[str] = None
    current_side: Optional[str] = None

    with open(path, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            # Strip comments and trailing whitespace.
            content = raw.split("#", 1)[0].rstrip("\n\r")
            if not content.strip():
                continue

            indent = len(content) - len(content.lstrip(" "))
            token = content.strip()

            if indent == 0:
                if token == "symbols:":
                    in_symbols = True
                else:
                    in_symbols = False
                current_pair = None
                current_side = None
                continue

            if not in_symbols:
                continue

            if indent == 2 and token.endswith(":"):
                current_pair = token[:-1].strip().upper()
                if not current_pair:
                    raise ValueError(f"Invalid symbol key at {path}:{line_no}")
                symbols.setdefault(current_pair, {})
                _reserved_effective_window.setdefault(current_pair, {})
                current_side = None
                continue

            if indent == 2 and not token.endswith(":"):
                raise ValueError(
                    f"Malformed symbol declaration at {path}:{line_no}; expected '<SYMBOL>:'"
                )

            if indent == 4 and token.endswith(":"):
                side = token[:-1].strip().lower()
                if side not in {"buy", "sell"}:
                    raise ValueError(f"Invalid side '{side}' at {path}:{line_no}; expected buy/sell")
                if not current_pair:
                    raise ValueError(f"Side declared before symbol at {path}:{line_no}")
                current_side = side
                continue

            if indent == 4 and not token.endswith(":"):
                if not current_pair:
                    raise ValueError(f"Malformed symbol block at {path}:{line_no}")
                key, value = _split_mapping_token(token, path, line_no)
                if key not in {"effective_from", "effective_to"}:
                    raise ValueError(
                        f"Unsupported symbol field '{key}' for {current_pair} at {path}:{line_no}"
                    )
                # Reserved for future historical rate calibration; parsed and ignored for now.
                _reserved_effective_window[current_pair][key] = value
                current_side = None
                continue

            if indent == 6 and token.startswith("daily_swap_per_001_lot:"):
                if not current_pair or not current_side:
                    raise ValueError(f"Swap value declared before symbol/side at {path}:{line_no}")
                raw_value = token.split(":", 1)[1].strip()
                try:
                    value = float(raw_value)
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid daily_swap_per_001_lot '{raw_value}' for {current_pair}.{current_side} at {path}:{line_no}"
                    ) from exc
                symbols[current_pair][current_side] = value
                continue

            if indent == 6 and not token.startswith("daily_swap_per_001_lot:"):
                if not current_pair or not current_side:
                    raise ValueError(f"Malformed side block at {path}:{line_no}")
                raise ValueError(
                    f"Unsupported field under {current_pair}.{current_side} at {path}:{line_no}: {token!r}"
                )

            raise ValueError(f"Malformed YAML structure at {path}:{line_no}: {token!r}")

    if not symbols:
        raise ValueError(f"No swap rates loaded from {path}")

    for pair, sides in symbols.items():
        if "buy" not in sides or "sell" not in sides:
            raise ValueError(f"Swap rates for {pair} must include both buy and sell")

    return symbols


class SwapEngine:
    """Generates modeled daily swap events from open reconstructed positions."""

    def __init__(self, rates_by_symbol: Dict[str, Dict[str, float]]):
        if not rates_by_symbol:
            raise ValueError("SwapEngine requires non-empty swap rates")

        self.rates_by_symbol = {
            str(symbol).upper(): {
                "buy": float(rates["buy"]),
                "sell": float(rates["sell"]),
            }
            for symbol, rates in rates_by_symbol.items()
        }
        self.reset()

    @classmethod
    def from_yaml(cls, path: str) -> "SwapEngine":
        return cls(load_swap_rates_yaml(path))

    def reset(self) -> None:
        self._next_id = 1
        self._positions: Dict[int, _OpenSwapPosition] = {}
        self._positions_by_pair: Dict[str, List[int]] = {}
        self._due_heap: List[Tuple[datetime, str, int, int]] = []

    @staticmethod
    def event_type_rank() -> int:
        # Ordering tie-breaker for replay: swap events participate explicitly.
        return 0

    @staticmethod
    def deal_event_type_rank() -> int:
        return 1

    def _rate_for(self, pair: str, direction: str) -> float:
        normalized_pair = str(pair).upper()
        side = "sell" if str(direction).lower().startswith("sell") else "buy"

        if normalized_pair not in self.rates_by_symbol:
            raise ValueError(f"Missing swap rates for pair: {normalized_pair}")
        return float(self.rates_by_symbol[normalized_pair][side])

    def calculate_daily_swap(self, pair: str, direction: str, reconstructed_lot: float) -> float:
        rate = self._rate_for(pair, direction)
        return round(rate * (float(reconstructed_lot) / 0.01), 2)

    def register_open_position(
        self,
        pair: str,
        direction: str,
        original_lot: float,
        reconstructed_lot: float,
        entry_time: datetime,
        sequence_in_pair: int,
    ) -> None:
        accrual_date = entry_time.date()
        next_event_time = _start_of_day(accrual_date + timedelta(days=1))
        position = _OpenSwapPosition(
            pair=str(pair).upper(),
            direction=str(direction or "buy").lower(),
            original_lot=max(0.0, float(original_lot)),
            reconstructed_lot=max(0.0, float(reconstructed_lot)),
            sequence_in_pair=int(sequence_in_pair),
            accrual_date=accrual_date,
            next_event_time=next_event_time,
            active=True,
        )

        position_id = self._next_id
        self._next_id += 1
        self._positions[position_id] = position
        self._positions_by_pair.setdefault(position.pair, []).append(position_id)
        heapq.heappush(
            self._due_heap,
            (position.next_event_time, position.pair, position.sequence_in_pair, position_id),
        )

    def close_latest_position(self, pair: str) -> None:
        normalized_pair = str(pair).upper()
        stack = self._positions_by_pair.get(normalized_pair)
        if not stack:
            return
        position_id = stack.pop()
        position = self._positions.get(position_id)
        if position is not None:
            position.active = False

    def _peek_active_preview(self) -> Optional[Tuple[int, _SwapEventPreview]]:
        while self._due_heap:
            ts, pair, seq, position_id = self._due_heap[0]
            position = self._positions.get(position_id)
            if position is None or not position.active:
                heapq.heappop(self._due_heap)
                continue
            if position.next_event_time != ts:
                heapq.heappop(self._due_heap)
                continue
            return position_id, _SwapEventPreview(timestamp=ts, pair=pair, sequence_in_pair=seq)
        return None

    def peek_next_event(self) -> Optional[_SwapEventPreview]:
        preview = self._peek_active_preview()
        return preview[1] if preview else None

    def pop_next_event(self) -> Optional[SwapEvent]:
        preview = self._peek_active_preview()
        if preview is None:
            return None

        position_id, preview_event = preview
        heapq.heappop(self._due_heap)
        position = self._positions.get(position_id)
        if position is None or not position.active:
            return None

        daily_swap = self.calculate_daily_swap(
            pair=position.pair,
            direction=position.direction,
            reconstructed_lot=position.reconstructed_lot,
        )
        description = f"Modeled daily swap accrual for {position.accrual_date.isoformat()}"
        event = SwapEvent(
            timestamp=preview_event.timestamp,
            pair=position.pair,
            direction=position.direction,
            original_lot=position.original_lot,
            reconstructed_lot=position.reconstructed_lot,
            daily_swap=daily_swap,
            description=description,
            sequence_in_pair=position.sequence_in_pair,
        )

        # Next accrual day is the day after the emitted accrual day.
        position.accrual_date = position.accrual_date + timedelta(days=1)
        position.next_event_time = _start_of_day(position.accrual_date + timedelta(days=1))
        heapq.heappush(
            self._due_heap,
            (position.next_event_time, position.pair, position.sequence_in_pair, position_id),
        )

        return event