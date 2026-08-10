"""
Page shell: header, banner, metrics, empty states.

FINDING (consistency). Every page in the original invented its own introduction,
its own filter placement, and its own way of saying "no data". One shell means
a new page is consistent by default rather than by discipline.

FINDING (performance). `animated_typing_title` slept 20ms per character and
every page called it at the top. Streamlit re-runs the whole script on every
widget interaction, so each filter change cost about half a second of
deliberate delay plus one markdown re-render per character. The animation is
kept, because it is part of the app's character -- but it runs once per session
per title, not once per interaction.
"""

from __future__ import annotations

import time
from typing import Iterable

import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import settings
from ..domain.money import format_currency, format_delta
from . import theme

_TITLE_SEEN_KEY = "_ais_titles_animated"


def bootstrap(page_title: str = "UF AIS Financial Management") -> None:
    """Call once at the top of the entry point."""
    st.set_page_config(
        page_title=page_title,
        page_icon="📒",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    theme.register()
    st.markdown(theme.APP_CSS, unsafe_allow_html=True)


def environment_banner() -> None:
    """Impossible to mistake the sandbox for the real system."""
    sandbox = settings.is_sandbox()
    label = "SANDBOX" if sandbox else "PRODUCTION"
    css_class = "ais-banner" if sandbox else "ais-banner ais-banner-prod"
    st.markdown(
        f'<div class="{css_class}"><b>{label}</b> — {settings.banner_text()}</div>',
        unsafe_allow_html=True,
    )


def animated_title(text: str, *, delay: float = 0.012) -> None:
    """
    Typing animation, but only the first time a given title is shown this session.

    Subsequent re-runs render it instantly, so filters stay responsive.
    """
    seen: set[str] = st.session_state.setdefault(_TITLE_SEEN_KEY, set())
    if text in seen:
        st.markdown(f'<div class="ais-head"><h1>{text}</h1></div>', unsafe_allow_html=True)
        return

    seen.add(text)
    placeholder = st.empty()
    for index in range(1, len(text) + 1):
        placeholder.markdown(
            f'<div class="ais-head"><h1>{text[:index]}</h1></div>', unsafe_allow_html=True
        )
        time.sleep(delay)


def page_header(title: str, subtitle: str = "", *, animate: bool = True) -> None:
    if animate:
        animated_title(title)
    else:
        st.markdown(f'<div class="ais-head"><h1>{title}</h1></div>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<div class="ais-head"><p>{subtitle}</p></div>', unsafe_allow_html=True)
    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)


def money_safe(text: str) -> str:
    """
    Escape dollar signs for Streamlit markdown.

    Streamlit treats `$...$` as LaTeX math delimiters, so a sentence containing
    two currency figures -- which is most sentences in this app -- silently
    renders the text between them as an equation. Every markdown surface that
    can carry a formatted amount routes through here.
    """
    return re.sub(r"\$", r"\\$", text)


def say(text: str, *, caption: bool = False) -> None:
    """Render prose that may contain currency, without LaTeX mangling it."""
    if caption:
        st.caption(money_safe(text))
    else:
        st.markdown(money_safe(text))


def empty_state(headline: str, detail: str = "") -> None:
    st.markdown(
        f'<div class="ais-empty"><strong>{headline}</strong>{detail}</div>',
        unsafe_allow_html=True,
    )


def error_state(headline: str, detail: str = "") -> None:
    """
    An error that explains itself.

    FINDING F8's UI half: the original swallowed categorization failures
    entirely, so the treasurer had no reason to retry. Failures get a visible,
    specific message.
    """
    body = f"**{headline}**\n\n{detail}" if detail else f"**{headline}**"
    st.error(money_safe(body))


def notify(kind: str, text: str) -> None:
    """success / warning / info that is safe to contain currency."""
    {"success": st.success, "warning": st.warning, "info": st.info}[kind](money_safe(text))


def pill(label: str, kind: str = "muted") -> str:
    mapping = {
        "over": "ais-over",
        "approaching": "ais-approaching",
        "on track": "ais-ontrack",
    }
    css = mapping.get(kind, "ais-muted")
    return f'<span class="ais-pill {css}">{label}</span>'


def metric(
    container,
    label: str,
    value: object,
    *,
    delta: object = None,
    inverse: bool = False,
    currency: bool = True,
    trend: list[float] | None = None,
) -> None:
    """
    One KPI.

    FINDING F14: the delta is preformatted, so it no longer renders as a bare
    1234.56 beneath a value formatted as $1,234.56.

    FINDING F11: `inverse=True` on expenses, so spending more than last semester
    reads as negative rather than being coloured as an improvement.
    """
    display_value = format_currency(value) if currency else f"{value:,}"
    display_delta = format_delta(delta) if currency else (f"{delta:+,}" if delta else None)
    container.metric(
        label=label,
        value=display_value,
        delta=display_delta,
        delta_color="inverse" if inverse else "normal",
    )
    if trend:
        from . import charts

        container.plotly_chart(
            charts.sparkline(trend, color=theme.OVER if inverse else theme.ACCENT),
            width="stretch",
            config={"displayModeBar": False},
            key=f"spark_{label}",
        )


def metric_row(specs: Iterable[dict]) -> None:
    """Lay out KPI tiles. Falls back to a single column on narrow screens."""
    specs = list(specs)
    if not specs:
        return
    columns = st.columns(len(specs))
    for column, spec in zip(columns, specs):
        metric(
            column,
            spec["label"],
            spec["value"],
            delta=spec.get("delta"),
            inverse=spec.get("inverse", False),
            currency=spec.get("currency", True),
            trend=spec.get("trend"),
        )


def chart(figure: go.Figure, *, key: str | None = None) -> None:
    """Render a Plotly figure with consistent options."""
    st.plotly_chart(
        figure,
        width="stretch",
        config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
        key=key,
    )


def dataframe(df: pd.DataFrame, **kwargs) -> None:
    """Render a table. Wide tables scroll inside their own container."""
    kwargs.setdefault("hide_index", True)
    st.dataframe(df, width="stretch", **kwargs)


def sidebar_footer() -> None:
    st.sidebar.markdown("---")
    mode = "Sandbox" if settings.is_sandbox() else "Production"
    st.sidebar.caption(f"Mode: **{mode}**")
    if settings.is_sandbox():
        st.sidebar.caption("Local SQLite. No network, no real money.")
