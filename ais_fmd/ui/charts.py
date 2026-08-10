"""
Chart factories.

Every chart the app draws comes from here, so they cannot drift apart. The
replacements for the original charts:

  * Two donuts (income / expenses) -> sorted horizontal bars. Both were ranking
    questions, and a donut is the weakest common mark for ranking; at six to
    eight slices the labels also collided.
  * Continuous blue % ramp -> bullet chart with semantic colour. The original
    put 40% spent and 110% spent on the same scale, so the state that needs
    attention did not stand out.
  * Bare metric numbers -> KPI values with sparklines, so a figure arrives with
    its own trend rather than a single delta.
  * New: a waterfall, which is the clearest way to show where a semester's
    money actually went.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from . import theme

_HEIGHT_PER_ROW = 34
_MIN_HEIGHT = 220


def _height(rows: int, *, per_row: int = _HEIGHT_PER_ROW, base: int = 90) -> int:
    return max(_MIN_HEIGHT, base + rows * per_row)


def empty_figure(message: str) -> go.Figure:
    """A chart-shaped placeholder, so layout does not jump when data is absent."""
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        showarrow=False,
        font=dict(color=theme.INK_MUTE, size=13),
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
    )
    figure.update_layout(
        height=_MIN_HEIGHT,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return figure


def budget_bullet(df: pd.DataFrame) -> go.Figure:
    """
    Budget health as a bullet chart.

    Each committee is one row: a light bar for the allocation, a solid bar for
    actual spend coloured by status, and a tick at 100%. Over-budget reads
    instantly because it is a different colour, not a longer bar.
    """
    if df.empty:
        return empty_figure("No budget data for this selection.")

    data = df.sort_values("Spent", ascending=True)
    names = data["Committee_Name"].tolist()

    figure = go.Figure()

    figure.add_trace(
        go.Bar(
            name="Budget",
            y=names,
            x=data["Budget"],
            orientation="h",
            marker=dict(color=theme.GRID),
            width=0.62,
            hovertemplate="<b>%{y}</b><br>Budget: $%{x:,.2f}<extra></extra>",
        )
    )

    figure.add_trace(
        go.Bar(
            name="Spent",
            y=names,
            x=data["Spent"],
            orientation="h",
            marker=dict(
                color=[theme.STATUS_COLORS.get(status, theme.UNBUDGETED) for status in data["Status"]]
            ),
            width=0.3,
            customdata=data[["% Spent", "Budget", "Status"]].values,
            hovertemplate=(
                "<b>%{y}</b><br>Spent: $%{x:,.2f}<br>"
                "Budget: $%{customdata[1]:,.2f}<br>"
                "Used: %{customdata[0]:.1f}%<br>"
                "Status: %{customdata[2]}<extra></extra>"
            ),
        )
    )

    # Allocation ticks, so 100% is visible even where spend is far short of it.
    figure.add_trace(
        go.Scatter(
            name="Allocation",
            y=names,
            x=data["Budget"],
            mode="markers",
            marker=dict(symbol="line-ns", size=18, line=dict(color=theme.INK_SOFT, width=2)),
            hoverinfo="skip",
            showlegend=False,
        )
    )

    figure.update_layout(
        barmode="overlay",
        height=_height(len(data)),
        xaxis=dict(title="Dollars", tickprefix="$", separatethousands=True),
        yaxis=dict(title=None),
        legend=dict(orientation="h", y=1.04, x=0),
    )
    return figure


def ranked_bar(
    df: pd.DataFrame,
    *,
    label_column: str,
    value_column: str,
    color: str,
    title: str | None = None,
    max_rows: int = 12,
) -> go.Figure:
    """Sorted horizontal bars -- the donut replacement."""
    if df.empty:
        return empty_figure("Nothing to show for this selection.")

    data = df.sort_values(value_column, ascending=False).head(max_rows)
    data = data.sort_values(value_column, ascending=True)
    total = float(df[value_column].sum()) or 1.0
    share = data[value_column] / total * 100

    figure = go.Figure(
        go.Bar(
            y=data[label_column],
            x=data[value_column],
            orientation="h",
            marker=dict(color=color),
            text=[f"${value:,.0f}" for value in data[value_column]],
            textposition="outside",
            textfont=dict(color=theme.INK_MUTE, size=11),
            customdata=share,
            hovertemplate="<b>%{y}</b><br>$%{x:,.2f}<br>%{customdata:.1f}% of total<extra></extra>",
        )
    )
    figure.update_layout(
        height=_height(len(data)),
        xaxis=dict(title=None, tickprefix="$", separatethousands=True),
        yaxis=dict(title=None),
        showlegend=False,
    )
    # Only set a title when there is one -- passing None leaves an empty title
    # object that Plotly renders as the literal string "undefined".
    if title:
        figure.update_layout(title=title)
    # Headroom so the outside labels are not clipped.
    figure.update_xaxes(range=[0, float(data[value_column].max()) * 1.18])
    return figure


def waterfall(
    labels: list[str],
    values: list[float],
    measures: list[str],
    *,
    title: str | None = None,
) -> go.Figure:
    """Opening -> income -> expenses -> closing, as one continuous story."""
    if not labels:
        return empty_figure("Not enough data for a waterfall.")

    figure = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=measures,
            x=labels,
            y=values,
            connector=dict(line=dict(color=theme.GRID)),
            increasing=dict(marker=dict(color=theme.INCOME)),
            decreasing=dict(marker=dict(color=theme.EXPENSE)),
            totals=dict(marker=dict(color=theme.BUDGET)),
            hovertemplate="<b>%{x}</b><br>$%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        height=380,
        showlegend=False,
        yaxis=dict(title="Dollars", tickprefix="$", separatethousands=True),
        xaxis=dict(title=None),
    )
    if title:
        figure.update_layout(title=title)
    return figure


def sparkline(values: list[float], *, color: str | None = None) -> go.Figure:
    """
    A small trend with an emphasised endpoint.

    Sized to sit under a metric, with chrome stripped -- the shape is the
    message, the axes would only add noise.
    """
    color = color or theme.ACCENT
    if not values:
        return empty_figure("")

    figure = go.Figure(
        go.Scatter(
            y=values,
            mode="lines",
            line=dict(color=color, width=2),
            fill="tozeroy",
            fillcolor=_translucent(color, 0.16),
            hoverinfo="skip",
        )
    )
    figure.add_trace(
        go.Scatter(
            y=[values[-1]],
            x=[len(values) - 1],
            mode="markers",
            marker=dict(color=color, size=7),
            hoverinfo="skip",
        )
    )
    figure.update_layout(
        height=58,
        margin=dict(l=0, r=0, t=4, b=0),
        showlegend=False,
        xaxis=dict(visible=False, fixedrange=True),
        yaxis=dict(visible=False, fixedrange=True),
    )
    return figure


def trend_bars(df: pd.DataFrame) -> go.Figure:
    """Budget against actual across semesters."""
    if df.empty:
        return empty_figure("No historical data yet.")

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            name="Budget",
            x=df["Semester"],
            y=df["Budget"],
            marker=dict(color=theme.BUDGET),
            hovertemplate="Budget: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            name="Actual",
            x=df["Semester"],
            y=df["Spent"],
            marker=dict(color=theme.EXPENSE),
            hovertemplate="Spent: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        barmode="group",
        height=340,
        yaxis=dict(title="Dollars", tickprefix="$", separatethousands=True),
        xaxis=dict(title=None),
    )
    return figure


def category_treemap(df: pd.DataFrame, *, label_column: str, value_column: str) -> go.Figure:
    """Area-encoded spend -- useful when there are more categories than bars can hold."""
    if df.empty:
        return empty_figure("Nothing to show for this selection.")

    figure = go.Figure(
        go.Treemap(
            labels=df[label_column],
            parents=[""] * len(df),
            values=df[value_column],
            marker=dict(colors=theme.CATEGORICAL * (len(df) // len(theme.CATEGORICAL) + 1)),
            texttemplate="<b>%{label}</b><br>$%{value:,.0f}",
            hovertemplate="<b>%{label}</b><br>$%{value:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0))
    return figure


def confidence_histogram(confidences: list[float]) -> go.Figure:
    """How certain the categorizer was, for the review queue."""
    if not confidences:
        return empty_figure("Nothing categorized yet.")
    figure = go.Figure(
        go.Histogram(
            x=confidences,
            nbinsx=10,
            marker=dict(color=theme.ACCENT),
            hovertemplate="Confidence %{x}<br>%{y} transactions<extra></extra>",
        )
    )
    figure.update_layout(
        height=220,
        xaxis=dict(title="Match confidence", range=[0, 1]),
        yaxis=dict(title="Transactions"),
        showlegend=False,
    )
    return figure


def _translucent(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"
