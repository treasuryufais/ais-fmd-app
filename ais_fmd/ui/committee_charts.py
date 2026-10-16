"""
Charts specific to a VP's committee page.

Kept out of `ui/charts.py`, which the UI team owns, so the two teams can edit
without colliding. Everything here follows the same conventions: colours from
the active palette (so it works in light and dark), no data fetching, and an
`empty_figure` placeholder when there is nothing to draw.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from . import charts, theme


def _translucent(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def pace_chart(
    spend: pd.DataFrame,
    budget: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    today: pd.Timestamp | None = None,
) -> go.Figure:
    """
    Money spent so far against the straight line a budget would follow if spent
    evenly across the term.

    The solid line is cumulative spend, stopping where the data stops. The dashed
    line runs from $0 at the start of the term to the full budget at the end, so
    "ahead of pace" is simply "above the dashed line". The dotted ceiling is the
    budget itself. A faint vertical marks today when it falls inside the term.
    """
    if budget <= 0 and (spend.empty or float(spend["spent"].max()) <= 0):
        return charts.empty_figure("No budget or spending to plot yet.")

    p = theme.active()
    last_spent = float(spend["spent"].iloc[-1]) if not spend.empty else 0.0
    over = budget > 0 and last_spent > budget
    spend_color = p.over if over else p.expense

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            name="On pace",
            x=[start, end],
            y=[0.0, float(budget)],
            mode="lines",
            line=dict(color=p.budget, width=1.6, dash="dash"),
            hovertemplate="On pace  $%{y:,.2f}<extra></extra>",
        )
    )
    if not spend.empty:
        figure.add_trace(
            go.Scatter(
                name="Spent",
                x=spend["date"],
                y=spend["spent"],
                mode="lines",
                line=dict(color=spend_color, width=2.2),
                fill="tozeroy",
                fillcolor=_translucent(spend_color, 0.12 * theme.active().fill_scale),
                hovertemplate="%{x|%b %d}  $%{y:,.2f}<extra></extra>",
            )
        )

    if budget > 0:
        figure.add_shape(
            type="line",
            xref="x", x0=start, x1=end,
            yref="y", y0=float(budget), y1=float(budget),
            line=dict(color=p.budget, width=1, dash="dot"),
        )
        figure.add_annotation(
            x=end, y=float(budget), xanchor="right", yanchor="bottom",
            text="Budget", showarrow=False,
            font=dict(color=p.ink_mute, size=10),
        )

    if today is not None and start <= today <= end:
        figure.add_shape(
            type="line",
            xref="x", x0=today, x1=today,
            yref="paper", y0=0, y1=1,
            line=dict(color=p.ink_mute, width=1, dash="dot"),
        )

    figure.update_layout(
        height=340,
        yaxis=dict(title=None, tickprefix="$", separatethousands=True, rangemode="tozero"),
        xaxis=dict(title=None, showgrid=False, range=[start, end]),
        legend=dict(orientation="h", y=1.1, x=0),
    )
    return figure
