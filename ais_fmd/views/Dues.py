"""Module M7 -- dues and membership revenue."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues as dues_domain
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import default_semester_index, ordered_semesters
from ais_fmd.ui import charts, shell, theme

auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Dues",
    "The organisation's most predictable income line: who has paid, how much is "
    "outstanding, and whether collection is tracking previous terms.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined", "Add an academic term first.")
    st.stop()

selected = st.selectbox(
    "Semester",
    semesters,
    index=default_semester_index(bundle.transactions, bundle.terms),
    key="dues_semester",
)
summary = dues_domain.summarize(bundle.transactions, bundle.terms, selected)

if summary.payment_count == 0:
    shell.empty_state(
        f"No dues payments found in {selected}",
        "Dues are identified by committee assignment or by the standard payment "
        "amounts arriving via Venmo or Zelle.",
    )
    st.stop()

# --- Headline ----------------------------------------------------------------

metrics = st.columns(4)
metrics[0].metric("Collected", format_currency(summary.total_collected))
metrics[1].metric("Payments", f"{summary.payment_count:,}")
metrics[2].metric("Members paid", f"{summary.unique_payers:,}")
metrics[3].metric("Average payment", format_currency(summary.average_payment))

if summary.first_payment is not None:
    st.caption(
        f"First payment {summary.first_payment:%b %d, %Y}, "
        f"most recent {summary.last_payment:%b %d, %Y}."
    )

# --- Gap against an expected roster ------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Collection against roster")

roster_column, gap_column = st.columns([1, 3])
with roster_column:
    expected = st.number_input(
        "Expected members",
        min_value=0,
        value=max(summary.unique_payers, 0),
        step=5,
        help="How many members you expect to pay this term. The gap is calculated from this.",
    )

gap = dues_domain.outstanding(summary.unique_payers, int(expected))
with gap_column:
    if gap["expected"] == 0:
        st.caption("Enter an expected roster size to see the outstanding count.")
    else:
        bar = st.columns(3)
        bar[0].metric("Paid", f"{gap['paid']:,}")
        bar[1].metric("Outstanding", f"{gap['outstanding']:,}")
        bar[2].metric("Collection rate", f"{gap['rate']:.0f}%")
        st.progress(min(gap["rate"] / 100, 1.0))

# --- Collection curve --------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Collection curve")
st.caption(
    "Cumulative dues by day since the term began. Indexing on elapsed days rather "
    "than calendar date is what makes two semesters comparable on one axis."
)

compare_with = st.multiselect(
    "Compare against",
    [s for s in semesters if s != selected],
    default=[s for s in semesters if s != selected][-1:],
    key="dues_compare",
)

figure = go.Figure()
palette = [theme.ACCENT, theme.INCOME, theme.EXPENSE, theme.UNBUDGETED]

for index, semester in enumerate([selected] + compare_with):
    curve = dues_domain.collection_curve(bundle.transactions, bundle.terms, semester)
    if curve.empty:
        continue
    figure.add_trace(
        go.Scatter(
            x=curve["Day"],
            y=curve["Cumulative"],
            mode="lines",
            name=semester,
            line=dict(
                color=palette[index % len(palette)],
                width=3 if semester == selected else 2,
                dash="solid" if semester == selected else "dot",
            ),
            hovertemplate=f"<b>{semester}</b><br>Day %{{x}}<br>$%{{y:,.2f}}<extra></extra>",
        )
    )

figure.update_layout(
    height=360,
    xaxis=dict(title="Days since term start"),
    yaxis=dict(title="Cumulative collected", tickprefix="$", separatethousands=True),
)
shell.chart(figure, key="dues_curve")

# --- Rates and roster --------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
rate_column, roster_col = st.columns([1, 2])

with rate_column:
    st.markdown("#### Payment amounts")
    rates = pd.DataFrame(
        [{"Amount": amount, "Payments": count} for amount, count in sorted(summary.rates.items())]
    )
    shell.dataframe(
        rates,
        column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
    )
    st.caption(
        "Multiple amounts usually mean an instalment rate alongside the full rate."
    )

with roster_col:
    st.markdown("#### Who has paid")
    roster = dues_domain.payer_roster(bundle.transactions, bundle.terms, selected)
    if roster.empty:
        shell.empty_state(
            "No payer names could be recovered",
            "Names are read from the transfer description, which not every payment carries.",
        )
    else:
        search = st.text_input("Search members", placeholder="name…", key="dues_search")
        if search.strip():
            roster = roster[roster["Payer"].str.contains(search.strip(), case=False, na=False)]
        shell.dataframe(
            roster,
            column_config={
                "Paid": st.column_config.NumberColumn(format="$%.2f"),
                "First payment": st.column_config.DateColumn(format="MMM D, YYYY"),
            },
            height=340,
        )
        st.caption(f"{len(roster):,} member(s). Repeat payments are aggregated per person.")

# --- Across semesters --------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Across semesters")

comparison = dues_domain.compare_semesters(bundle.transactions, bundle.terms, semesters)
table_col, chart_col = st.columns([2, 3])
with table_col:
    shell.dataframe(
        comparison,
        column_config={
            "Collected": st.column_config.NumberColumn(format="$%.2f"),
            "Average": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
with chart_col:
    shell.chart(
        charts.ranked_bar(
            comparison.rename(columns={"Collected": "Amount"}),
            label_column="Semester",
            value_column="Amount",
            color=theme.INCOME,
        ),
        key="dues_by_semester",
    )
