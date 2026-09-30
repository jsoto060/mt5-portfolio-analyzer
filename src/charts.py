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
    balance = pd.to_numeric(df["balance"], errors="coerce").replace(0, float("nan"))
    floating = pd.to_numeric(df.get("floating_pnl", 0.0), errors="coerce").fillna(0.0)
    df["drawdown"] = floating.where(floating < 0.0, 0.0).div(balance).mul(100.0)

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=df["time"], y=df["balance"], name="Balance", line=dict(width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["time"], y=df["equity"], name="Equity", line=dict(width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["time"], y=df["drawdown"], name="Drawdown %", line=dict(width=1.5, dash="dot")), secondary_y=True)
    fig.update_layout(title="Combined Balance, Equity, and Drawdown", template="plotly_white")
    fig.update_yaxes(title_text="Balance / Equity", secondary_y=False)
    fig.update_yaxes(title_text="Floating Drawdown (%)", ticksuffix="%", secondary_y=True)
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


def plot_monthly_growth(curve_rows, event_rows, initial_balance):
    """Monthly realized growth, including swaps after the last market timestamp."""
    fig = go.Figure()
    curve = pd.DataFrame(curve_rows)
    if curve.empty:
        return fig

    curve["time"] = pd.to_datetime(curve["time"], format="%Y.%m.%d %H:%M", errors="coerce")
    curve = curve.dropna(subset=["time"]).sort_values("time")
    if curve.empty:
        return fig

    months = pd.PeriodIndex(curve["time"].dt.to_period("M").unique()).sort_values()

    events = pd.DataFrame(event_rows)
    contributions = pd.DataFrame(index=months)
    if not events.empty:
        events["time"] = pd.to_datetime(events["time"], format="%Y.%m.%d %H:%M:%S", errors="coerce")
        events["scaled_net_profit"] = pd.to_numeric(events["scaled_net_profit"], errors="coerce").fillna(0.0)
        contributions = events.dropna(subset=["time"]).groupby([events["time"].dt.to_period("M"), "pair"])["scaled_net_profit"].sum().unstack(fill_value=0.0)
        months = months.union(contributions.index).sort_values()
        contributions = contributions.reindex(months, fill_value=0.0)

    month_pnl = contributions.sum(axis=1)
    month_start = (initial_balance + month_pnl.cumsum().shift(fill_value=0)).replace(0, float("nan"))
    growth = month_pnl.div(month_start).mul(100.0)
    for pair in sorted(contributions.columns):
        fig.add_trace(go.Bar(
            x=months.astype(str),
            y=contributions[pair].div(month_start).mul(100.0),
            name=pair,
            marker_color=PAIR_COLORS.get(pair),
            hovertemplate="%{x}<br>%{y:.2f}%<extra>%{fullData.name}</extra>",
        ))

    fig.add_trace(go.Scatter(
        x=months.astype(str), y=growth, name="Portfolio growth",
        mode="lines+markers", line=dict(color="#202124", width=2),
        hovertemplate="%{x}<br>%{y:.2f}%<extra>Portfolio growth</extra>",
    ))
    fig.update_layout(title="Monthly Balance Growth by Pair", barmode="relative", template="plotly_white", yaxis_title="Growth (%)")
    fig.update_yaxes(ticksuffix="%")
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


def build_pair_drawdown_contribution_df(curve_rows, timeline_snapshots):
    """Build per-pair percentage-point drawdown contributions on every replay timestamp.

    Uses portfolio floating drawdown from the replay curve and allocates it
    proportionally across pairs by current floating losses only.
    """
    curve_df = pd.DataFrame(curve_rows)
    if curve_df.empty:
        return pd.DataFrame(columns=["time", "pair", "drawdown_contribution", "portfolio_drawdown"])

    curve_df = curve_df.copy()
    curve_df["time"] = pd.to_datetime(curve_df["time"], format="%Y.%m.%d %H:%M", errors="coerce")
    floating = pd.to_numeric(curve_df.get("floating_pnl", 0.0), errors="coerce").fillna(0.0)
    balance = pd.to_numeric(curve_df["balance"], errors="coerce").replace(0, float("nan"))
    curve_df["portfolio_drawdown"] = floating.where(floating < 0.0, 0.0).div(balance).mul(100.0)
    snapshots = list(timeline_snapshots or [])

    all_pairs = sorted({
        pair
        for snapshot in snapshots
        for pair in snapshot.pair_snapshots.keys()
    })
    if not all_pairs:
        return pd.DataFrame(columns=["time", "pair", "drawdown_contribution", "portfolio_drawdown"])

    rows = []
    for idx, row in enumerate(curve_df.itertuples(index=False)):
        ts = row.time
        portfolio_drawdown = float(row.portfolio_drawdown)
        snapshot = snapshots[idx] if idx < len(snapshots) else None

        pair_losses = {}
        total_loss_abs = 0.0
        for pair in all_pairs:
            pair_floating = 0.0
            if snapshot is not None and pair in snapshot.pair_snapshots:
                pair_floating = float(snapshot.pair_snapshots[pair].floating_pnl)
            loss_abs = abs(pair_floating) if pair_floating < 0.0 else 0.0
            pair_losses[pair] = loss_abs
            total_loss_abs += loss_abs

        for pair in all_pairs:
            if portfolio_drawdown < 0.0 and total_loss_abs > 0.0:
                contribution = portfolio_drawdown * (pair_losses[pair] / total_loss_abs)
            else:
                contribution = 0.0
            rows.append({
                "time": ts,
                "pair": pair,
                "drawdown_contribution": contribution,
                "portfolio_drawdown": portfolio_drawdown,
            })

    return pd.DataFrame(rows)


