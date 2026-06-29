"""Chart builders for replay analysis notebooks and scripts."""

from __future__ import annotations

from typing import Dict

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


PAIR_COLORS: Dict[str, str] = {
    "EURUSD": "#1f77b4",
    "EURGBP": "#2ca02c",
    "GBPUSD": "#ff7f0e",
    "USDCHF": "#d62728",
}


def plot_equity(curve_rows):
    """Combined balance, equity, and drawdown chart."""
    df = pd.DataFrame(curve_rows)
    if df.empty:
        return go.Figure()

    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M", errors="coerce")
    df["drawdown"] = pd.to_numeric(df.get("floating_pnl", 0.0), errors="coerce").fillna(0.0)

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=df["time"], y=df["balance"], name="Balance", line=dict(width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["time"], y=df["equity"], name="Equity", line=dict(width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["time"], y=df["drawdown"], name="Drawdown", line=dict(width=1.5, dash="dot")), secondary_y=True)
    fig.update_layout(title="Combined Balance, Equity, and Drawdown", template="plotly_white")
    fig.update_yaxes(title_text="Balance / Equity", secondary_y=False)
    fig.update_yaxes(title_text="Floating Drawdown", secondary_y=True)
    return fig


def plot_margin(curve_rows):
    """Used margin, free margin, and margin level chart."""
    df = pd.DataFrame(curve_rows)
    if df.empty:
        return go.Figure()

    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M", errors="coerce")

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=df["time"], y=df.get("used_margin", 0.0), name="Used Margin"), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["time"], y=df.get("free_margin", 0.0), name="Free Margin"), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["time"], y=pd.to_numeric(df.get("margin_level_percent", None), errors="coerce"), name="Margin Level %"), secondary_y=True)
    fig.update_layout(title="Margin Metrics", template="plotly_white")
    fig.update_yaxes(title_text="Margin (USD)", secondary_y=False)
    fig.update_yaxes(title_text="Margin Level %", secondary_y=True)
    return fig


def _pair_cumulative_balance(event_rows):
    df = pd.DataFrame(event_rows)
    if df.empty:
        return pd.DataFrame(columns=["time", "pair", "pair_balance"])

    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S", errors="coerce")
    df["scaled_net_profit"] = pd.to_numeric(df["scaled_net_profit"], errors="coerce").fillna(0.0)
    df = df.sort_values(["pair", "time"])
    df["pair_balance"] = df.groupby("pair")["scaled_net_profit"].cumsum()
    return df


def plot_pair_balance(event_rows):
    """Per-pair cumulative PnL chart."""
    df = _pair_cumulative_balance(event_rows)
    fig = go.Figure()
    if df.empty:
        return fig

    for pair, group in df.groupby("pair"):
        fig.add_trace(go.Scatter(
            x=group["time"],
            y=group["pair_balance"],
            mode="lines",
            name=pair,
            line=dict(color=PAIR_COLORS.get(pair)),
        ))
    fig.update_layout(title="Per-Pair Balance Contribution", template="plotly_white")
    return fig


def plot_pair_floating(pairs_data):
    """Per-pair standalone floating profile from imported MT5 curves."""
    fig = go.Figure()
    for pair in sorted(pairs_data, key=lambda p: p.name):
        if not pair.curve:
            continue
        df = pd.DataFrame({
            "time": [c.time for c in pair.curve],
            "floating": [c.equity - c.balance for c in pair.curve],
        })
        fig.add_trace(go.Scatter(
            x=df["time"],
            y=df["floating"],
            mode="lines",
            name=pair.name,
            line=dict(color=PAIR_COLORS.get(pair.name)),
        ))
    fig.update_layout(title="Per-Pair Floating PnL", template="plotly_white")
    return fig


def plot_pair_drawdown(event_rows):
    """Per-pair drawdown contribution based on pair cumulative contribution curves."""
    df = _pair_cumulative_balance(event_rows)
    fig = go.Figure()
    if df.empty:
        return fig

    for pair, group in df.groupby("pair"):
        series = group["pair_balance"].astype(float)
        dd = series - series.cummax()
        fig.add_trace(go.Scatter(
            x=group["time"],
            y=dd,
            mode="lines",
            name=pair,
            line=dict(color=PAIR_COLORS.get(pair)),
        ))

    fig.update_layout(title="Per-Pair Drawdown Contribution", template="plotly_white")
    return fig


def plot_comparison(baseline_curve_rows, proposed_curve_rows):
    """Overlay baseline vs proposed equity curves."""
    b = pd.DataFrame(baseline_curve_rows)
    p = pd.DataFrame(proposed_curve_rows)
    fig = go.Figure()
    if not b.empty:
        b["time"] = pd.to_datetime(b["time"], format="%Y.%m.%d %H:%M", errors="coerce")
        fig.add_trace(go.Scatter(x=b["time"], y=b["equity"], name="Baseline Equity"))
    if not p.empty:
        p["time"] = pd.to_datetime(p["time"], format="%Y.%m.%d %H:%M", errors="coerce")
        fig.add_trace(go.Scatter(x=p["time"], y=p["equity"], name="Proposed Equity"))
    fig.update_layout(title="Portfolio Comparison", template="plotly_white")
    return fig
