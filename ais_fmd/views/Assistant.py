"""
Module M15 -- the assistant.

Runs entirely offline. Questions are answered by real tools computing over the
data, so the numbers are verifiable and the page costs nothing to use.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth, settings
from ais_fmd.data import repositories as repo
from ais_fmd.domain import assistant
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.ui import charts, shell, theme

auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Assistant",
    "Ask about the finances. Answers are computed from the data by named tools, "
    "not generated from a prompt — so every number is checkable.",
)

bundle = repo.load_bundle()
context = assistant.Context(
    transactions=bundle.transactions,
    budgets=bundle.budgets,
    terms=bundle.terms,
)

if bundle.transactions.empty:
    shell.empty_state("No data to ask about yet")
    st.stop()

EXAMPLES = [
    "What's the total spending for the Marketing committee this semester?",
    "Which committees are over budget?",
    "How much income came in via Venmo?",
    "Show me all transactions over $500",
    "Compare Fall 2025 and Spring 2026",
    "Find all uncategorized transactions",
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

    result = assistant.answer_question(question, context)

    with st.chat_message("assistant"):
        shell.say(result.text)
        st.caption(
            f"Answered by the `{result.tool}` tool"
            + (f" with {result.arguments}" if result.arguments else "")
        )

        if result.table is not None and not result.table.empty:
            shell.dataframe(result.table.head(50))

        if result.chart_hint == "committee_spending" and result.table is not None:
            if "Committee_Name" in result.table.columns and len(result.table) > 1:
                shell.chart(
                    charts.ranked_bar(
                        result.table,
                        label_column="Committee_Name",
                        value_column="Spent",
                        color=theme.EXPENSE,
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
                    color=theme.INCOME,
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

It also removes the token problem. The original injected the entire transaction
table into the prompt; here a model — when one is configured — would only choose
which tool to call and read back its result.
"""
    )
    shell.dataframe(assistant.tool_catalogue())

    if settings.is_sandbox():
        st.caption(
            "Sandbox mode: no model is contacted. The tools are the whole system, "
            "which is why the page works with no API key and no network."
        )