def plot_pair_drawdown(curve_rows, timeline_snapshots):
    """Per-pair drawdown contribution synchronized to replay timeline."""
    df = build_pair_drawdown_contribution_df(curve_rows, timeline_snapshots)
    fig = go.Figure()
    if df.empty:
        return fig

    for pair, group in df.groupby("pair", sort=True):
        fig.add_trace(go.Scatter(
            x=group["time"],
            y=group["drawdown_contribution"],
            mode="lines",
            name=pair,
            line=dict(color=PAIR_COLORS.get(pair)),
        ))

    fig.update_layout(title="Per-Pair Drawdown Contribution", template="plotly_white", yaxis_title="Drawdown Contribution (%)")
    fig.update_yaxes(ticksuffix="%")
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


# ===========================================================================
# Margin Analysis Charts
# These functions accept a DataFrame from MarginAnalysis.curve_df().
# Column names follow the MarginAnalysis convention:
#   timestamp, margin_level, used_margin, free_margin, floating_pnl
#   <PAIR>_used_margin, <PAIR>_floating_pnl
#   <PAIR>_used_margin_contribution_pct
# ===========================================================================


def plot_margin_level(margin_curve_df: pd.DataFrame) -> go.Figure:
    """Margin level over time with threshold reference lines.

    Parameters
    ----------
    margin_curve_df:
        DataFrame from ``MarginAnalysis.curve_df()`` or ``load_margin_curve()``.
    """
    df = margin_curve_df.copy()
    if df.empty or "margin_level" not in df.columns:
        return go.Figure()

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    margin = pd.to_numeric(df["margin_level"], errors="coerce")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=margin,
        name="Margin Level %",
        line=dict(color="royalblue", width=1.5),
    ))

    for threshold, color in [(300, "green"), (200, "orange"), (150, "red"), (100, "darkred")]:
        fig.add_hline(
            y=threshold, line_dash="dash", line_color=color,
            annotation_text=f"{threshold}%", annotation_position="right",
        )

    fig.update_layout(
        title="Margin Level Timeline",
        xaxis_title="Time", yaxis_title="Margin Level %",
        template="plotly_white", hovermode="x unified",
    )
    return fig


def plot_used_margin_by_pair(margin_curve_df: pd.DataFrame) -> go.Figure:
    """Stacked used margin by pair over time.

    Parameters
    ----------
    margin_curve_df:
        DataFrame from ``MarginAnalysis.curve_df()``.
    """
    df = margin_curve_df.copy()
    if df.empty:
        return go.Figure()

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    pair_cols = {col: col.replace("_used_margin", "")
                 for col in df.columns if col.endswith("_used_margin")}

    fig = go.Figure()
    for col, pair in pair_cols.items():
        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=pd.to_numeric(df[col], errors="coerce"),
            name=pair, mode="lines", stackgroup="used_margin",
            line=dict(color=PAIR_COLORS.get(pair)),
        ))

    fig.update_layout(
        title="Used Margin by Pair (Stacked)",
        xaxis_title="Time", yaxis_title="Used Margin (USD)",
        template="plotly_white", hovermode="x unified",
    )
    return fig


def plot_floating_pnl_by_pair(margin_curve_df: pd.DataFrame) -> go.Figure:
    """Stacked floating PnL by pair over time.

    Parameters
    ----------
    margin_curve_df:
        DataFrame from ``MarginAnalysis.curve_df()``.
    """
    df = margin_curve_df.copy()
    if df.empty:
        return go.Figure()

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    pair_cols = {col: col.replace("_floating_pnl", "")
                 for col in df.columns
                 if col.endswith("_floating_pnl") and col != "floating_pnl"}

    fig = go.Figure()
    for col, pair in pair_cols.items():
        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=pd.to_numeric(df[col], errors="coerce"),
            name=pair, mode="lines", stackgroup="floating",
            line=dict(color=PAIR_COLORS.get(pair)),
        ))

    fig.update_layout(
        title="Floating PnL by Pair (Stacked)",
        xaxis_title="Time", yaxis_title="Floating PnL (USD)",
        template="plotly_white", hovermode="x unified",
    )
    return fig


def plot_used_margin_contribution(margin_curve_df: pd.DataFrame) -> go.Figure:
    """Used-margin contribution % per pair over time.

    Parameters
    ----------
    margin_curve_df:
        DataFrame from ``MarginAnalysis.curve_df()``.
    """
    df = margin_curve_df.copy()
    if df.empty:
        return go.Figure()

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    pair_cols = {col: col.replace("_used_margin_contribution_pct", "")
                 for col in df.columns if col.endswith("_used_margin_contribution_pct")}

    fig = go.Figure()
    for col, pair in pair_cols.items():
        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=pd.to_numeric(df[col], errors="coerce"),
            name=pair, mode="lines",
            line=dict(color=PAIR_COLORS.get(pair), width=2),
        ))

    fig.update_layout(
        title="Used Margin Contribution % by Pair",
        xaxis_title="Time", yaxis_title="Contribution %",
        template="plotly_white", hovermode="x unified",
    )
    return fig

