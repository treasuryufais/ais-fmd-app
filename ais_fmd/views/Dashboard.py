"""
Financial dashboard.

The original was 562 lines with every calculation inlined between layout calls.
Everything numeric now lives in `domain.budgets`, so this file is filters,
layout and chart selection.

Fixes visible here:
  F11  expenses use inverse delta colouring -- rising spend no longer reads green
  F12  % spent guards division by zero instead of producing inf
  F14  deltas are preformatted currency, not bare floats
  visual: bullet chart replaces the continuous blue ramp; sorted bars replace
          the two mismatched donuts; a waterfall shows where the money went
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain.terms import attach_semester, ordered_semesters, previous_semester
from ais_fmd.ui import charts, shell, theme

auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Financial Dashboard",
    "Budget against actual, spending trends, and where the money came from and went.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined", "Add an academic term before using the dashboard.")
    st.stop()

# --- Filters -----------------------------------------------------------------

st.sidebar.header("Filters")
selected_semester = st.sidebar.selectbox(
    "Semester", semesters, index=len(semesters) - 1, key="dash_semester"
)

committee_options = ["All committees"] + [
    committee_name(committee_id) for committee_id in BUDGETED_COMMITTEE_IDS
]
selected_committee = st.sidebar.selectbox(
    "Committee", committee_options, key="dash_committee"
)
committee_filter = None if selected_committee == "All committees" else selected_committee

# --- Headline metrics --------------------------------------------------------

totals = budget_domain.semester_totals(bundle.transactions, bundle.terms, selected_semester)
prior = previous_semester(bundle.terms, selected_semester)
prior_totals = (
    budget_domain.semester_totals(bundle.transactions, bundle.terms, prior)
    if prior
    else {"income": 0.0, "expenses": 0.0, "net": 0.0, "count": 0}
)

# Trend series for the sparklines, so each figure carries its own context.
history = budget_domain.historical_budget_vs_actual(
    bundle.transactions, bundle.budgets, bundle.terms, committee_filter
)
income_trend = [
    budget_domain.semester_totals(bundle.transactions, bundle.terms, semester)["income"]
    for semester in semesters
]
expense_trend = [
    budget_domain.semester_totals(bundle.transactions, bundle.terms, semester)["expenses"]
    for semester in semesters
]

shell.metric_row(
    [
        {
            "label": "Income",
            "value": totals["income"],
            "delta": totals["income"] - prior_totals["income"],
            "trend": income_trend,
        },
        {
            "label": "Expenses",
            "value": totals["expenses"],
            "delta": totals["expenses"] - prior_totals["expenses"],
            "inverse": True,  # F11
            "trend": expense_trend,
        },
        {
            "label": "Net",
            "value": totals["net"],
            "delta": totals["net"] - prior_totals["net"],
        },
        {
            "label": "Transactions",
            "value": totals["count"],
            "delta": totals["count"] - prior_totals["count"],
            "currency": False,
        },
    ]
)

if prior:
    st.caption(f"Deltas compare against {prior}.")

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Budget vs actual --------------------------------------------------------

st.subheader("Budget health")

summary = budget_domain.budget_vs_actual(
    bundle.transactions, bundle.budgets, bundle.terms, selected_semester
)
if committee_filter:
    summary = summary[summary["Committee_Name"] == committee_filter]

if summary.empty:
    shell.empty_state(
        "No budget data for this selection",
        "Set committee budgets for this term on the Treasury page.",
    )
else:
    over = summary[summary["Status"] == "over"]
    approaching = summary[summary["Status"] == "approaching"]
    banner_parts = []
    if len(over):
        banner_parts.append(
            f"{shell.pill(f'{len(over)} over budget', 'over')}"
        )
    if len(approaching):
        banner_parts.append(
            f"{shell.pill(f'{len(approaching)} approaching', 'approaching')}"
        )
    if banner_parts:
        st.markdown(" &nbsp; ".join(banner_parts), unsafe_allow_html=True)

    chart_column, table_column = st.columns([3, 2])
    with chart_column:
        shell.chart(charts.budget_bullet(summary), key="budget_bullet")
    with table_column:
        display = summary.copy()
        display["% Spent"] = display["% Spent"].map(
            lambda value: "—" if value is None or value != value else f"{value:.1f}%"
        )
        shell.dataframe(
            display[["Committee_Name", "Budget", "Spent", "Remaining", "% Spent", "Status"]].rename(
                columns={"Committee_Name": "Committee"}
            ),
            column_config={
                "Budget": st.column_config.NumberColumn(format="$%.2f"),
                "Spent": st.column_config.NumberColumn(format="$%.2f"),
                "Remaining": st.column_config.NumberColumn(format="$%.2f"),
            },
            height=charts._height(len(display)),
        )

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Where the money went ----------------------------------------------------

st.subheader("Where the money went")

scoped = attach_semester(bundle.transactions, bundle.terms)
scoped = scoped[scoped["Semester"] == selected_semester]
if committee_filter:
    target_id = next(
        (cid for cid in BUDGETED_COMMITTEE_IDS if committee_name(cid) == committee_filter),
        None,
    )
    if target_id is not None:
        scoped = scoped[scoped["budget_category"] == target_id]

waterfall_column, split_column = st.columns([3, 2])

with waterfall_column:
    spending = budget_domain.spending_by_committee(scoped, budgeted_only=True)
    labels = ["Income"] + spending["Committee_Name"].head(8).tolist() + ["Net"]
    values = (
        [totals["income"]]
        + [-float(value) for value in spending["Spent"].head(8)]
        + [0.0]
    )
    measures = ["relative"] + ["relative"] * min(8, len(spending)) + ["total"]
    shell.chart(
        charts.waterfall(labels, values, measures, title=f"{selected_semester} flow"),
        key="waterfall",
    )

with split_column:
    tab_expense, tab_income = st.tabs(["Expenses", "Income"])
    with tab_expense:
        expense_split = budget_domain.categorize_flow(scoped, budget_domain.EXPENSE)
        shell.chart(
            charts.ranked_bar(
                expense_split,
                label_column="Category",
                value_column="Amount",
                color=theme.EXPENSE,
            ),
            key="expense_split",
        )
    with tab_income:
        income_split = budget_domain.categorize_flow(scoped, budget_domain.INCOME)
        shell.chart(
            charts.ranked_bar(
                income_split,
                label_column="Category",
                value_column="Amount",
                color=theme.INCOME,
            ),
            key="income_split",
        )

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Trend and burn rate -----------------------------------------------------

st.subheader("Across semesters")

trend_column, burn_column = st.columns([3, 2])

with trend_column:
    shell.chart(charts.trend_bars(history), key="trend")

with burn_column:
    st.markdown("**Projected burn**")
    burn = budget_domain.burn_rate_projection(
        bundle.transactions, bundle.budgets, bundle.terms, selected_semester
    )
    if committee_filter:
        burn = burn[burn["Committee_Name"] == committee_filter]
    at_risk = burn[burn["Note"].isin({"Projected to exhaust before term end", "Already over budget"})]
    if at_risk.empty:
        shell.empty_state("Nothing projected to overrun", "Every committee finishes within budget at the current pace.")
    else:
        shell.dataframe(
            at_risk[["Committee_Name", "Daily Burn", "Projected Exhaustion", "Note"]].rename(
                columns={"Committee_Name": "Committee", "Daily Burn": "Per day"}
            ),
            column_config={
                "Per day": st.column_config.NumberColumn(format="$%.2f"),
                "Projected Exhaustion": st.column_config.DateColumn(format="MMM D, YYYY"),
            },
        )
