"""
Module M15 -- the assistant.

Questions are answered by named, tested tools computing over the data, so every
number is checkable. Something picks the tool: a language model when one is
configured (a later change), otherwise the offline keyword router. Either way
`assistant.run_tool` checks what this user is allowed to see before answering.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth, settings
from ais_fmd.config.categories import committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import assistant
from ais_fmd.domain import roster as roster_domain
from ais_fmd.domain.terms import term_id_for_semester
from ais_fmd.ui import charts, shell, theme

identity = auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Assistant",
    "Ask about the finances, or where something is in the app. Answers are computed "
    "from the data by named tools, so every number is checkable.",
)

bundle = repo.load_bundle()

# --- Who is asking -------------------------------------------------------------

ROLE_NAMES = {
    auth.Role.MEMBER: "member",
    auth.Role.OFFICER: "officer",
    auth.Role.TREASURER: "treasurer",
    auth.Role.ADMIN: "admin",
}
own_committee = committee_name(identity.committee_id) or None

if settings.is_sandbox() and identity.role == auth.Role.OFFICER and own_committee is None:
    # In production an officer's committee comes from their profile. The sandbox
    # role switcher has no committee, so pick one here to test the scoping.
    own_committee = st.selectbox(
        "Sandbox: answer as the officer of",
        assistant.COMMITTEE_NAMES,
        key="assistant_test_committee",
        help="Sandbox only. Officers only ever get their own committee's figures.",
    )

scope = assistant.Scope(role=ROLE_NAMES[identity.role], committee=own_committee)


def _text(value: object) -> str:
    """A NULL-safe string. `value or ""` keeps a pandas NaN, which is truthy."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def _members_for(semester: str | None) -> dict[str, list]:
    """The stored roster for one term, as roster.Member. Treasurers only."""
    if not semester or not scope.sees_every_committee:
        return {}
    term_id = term_id_for_semester(bundle.terms, semester)
    stored = repo.load_members(term_id) if term_id else pd.DataFrame()
    members = [
        roster_domain.Member(
            full_name=_text(row.get("full_name")),
            match_key=_text(row.get("match_key")),
            alt_keys=tuple(p.strip() for p in _text(row.get("alt_keys")).split(",") if p.strip()),
            preferred_name=_text(row.get("preferred_name")),
            claims_paid=bool(row.get("claims_paid") or 0),
        )
        for row in stored.to_dict("records")
    ]
    return {semester: members} if members else {}


context = assistant.Context(
    transactions=bundle.transactions,
    budgets=bundle.budgets,
    terms=bundle.terms,
    scope=scope,
)
context.members = _members_for(assistant.current_semester(context))

if bundle.transactions.empty:
    shell.empty_state("No data to ask about yet")
    st.stop()

if scope.role == "officer":
    EXAMPLES = [
        "Catch me up on my committee",
        "How much is left in my budget?",
        "What did my committee spend this month?",
        "Show me my committee's latest charges",
        "How much in dues have we collected this semester?",
        "Where can I see my committee's budget?",
    ]
elif scope.sees_every_committee:
    EXAMPLES = [
        "Summarize this semester's finances",
        "Which committees are over budget?",
        "How much has Consulting spent this month?",
        "Who hasn't paid dues yet?",
        "Show me all transactions over $500",
        "Where do I upload a bank statement?",
    ]
else:
    EXAMPLES = [
        "How much of the budget has been spent this semester?",
        "How much in dues have we collected this semester?",
        "What was our total income this month?",
        "Where do I see unpaid dues?",
        "Where can I find the list of transactions?",
        "How does this semester compare to last semester?",
    ]

st.markdown("**Try one of these**")
example_columns = st.columns(3)
clicked_example = None
for position, example in enumerate(EXAMPLES):
    with example_columns[position % 3]:
        if st.button(example, key=f"example_{position}", width="stretch"):
            clicked_example = example

if "assistant_history" not in st.session_state:
    st.session_state.assistant_history = []

typed = st.chat_input("Ask a question about AIS finances…")
question = clicked_example or typed

for entry in st.session_state.assistant_history:
    with st.chat_message(entry["role"]):
        shell.say(entry["content"])

if question:
    st.session_state.assistant_history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        shell.say(question)

    with st.spinner("Working it out…"):
        result = assistant.answer_question(question, context)

    with st.chat_message("assistant"):
        shell.say(result.text)
        provenance = f"Answered by the `{result.tool}` tool"
        shown_args = {k: v for k, v in result.arguments.items() if k != "topic"}
        if shown_args:
            provenance += f" with {shown_args}"
        if result.source:
            provenance += f" · Check it on **{result.source}**"
        shell.say(provenance, caption=True)

        if result.table is not None and not result.table.empty:
            shell.dataframe(result.table.head(50))

        if result.status != assistant.ANSWERED:
            pass
        elif result.chart_hint == "committee_spending" and result.table is not None:
            if "Committee_Name" in result.table.columns and len(result.table) > 1:
                shell.chart(
                    charts.ranked_bar(
                        result.table,
                        label_column="Committee_Name",
                        value_column="Spent",
                        color=theme.active().expense,
                    ),
                    key=f"assistant_chart_{len(st.session_state.assistant_history)}",
                )
        elif result.chart_hint == "budget" and result.table is not None:
            shell.chart(
                charts.budget_bullet(result.table),
                key=f"assistant_budget_{len(st.session_state.assistant_history)}",
            )
        elif result.chart_hint == "income" and result.table is not None:
            shell.chart(
                charts.ranked_bar(
                    result.table,
                    label_column="Category",
                    value_column="Amount",
                    color=theme.active().income,
                ),
                key=f"assistant_income_{len(st.session_state.assistant_history)}",
            )
        elif result.chart_hint == "comparison" and result.table is not None:
            comparison = result.table.rename(columns={"Expenses": "Spent"})
            comparison["Budget"] = comparison["Income"]
            shell.chart(
                charts.trend_bars(comparison[["Semester", "Budget", "Spent"]]),
                key=f"assistant_compare_{len(st.session_state.assistant_history)}",
            )

    st.session_state.assistant_history.append({"role": "assistant", "content": result.text})

if st.session_state.assistant_history and st.button("Clear conversation"):
    st.session_state.assistant_history = []
    st.rerun()

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

with st.expander("How this works, and why it was rebuilt"):
    st.markdown(
        """
The previous assistant routed questions through an `if`/`elif` chain that tested
for the word **committee** before it tested for spending, income or budget — and
returned early with just the committee roster. Its own first advertised example
question, *"What's the total spending for the Marketing committee this semester?"*,
never reached the spending branch: the model received a list of names with no
financial data attached and answered from that.

Reordering those branches would have fixed one question and kept the shape that
produced it. Instead each capability is a **tool** with declared parameters. A
question selects a tool by score across all of them, the tool computes a real
answer from the data, and you get numbers, a table and a chart.

When a model is configured it only **chooses** the tool and its details (which
committee, which dates). It never sees a transaction and never writes the answer:
the text you read is the tool's output. Who may see what is checked in code after
the model has chosen, so an officer gets only their own committee's figures however
the question is phrased. A question no tool covers gets "I don't know".
"""
    )
    shell.dataframe(assistant.tool_catalogue())

    if settings.is_sandbox():
        st.caption(
            "Sandbox mode: no model is contacted. The keyword router chooses the tool, "
            "which is why the page works with no API key and no network."
        )
