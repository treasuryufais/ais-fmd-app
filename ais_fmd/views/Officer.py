"""
Module M11 -- committee officer portal.

A scoped view for a committee chair: their budget, their spend, their remaining,
and a way to raise a reimbursement — without seeing or being able to change
anything belonging to another committee.

This is the page that depends on FINDING F2 being fixed. Under the original
shared-password model there was no way to give a chair sight of their own budget
without handing them the keys to everything.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import reimbursements as reimb
from ais_fmd.domain.money import format_currency, safe_percent
from ais_fmd.domain.terms import attach_semester, default_semester_index, ordered_semesters
from ais_fmd.ui import charts, shell, theme

identity = auth.require(auth.Role.OFFICER)

shell.environment_banner()
shell.page_header(
    "My Committee",
    "Your budget, your spending, and your outstanding reimbursements.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined")
    st.stop()

# --- Which committee ---------------------------------------------------------
#
# In production the committee comes from the signed-in profile. A treasurer or
# admin can look at any committee; an officer sees only their own.

can_choose = identity.can(auth.Role.TREASURER) or identity.committee_id is None

controls = st.columns([2, 2])
with controls[0]:
    if can_choose:
        committee_names = [committee_name(cid) for cid in BUDGETED_COMMITTEE_IDS]
        default_index = (
            committee_names.index(committee_name(identity.committee_id))
            if identity.committee_id in BUDGETED_COMMITTEE_IDS
            else 0
        )
        chosen_name = st.selectbox("Committee", committee_names, index=default_index)
        committee_id = next(
            cid for cid in BUDGETED_COMMITTEE_IDS if committee_name(cid) == chosen_name
        )
    else:
        committee_id = identity.committee_id
        chosen_name = committee_name(committee_id)
        st.markdown(f"**Committee**  \n{chosen_name}")

with controls[1]:
    semester = st.selectbox(
        "Semester",
        semesters,
        index=default_semester_index(bundle.transactions, bundle.terms),
        key="officer_semester",
    )

if not can_choose:
    st.caption(
        "You are seeing only your own committee. Treasurers can view any committee."
    )

# --- Position ----------------------------------------------------------------

summary = budget_domain.budget_vs_actual(
    bundle.transactions, bundle.budgets, bundle.terms, semester
)
row = summary[summary["Committee_Name"] == chosen_name]

if row.empty:
    shell.empty_state(
        f"No budget or spending recorded for {chosen_name} in {semester}",
        "Ask the treasurer to set an allocation for this term.",
    )
    st.stop()

position = row.iloc[0]
percent = safe_percent(position["Spent"], position["Budget"])

metrics = st.columns(4)
metrics[0].metric("Budget", format_currency(position["Budget"]))
metrics[1].metric("Spent", format_currency(position["Spent"]))
metrics[2].metric("Remaining", format_currency(position["Remaining"]))
metrics[3].metric("Used", "—" if percent is None else f"{percent:.0f}%")

status_style = {"over": "over", "approaching": "approaching", "on track": "on track"}
st.markdown(
    shell.pill(position["Status"], status_style.get(position["Status"], "muted")),
    unsafe_allow_html=True,
)
if percent is not None:
    st.progress(min(percent / 100, 1.0))

if position["Status"] == "over":
    shell.notify(
        "warning",
        f"{chosen_name} is over its allocation by "
        f"{format_currency(abs(position['Remaining']))}. Speak to the treasurer "
        f"before committing anything further.",
    )

# --- Spending ----------------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Your spending this term")

scoped = attach_semester(bundle.transactions, bundle.terms)
scoped = scoped[(scoped["Semester"] == semester) & (scoped["budget_category"] == committee_id)]

if scoped.empty:
    shell.empty_state("Nothing recorded against this committee yet")
else:
    table_column, chart_column = st.columns([3, 2])
    with table_column:
        display = scoped.sort_values("transaction_date", ascending=False)
        shell.dataframe(
            pd.DataFrame(
                {
                    "Date": pd.to_datetime(display["transaction_date"]).dt.date,
                    "Amount": display["amount"],
                    "Purpose": display["purpose"].fillna("—"),
                    "Details": display["details"].astype(str).str.slice(0, 70),
                }
            ),
            column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
            height=360,
        )
    with chart_column:
        by_purpose = budget_domain.categorize_flow(scoped, budget_domain.EXPENSE)
        shell.chart(
            charts.ranked_bar(
                by_purpose,
                label_column="Category",
                value_column="Amount",
                color=theme.EXPENSE,
            ),
            key="officer_purposes",
        )

# --- Reimbursements ----------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Reimbursements for this committee")

requests = repo.load_reimbursements()
if requests.empty:
    shell.empty_state(
        "No requests raised",
        "Submit one from the Reimbursements page — it will be pre-assigned to this committee.",
    )
else:
    mine = requests[requests["committee_id"] == committee_id]
    if mine.empty:
        shell.empty_state("No requests for this committee yet")
    else:
        outstanding = mine[mine["status"].isin([reimb.PENDING, reimb.APPROVED])]
        if not outstanding.empty:
            shell.say(
                f"{len(outstanding)} outstanding, totalling "
                f"{format_currency(outstanding['amount'].sum())} — this is committed "
                f"but not yet reflected in the spend figure above.",
                caption=True,
            )
        shell.dataframe(
            reimb.display_frame(mine),
            column_config={
                "Amount": st.column_config.NumberColumn(format="$%.2f"),
                "Submitted": st.column_config.DatetimeColumn(format="MMM D, YYYY"),
            },
        )
