"""
Design tokens and the registered Plotly template.

FINDING (visual). `.streamlit/config.toml` set a dark palette, but no Plotly
template was ever registered or applied, so charts rendered on Plotly's default
light-oriented theme. The income donut used `qualitative.Set3` -- pale pastels
built for white backgrounds -- while the expense donut immediately beside it
used `Set1`, harsh saturated primaries. Two unrelated palettes, side by side,
on a ground neither was designed for.

Registering a template and setting it as the default means every chart in the
app inherits a coherent look, including charts written later.

Semantic colour is kept separate from the accent. The accent identifies the
product; income/expense/over-budget identify *meaning*, and must not drift as
the brand colour changes.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

# --- Tokens ------------------------------------------------------------------

ACCENT = "#1DB9FF"
ACCENT_DIM = "#0E7FB4"

# Semantic — these encode meaning, not brand.
INCOME = "#3FCF8E"
EXPENSE = "#F59E5B"
BUDGET = "#5B93F5"
OVER = "#F2748C"
APPROACHING = "#E8C05A"
ON_TRACK = "#3FCF8E"
UNBUDGETED = "#8B9AA3"

STATUS_COLORS = {
    "over": OVER,
    "approaching": APPROACHING,
    "on track": ON_TRACK,
    "unbudgeted": UNBUDGETED,
    "no budget": UNBUDGETED,
}

# Neutrals — biased slightly cool so they sit with the cyan accent rather than
# fighting it.
INK = "#E2EAEE"
INK_SOFT = "#B2C0C7"
INK_MUTE = "#7C8D95"
GRID = "#2A363C"
SURFACE = "#1E282D"

# Ordered categorical palette. One palette, used everywhere a series needs a
# colour, so adjacent charts stop disagreeing.
CATEGORICAL = [
    "#1DB9FF", "#3FCF8E", "#F59E5B", "#B99BF7", "#F2748C",
    "#E8C05A", "#5BC8C4", "#8B9AA3", "#7FA5F0", "#D98CB3",
]

TEMPLATE_NAME = "ais_fmd"

_FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif"


def build_template() -> go.layout.Template:
    return go.layout.Template(
        layout=go.Layout(
            font=dict(family=_FONT, size=13, color=INK_SOFT),
            title=dict(font=dict(size=16, color=INK), x=0, xanchor="left"),
            # Transparent grounds let charts sit on the Streamlit surface
            # instead of punching a differently-coloured rectangle into it.
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            colorway=CATEGORICAL,
            xaxis=dict(
                gridcolor=GRID,
                zerolinecolor=GRID,
                linecolor=GRID,
                tickfont=dict(color=INK_MUTE, size=12),
                title=dict(font=dict(color=INK_MUTE, size=12)),
                automargin=True,
            ),
            yaxis=dict(
                gridcolor=GRID,
                zerolinecolor=GRID,
                linecolor=GRID,
                tickfont=dict(color=INK_MUTE, size=12),
                title=dict(font=dict(color=INK_MUTE, size=12)),
                automargin=True,
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(color=INK_MUTE, size=12),
                bgcolor="rgba(0,0,0,0)",
            ),
            hoverlabel=dict(
                bgcolor=SURFACE,
                bordercolor=GRID,
                font=dict(family=_FONT, size=12, color=INK),
            ),
            margin=dict(l=8, r=8, t=36, b=8),
            separators=".,",
        )
    )


def register() -> None:
    """Register and activate. Safe to call repeatedly."""
    pio.templates[TEMPLATE_NAME] = build_template()
    pio.templates.default = TEMPLATE_NAME


APP_CSS = f"""
<style>
  /* Tighten the default Streamlit rhythm -- the stock spacing wastes a lot of
     vertical room on a dense financial page. */
  .block-container {{ padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1400px; }}

  /* Sandbox banner. Deliberately loud: it must be impossible to mistake the
     sandbox for the real system. */
  .ais-banner {{
    display: flex; align-items: center; gap: .6rem;
    border: 1px solid {ACCENT_DIM};
    border-left: 4px solid {ACCENT};
    background: rgba(29,185,255,.08);
    color: {INK};
    padding: .6rem .9rem;
    border-radius: 4px;
    font-size: .86rem;
    margin-bottom: 1.1rem;
  }}
  .ais-banner-prod {{
    border-color: {OVER};
    border-left-color: {OVER};
    background: rgba(242,116,140,.10);
  }}
  .ais-banner b {{ color: {ACCENT}; letter-spacing: .04em; }}
  .ais-banner-prod b {{ color: {OVER}; }}

  /* Page header */
  .ais-head {{ margin-bottom: .35rem; }}
  .ais-head h1 {{
    font-size: 1.85rem; font-weight: 620; letter-spacing: -.015em;
    margin: 0 0 .2rem; color: {INK};
  }}
  .ais-head p {{ color: {INK_MUTE}; font-size: .95rem; margin: 0; max-width: 70ch; }}
  .ais-rule {{ height: 1px; background: {GRID}; margin: .9rem 0 1.3rem; border: 0; }}

  /* Status pills */
  .ais-pill {{
    display: inline-block; font-size: .68rem; letter-spacing: .07em;
    text-transform: uppercase; padding: .15em .5em; border-radius: 3px;
    border: 1px solid currentColor; line-height: 1.6;
  }}
  .ais-over {{ color: {OVER}; }}
  .ais-approaching {{ color: {APPROACHING}; }}
  .ais-ontrack {{ color: {ON_TRACK}; }}
  .ais-muted {{ color: {INK_MUTE}; }}

  /* Empty / error states */
  .ais-empty {{
    border: 1px dashed {GRID}; border-radius: 5px;
    padding: 1.6rem; text-align: center; color: {INK_MUTE};
    font-size: .92rem;
  }}
  .ais-empty strong {{ display: block; color: {INK_SOFT}; margin-bottom: .3rem; }}

  /* Numeric columns line up */
  [data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; }}
</style>
"""
