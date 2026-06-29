"""Portfolio metric utilities for replay analysis."""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd


def average_monthly_growth_percent(curve_rows: List[Dict[str, object]]) -> Optional[float]:
    """Average month-over-month balance growth in percent."""
    df = pd.DataFrame(curve_rows)
    if df.empty or "time" not in df.columns or "balance" not in df.columns:
        return None

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M", errors="coerce")
    df = df.dropna(subset=["time"])
    if df.empty:
        return None

    df["ym"] = df["time"].dt.to_period("M")
    monthly_bal = df.groupby("ym")["balance"].last()
    if len(monthly_bal) < 2:
        return None

    returns = monthly_bal.pct_change().dropna()
    if len(returns) == 0:
        return None
    return float(returns.mean() * 100.0)


def peak_equity_drawdown(curve_rows: List[Dict[str, object]]) -> Dict[str, Optional[float]]:
    """Compute peak-to-trough drawdown on the equity series."""
    df = pd.DataFrame(curve_rows)
    if df.empty or "equity" not in df.columns:
        return {"peak_equity_drawdown_abs": None, "peak_equity_drawdown_percent": None}

    equity = pd.to_numeric(df["equity"], errors="coerce").fillna(0.0)
    running_peak = equity.cummax()
    dd_abs = running_peak - equity
    dd_pct = (dd_abs / running_peak.replace(0, pd.NA)) * 100.0

    max_abs = float(dd_abs.max()) if len(dd_abs) else None
    max_pct = float(dd_pct.fillna(0.0).max()) if len(dd_pct) else None
    return {
        "peak_equity_drawdown_abs": max_abs,
        "peak_equity_drawdown_percent": max_pct,
    }


def max_floating_drawdown(curve_rows: List[Dict[str, object]]) -> Dict[str, Optional[float]]:
    """Compute worst floating PnL drawdown from reconstructed curve rows."""
    df = pd.DataFrame(curve_rows)
    if df.empty or "floating_pnl" not in df.columns:
        return {"max_floating_drawdown_abs": None, "max_floating_drawdown_percent": None}

    floating = pd.to_numeric(df["floating_pnl"], errors="coerce").fillna(0.0)
    balance = pd.to_numeric(df.get("balance", 0.0), errors="coerce").replace(0, pd.NA)

    min_floating = float(floating.min()) if len(floating) else None
    min_idx = int(floating.idxmin()) if len(floating) else None
    pct = None
    if min_idx is not None and min_idx in balance.index and pd.notna(balance.loc[min_idx]):
        pct = float((floating.loc[min_idx] / balance.loc[min_idx]) * 100.0)

    return {
        "max_floating_drawdown_abs": min_floating,
        "max_floating_drawdown_percent": pct,
    }


def margin_statistics(curve_rows: List[Dict[str, object]]) -> Dict[str, Optional[float]]:
    """Compute margin-related min/max metrics."""
    df = pd.DataFrame(curve_rows)
    if df.empty:
        return {
            "minimum_margin_level_percent": None,
            "minimum_free_margin": None,
            "maximum_used_margin": None,
        }

    margin = pd.to_numeric(df.get("margin_level_percent", pd.Series(dtype=float)), errors="coerce")
    free_margin = pd.to_numeric(df.get("free_margin", pd.Series(dtype=float)), errors="coerce")
    used_margin = pd.to_numeric(df.get("used_margin", pd.Series(dtype=float)), errors="coerce")

    return {
        "minimum_margin_level_percent": float(margin.min()) if margin.notna().any() else None,
        "minimum_free_margin": float(free_margin.min()) if free_margin.notna().any() else None,
        "maximum_used_margin": float(used_margin.max()) if used_margin.notna().any() else None,
    }


def trade_statistics(event_rows: List[Dict[str, object]]) -> Dict[str, Optional[float]]:
    """Compute trade-level win/loss and profitability statistics."""
    df = pd.DataFrame(event_rows)
    if df.empty or "scaled_net_profit" not in df.columns:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "win_rate_percent": None,
            "profit_factor": None,
        }

    pnl = pd.to_numeric(df["scaled_net_profit"], errors="coerce").fillna(0.0)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    gross_profit = float(wins.sum())
    gross_loss_abs = float(abs(losses.sum()))

    profit_factor = None
    if gross_loss_abs > 0:
        profit_factor = gross_profit / gross_loss_abs

    total = int(len(pnl))
    win_count = int((pnl > 0).sum())
    win_rate = (win_count / total * 100.0) if total > 0 else None

    return {
        "total_trades": total,
        "winning_trades": win_count,
        "win_rate_percent": float(win_rate) if win_rate is not None else None,
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
    }


def recovery_factor(final_balance: float, initial_balance: float, max_drawdown_abs: Optional[float]) -> Optional[float]:
    """Recovery factor = net profit / absolute max drawdown."""
    if max_drawdown_abs is None:
        return None
    denom = abs(float(max_drawdown_abs))
    if denom <= 0:
        return None
    return float((final_balance - initial_balance) / denom)


def build_metric_bundle(summary: Dict[str, object], curve_rows: List[Dict[str, object]], event_rows: List[Dict[str, object]]) -> Dict[str, object]:
    """Build an extended metric dictionary from simulator outputs."""
    out = dict(summary)
    out["profit"] = float(out["final_balance"] - out["initial_balance"])

    out["average_monthly_growth_percent"] = average_monthly_growth_percent(curve_rows)

    out.update(max_floating_drawdown(curve_rows))
    out.update(peak_equity_drawdown(curve_rows))
    out.update(margin_statistics(curve_rows))
    out.update(trade_statistics(event_rows))

    out["recovery_factor"] = recovery_factor(
        final_balance=float(out["final_balance"]),
        initial_balance=float(out["initial_balance"]),
        max_drawdown_abs=out.get("max_floating_drawdown_abs"),
    )
    return out
