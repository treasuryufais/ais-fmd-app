"""Landing page: orientation plus the handful of numbers worth seeing first."""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import quality, reconcile
from ais_fmd.domain.terms import ordered_semesters
from ais_fmd.ui import shell

identity = auth.current_user()

shell.environment_banner()
shell.page_header(
    "UF AIS Financial Management",
    "Transactions, committee budgets, and reconciliation for the Association for "
    "Information Systems.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state(
        "No terms defined yet",
        "Add an academic term from Treasury → Manage Terms to get started.",
    )
    st.stop()

latest = semesters[-1]
totals = budget_domain.semester_totals(bundle.transactions, bundle.terms, latest)

st.subheader(f"{latest} at a glance")
shell.metric_row(
    [
        {"label": "Income", "value": totals["income"]},
        {"label": "Expenses", "value": totals["expenses"], "inverse": True},
        {"label": "Net", "value": totals["net"]},
        {"label": "Transactions", "value": totals["count"], "currency": False},
    ]
)

st.markdown("")

# --- Things that need attention ---------------------------------------------

col_left, col_right = st.columns(2)

with col_left:
    st.markdown("#### Needs attention")
    issues = quality.run_all_checks(bundle.transactions, bundle.budgets, bundle.terms)
    actionable = [issue for issue in issues if issue.severity in {"critical", "high", "medium"}]
    if not actionable:
        shell.empty_state("Nothing flagged", "All data-quality checks pass.")
    else:
        for issue in actionable[:4]:
            st.markdown(
                f"{shell.pill(issue.severity, 'over' if issue.severity in {'critical', 'high'} else 'approaching')} "
                f"&nbsp;**{issue.title}** — {issue.count:,} row(s)",
                unsafe_allow_html=True,
            )
        st.caption("Full detail on the Data Quality page.")

with col_right:
    st.markdown("#### Reconciliation")
    balances = repo.load_statement_balances()
    results = reconcile.reconcile_all(bundle.transactions, balances)
    if not results:
        shell.empty_state("No statement balances recorded", "Add them on the Reconciliation page.")
    else:
        unbalanced = [result for result in results if not result.balanced]
        if unbalanced:
            st.markdown(
                f"{shell.pill('out of balance', 'over')} &nbsp;"
                f"**{len(unbalanced)} of {len(results)}** periods do not reconcile.",
                unsafe_allow_html=True,
            )
            for result in unbalanced[:3]:
                shell.say(f"• {result.explain()}", caption=True)
        else:
            st.markdown(
                f"{shell.pill('balanced', 'on track')} &nbsp;"
                f"All {len(results)} statement periods reconcile.",
                unsafe_allow_html=True,
            )

st.markdown("")
st.markdown("#### Where things are")

st.markdown(
    """
| Page | What it is for |
| --- | --- |
| **Dashboard** | Budget against actual, spending trends, income and expense breakdown |
| **Transactions** | Search, filter and bulk-edit categorisation |
| **Review Queue** | Work through what the categorizer could not resolve on its own |
| **Reconciliation** | Prove the ledger agrees with the bank |
| **Treasury** | Upload statements, set budgets, manage terms |
| **Data Quality** | Everything the integrity checks found |
| **Audit Log** | Who changed what, and when |
| **Assistant** | Ask questions about the data in plain language |
"""
)

if identity.role < auth.Role.TREASURER:
    st.caption(
        f"Signed in as **{identity.role.label}** — some pages are read-only or hidden."
    )
