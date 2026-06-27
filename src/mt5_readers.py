"""
Readers for MT5 Strategy Tester exports.

Two file types are supported:

1. XLSX report  (ReportTester-*.xlsx)
   - Single sheet named 'Sheet1'.
   - The word 'Deals' appears alone in column A; the very next row is the
     column header:  Time | Deal | Symbol | Type | Direction | Volume |
                     Price | Order | Commission | Swap | Profit | Balance |
                     Comment
   - Only rows where Direction == 'out' represent closed trades.
   - The final row has None in column A (totals line) and must be skipped.
   - Volume is stored as a string (e.g. '0.17').

2. Tester-graph CSV  (testergraph.report.*.csv)
   - Encoding: UTF-16 with BOM.
   - Delimiter: tab.
   - Header row: <DATE>  <BALANCE>  <EQUITY>  <DEPOSIT LOAD>
   - Time format: %Y.%m.%d %H:%M
"""

import csv
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import warnings
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import openpyxl
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False


_MT5_TIME_FORMATS = [
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


logger = logging.getLogger(__name__)


@dataclass
class RawDeal:
    time: datetime
    direction: str          # 'in' or 'out'
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    balance: float


@dataclass
class RawCurvePoint:
    time: datetime
    balance: float
    equity: float


def _parse_mt5_time(raw) -> datetime:
    text = str(raw).strip()
    for fmt in _MT5_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Cannot parse MT5 time: {text!r}") from exc


def _safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# XLSX reader
# ---------------------------------------------------------------------------

def load_xlsx_deals(path: str, include_open: bool = False) -> Tuple[List[RawDeal], float]:
    """
    Parse an MT5 Strategy Tester XLSX report.

    Returns (deals, initial_balance). By default, deals contains only
    closed-trade rows (Direction == 'out'). If include_open=True, both
    opening and closing rows ('in' and 'out') are included.
    initial_balance is read from the 'Initial Deposit:' metadata row.
    """
    if not _HAS_OPENPYXL:
        raise ImportError("openpyxl is required to read XLSX files: pip install openpyxl")

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    # --- locate initial deposit ---
    initial_balance = 0.0
    for row in rows:
        if row[0] and str(row[0]).strip() == "Initial Deposit:":
            initial_balance = _safe_float(row[3], 0.0)
            break

    # --- locate 'Deals' section header ---
    deals_header_row = None
    for i, row in enumerate(rows):
        if row[0] and str(row[0]).strip() == "Deals":
            deals_header_row = i + 1  # header is the very next row
            break

    if deals_header_row is None:
        raise ValueError(f"Could not find 'Deals' section in {path}")

    header = rows[deals_header_row]
    col = {str(v).strip(): idx for idx, v in enumerate(header) if v is not None}

    required = {"Time", "Direction", "Volume", "Price", "Commission", "Swap", "Profit", "Balance"}
    missing = required - col.keys()
    if missing:
        raise ValueError(f"XLSX deals table missing columns: {missing} in {path}")

    deals: List[RawDeal] = []
    for row in rows[deals_header_row + 1:]:
        # Stop at an empty Time cell (totals row or blank).
        raw_time = row[col["Time"]]
        if raw_time is None:
            continue

        direction = str(row[col["Direction"]] or "").strip().lower()
        if direction not in {"in", "out"}:
            # balance rows and unsupported rows are ignored
            continue
        if not include_open and direction != "out":
            continue

        try:
            ts = _parse_mt5_time(raw_time)
        except ValueError:
            continue

        deals.append(RawDeal(
            time=ts,
            direction=direction,
            volume=_safe_float(row[col["Volume"]], 0.0),
            price=_safe_float(row[col["Price"]], 0.0),
            commission=_safe_float(row[col["Commission"]], 0.0),
            swap=_safe_float(row[col["Swap"]], 0.0),
            profit=_safe_float(row[col["Profit"]], 0.0),
            balance=_safe_float(row[col["Balance"]], 0.0),
        ))

    deals.sort(key=lambda d: d.time)
    return deals, initial_balance


# ---------------------------------------------------------------------------
# Tester-graph CSV reader
# ---------------------------------------------------------------------------

def load_graph_csv(path: str) -> List[RawCurvePoint]:
    """
    Parse an MT5 tester-graph CSV file (UTF-16, tab-delimited).

    Returns curve points sorted chronologically.
    """
    points: List[RawCurvePoint] = []
    skipped_rows = 0

    with open(path, encoding="utf-16", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = None
        date_col = bal_col = eq_col = None
        for row in reader:
            if header is None:
                # Normalise header: strip < > and whitespace, lower-case
                header = [c.strip().strip("<>").lower().replace(" ", "_") for c in row]
                col = {name: idx for idx, name in enumerate(header)}
                date_col = col.get("date")
                bal_col = col.get("balance")
                eq_col = col.get("equity")

                if date_col is None or bal_col is None or eq_col is None:
                    # Header mapping failed; try positional fallback (DATE BAL EQ ...)
                    date_col, bal_col, eq_col = 0, 1, 2
                continue

            if len(row) < 3:
                skipped_rows += 1
                continue

            try:
                ts  = _parse_mt5_time(row[date_col])
                bal = float(row[bal_col].replace(",", ""))
                eq  = float(row[eq_col].replace(",", ""))
            except (ValueError, IndexError):
                skipped_rows += 1
                continue

            points.append(RawCurvePoint(time=ts, balance=bal, equity=eq))

    if skipped_rows > 0:
        logger.warning("Skipped %s malformed curve rows while reading %s", skipped_rows, path)

    points.sort(key=lambda p: p.time)
    return points


# ---------------------------------------------------------------------------
# Auto-discovery helper
# ---------------------------------------------------------------------------

_PAIR_ALIASES = {
    "eurusd": "EURUSD",
    "eurgbp": "EURGBP",
    "gbpusd": "GBPUSD",
    "usdchf": "USDCHF",
}


def discover_files(data_dir: str):
    """
    Scan data_dir and return a dict mapping canonical pair name ->
    {xlsx: path, csv: path}.  Matches are case-insensitive substring search.
    """
    found = {p: {} for p in _PAIR_ALIASES.values()}
    for fname in sorted(os.listdir(data_dir)):
        lower = fname.lower()
        fpath = os.path.join(data_dir, fname)
        for alias, pair in _PAIR_ALIASES.items():
            if alias in lower:
                if lower.endswith(".xlsx"):
                    if "xlsx" in found[pair] and found[pair]["xlsx"] != fpath:
                        raise ValueError(
                            f"Multiple XLSX files matched for {pair} in {data_dir}: "
                            f"{os.path.basename(found[pair]['xlsx'])}, {fname}"
                        )
                    found[pair]["xlsx"] = fpath
                elif lower.endswith(".csv"):
                    if "csv" in found[pair] and found[pair]["csv"] != fpath:
                        raise ValueError(
                            f"Multiple CSV files matched for {pair} in {data_dir}: "
                            f"{os.path.basename(found[pair]['csv'])}, {fname}"
                        )
                    found[pair]["csv"] = fpath
    return found
