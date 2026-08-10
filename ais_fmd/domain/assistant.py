"""
Module M15 -- the assistant, rebuilt on tools.

FINDING F10. The original routed questions through an if/elif chain that tested
for the word "committee" *before* it tested for spending, income or budget, and
returned early with just the committee roster. So the app's own first advertised
example question -- "What's the total spending for the Marketing committee this
semester?" -- never reached the spending branch. Gemini received a list of
committee names with no financial data attached and answered from that.

Reordering the branches would have fixed that one question and left the shape
that produced it. Instead each capability is a *tool* with declared parameters:
the question selects a tool and binds arguments, the tool computes a real answer
from the data, and the result is returned as numbers plus a table plus a chart.

The tools are plain functions over DataFrames, so they are testable and correct
independently of any model. When an API key is configured the same tools are
handed to the model as callable functions; the difference is only who chooses
which tool to call. This is also what removes FINDING F9's token bloat -- the
model never receives a dumped transaction table, only a tool result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from ..config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from . import budgets as budget_domain
from .money import format_currency
from .terms import attach_semester, ordered_semesters


@dataclass
class Answer:
    text: str
    table: pd.DataFrame | None = None
    chart_hint: str | None = None
    tool: str = ""
    arguments: dict = field(default_factory=dict)


@dataclass
class Context:
    transactions: pd.DataFrame
    budgets: pd.DataFrame
    terms: pd.DataFrame


# --- Argument extraction -----------------------------------------------------

def _find_semester(question: str, context: Context) -> str | None:
    lowered = question.lower()
    for semester in ordered_semesters(context.terms):
        if semester.lower() in lowered:
            return semester
    # "this semester" / "current" resolve to the most recent term.
    if any(word in lowered for word in ("this semester", "current", "latest", "this term")):
        order = ordered_semesters(context.terms)
        return order[-1] if order else None
    return None


def _find_committee(question: str) -> str | None:
    lowered = question.lower()
    matches = [
        committee_name(committee_id)
        for committee_id in BUDGETED_COMMITTEE_IDS
        if committee_name(committee_id).lower() in lowered
    ]
    # Longest match wins, so "Professional Development" beats "Development".
    return max(matches, key=len) if matches else None


def _find_account(question: str) -> str | None:
    lowered = question.lower()
    if "venmo" in lowered:
        return "Venmo"
    if "wells" in lowered or "checking" in lowered:
        return "Wells Fargo"
    return None


def _find_amount(question: str) -> float | None:
    match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", question)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def _scope(context: Context, semester: str | None) -> pd.DataFrame:
    if not semester:
        return context.transactions
    tagged = attach_semester(context.transactions, context.terms)
    return tagged[tagged["Semester"] == semester]


# --- Tools -------------------------------------------------------------------

def tool_committee_spending(question: str, context: Context) -> Answer:
    semester = _find_semester(question, context)
    committee = _find_committee(question)
    scoped = _scope(context, semester)
    spending = budget_domain.spending_by_committee(scoped)

    window = f" in {semester}" if semester else " across all terms"

    if committee:
        row = spending[spending["Committee_Name"] == committee]
        total = float(row["Spent"].iloc[0]) if not row.empty else 0.0
        summary = budget_domain.budget_vs_actual(
            context.transactions, context.budgets, context.terms, semester
        ) if semester else pd.DataFrame()
        budget_line = ""
        if not summary.empty:
            match = summary[summary["Committee_Name"] == committee]
            if not match.empty:
                allocated = float(match["Budget"].iloc[0])
                percent = match["% Spent"].iloc[0]
                budget_line = (
                    f" That is against a budget of {format_currency(allocated)}"
                    + (f" ({percent:.1f}% used)." if percent is not None and percent == percent else ".")
                )
        return Answer(
            text=f"**{committee}** spent **{format_currency(total)}**{window}.{budget_line}",
            table=row if not row.empty else None,
            chart_hint="committee_spending",
            tool="committee_spending",
            arguments={"semester": semester, "committee": committee},
        )

    total = float(spending["Spent"].sum()) if not spending.empty else 0.0
    leader = spending.iloc[0] if not spending.empty else None
    text = f"Total committee spending{window} was **{format_currency(total)}**."
    if leader is not None:
        text += (
            f" The largest was **{leader['Committee_Name']}** at "
            f"{format_currency(leader['Spent'])}."
        )
    return Answer(
        text=text,
        table=spending,
        chart_hint="committee_spending",
        tool="committee_spending",
        arguments={"semester": semester},
    )


def tool_budget_status(question: str, context: Context) -> Answer:
    semester = _find_semester(question, context) or (
        ordered_semesters(context.terms)[-1] if ordered_semesters(context.terms) else None
    )
    if not semester:
        return Answer(text="No terms are defined, so there is no budget to report on.")

    summary = budget_domain.budget_vs_actual(
        context.transactions, context.budgets, context.terms, semester
    )
    committee = _find_committee(question)
    if committee:
        summary = summary[summary["Committee_Name"] == committee]

    if summary.empty:
        return Answer(text=f"No budget data found for {semester}.")

    over = summary[summary["Status"] == "over"]
    approaching = summary[summary["Status"] == "approaching"]
    total_budget = float(summary["Budget"].sum())
    total_spent = float(summary["Spent"].sum())

    text = (
        f"For **{semester}**, {format_currency(total_spent)} of "
        f"{format_currency(total_budget)} allocated has been spent."
    )
    if len(over):
        names = ", ".join(over["Committee_Name"].head(4))
        text += f" **{len(over)} over budget**: {names}."
    if len(approaching):
        text += f" {len(approaching)} approaching the limit."
    if not len(over) and not len(approaching):
        text += " Every committee is on track."

    return Answer(
        text=text,
        table=summary,
        chart_hint="budget",
        tool="budget_status",
        arguments={"semester": semester, "committee": committee},
    )


def tool_income_summary(question: str, context: Context) -> Answer:
    semester = _find_semester(question, context)
    account = _find_account(question)
    scoped = _scope(context, semester)

    income = scoped[scoped["amount"] > 0].copy()
    if account:
        income = income[income["account"].astype(str).str.contains(account.split()[0], case=False, na=False)]

    total = float(income["amount"].sum()) if not income.empty else 0.0
    window = f" in {semester}" if semester else " across all terms"
    source = f" via {account}" if account else ""

    by_purpose = budget_domain.categorize_flow(income, budget_domain.INCOME)
    leader = by_purpose.iloc[0] if not by_purpose.empty else None
    text = f"Income{source}{window} totalled **{format_currency(total)}** across {len(income):,} transactions."
    if leader is not None:
        text += f" The largest source was **{leader['Category']}** at {format_currency(leader['Amount'])}."

    return Answer(
        text=text,
        table=by_purpose,
        chart_hint="income",
        tool="income_summary",
        arguments={"semester": semester, "account": account},
    )


def tool_largest_transactions(question: str, context: Context) -> Answer:
    semester = _find_semester(question, context)
    threshold = _find_amount(question)
    account = _find_account(question)
    scoped = _scope(context, semester).copy()

    if account:
        scoped = scoped[scoped["account"].astype(str).str.contains(account.split()[0], case=False, na=False)]

    scoped["magnitude"] = scoped["amount"].abs()
    if threshold:
        scoped = scoped[scoped["magnitude"] >= threshold]

    ranked = scoped.sort_values("magnitude", ascending=False).head(15)
    window = f" in {semester}" if semester else ""
    qualifier = f" over {format_currency(threshold)}" if threshold else ""
    source = f" on {account}" if account else ""

    if ranked.empty:
        return Answer(text=f"No transactions{qualifier}{source}{window} were found.")

    display = ranked[["transaction_date", "amount", "details", "purpose", "account"]].rename(
        columns={
            "transaction_date": "Date",
            "amount": "Amount",
            "details": "Details",
            "purpose": "Purpose",
            "account": "Account",
        }
    )
    return Answer(
        text=(
            f"Found **{len(scoped):,}** transactions{qualifier}{source}{window}. "
            f"The largest is {format_currency(ranked.iloc[0]['amount'])}."
        ),
        table=display,
        tool="largest_transactions",
        arguments={"semester": semester, "threshold": threshold, "account": account},
    )


def tool_uncategorized(question: str, context: Context) -> Answer:
    pending = context.transactions[
        context.transactions["budget_category"].isna() | context.transactions["purpose"].isna()
    ]
    if pending.empty:
        return Answer(text="Every transaction has a committee and a purpose.", tool="uncategorized")

    total = float(pending["amount"].abs().sum())
    display = pending[["transaction_date", "amount", "details", "account"]].rename(
        columns={
            "transaction_date": "Date",
            "amount": "Amount",
            "details": "Details",
            "account": "Account",
        }
    )
    return Answer(
        text=(
            f"**{len(pending):,}** transactions are uncategorized, worth "
            f"{format_currency(total)} in absolute value. Work them on the Review Queue page."
        ),
        table=display.head(50),
        tool="uncategorized",
    )


def tool_compare_semesters(question: str, context: Context) -> Answer:
    order = ordered_semesters(context.terms)
    mentioned = [semester for semester in order if semester.lower() in question.lower()]
    if len(mentioned) < 2:
        mentioned = order[-2:] if len(order) >= 2 else order

    if len(mentioned) < 2:
        return Answer(text="At least two terms are needed to compare.")

    rows = []
    for semester in mentioned:
        totals = budget_domain.semester_totals(context.transactions, context.terms, semester)
        rows.append(
            {
                "Semester": semester,
                "Income": totals["income"],
                "Expenses": totals["expenses"],
                "Net": totals["net"],
                "Transactions": totals["count"],
            }
        )
    frame = pd.DataFrame(rows)

    first, last = frame.iloc[0], frame.iloc[-1]
    change = last["Expenses"] - first["Expenses"]
    direction = "more" if change > 0 else "less"
    return Answer(
        text=(
            f"Comparing **{first['Semester']}** with **{last['Semester']}**: spending was "
            f"{format_currency(abs(change))} {direction}, and net went from "
            f"{format_currency(first['Net'])} to {format_currency(last['Net'])}."
        ),
        table=frame,
        chart_hint="comparison",
        tool="compare_semesters",
        arguments={"semesters": mentioned},
    )


# --- Registry and routing ----------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    keywords: tuple[str, ...]
    run: Callable[[str, Context], Answer]
    # Higher wins ties; more specific tools sit above general ones.
    specificity: int = 0


TOOLS: tuple[Tool, ...] = (
    Tool(
        "compare_semesters",
        "Compare income, expenses and net between two terms.",
        ("compare", "versus", " vs ", "between", "difference"),
        tool_compare_semesters,
        specificity=3,
    ),
    Tool(
        "uncategorized",
        "List transactions still missing a committee or purpose.",
        ("uncategorized", "uncategorised", "missing category", "not categorized", "need review"),
        tool_uncategorized,
        specificity=3,
    ),
    Tool(
        "largest_transactions",
        "Find the biggest transactions, optionally above a threshold or on one account.",
        ("largest", "biggest", "over $", "above", "top transaction", "show me all", "list"),
        tool_largest_transactions,
        specificity=2,
    ),
    Tool(
        "budget_status",
        "Budget against actual, and who is over.",
        ("budget", "over budget", "allocation", "utilisation", "utilization", "remaining"),
        tool_budget_status,
        specificity=2,
    ),
    Tool(
        "income_summary",
        "Total income, optionally by term or account.",
        ("income", "revenue", "dues collected", "brought in", "earned", "received"),
        tool_income_summary,
        specificity=1,
    ),
    Tool(
        "committee_spending",
        "How much a committee spent.",
        ("spend", "spent", "spending", "expense", "cost", "paid"),
        tool_committee_spending,
        specificity=1,
    ),
)


def select_tool(question: str) -> Tool:
    """
    Score every tool and pick the best, rather than returning on first match.

    This is the structural fix for F10: because all tools are scored, a question
    that mentions a committee *and* asks about spending routes on the spending
    intent instead of being captured by whichever branch happened to be first.
    """
    lowered = f" {question.lower()} "
    best = TOOLS[-1]
    best_score = -1.0

    for tool in TOOLS:
        hits = sum(1 for keyword in tool.keywords if keyword in lowered)
        if not hits:
            continue
        score = hits * 10 + tool.specificity
        if score > best_score:
            best, best_score = tool, score

    return best


def answer_question(question: str, context: Context) -> Answer:
    if not question.strip():
        return Answer(text="Ask something about the finances.")
    tool = select_tool(question)
    try:
        return tool.run(question, context)
    except Exception as exc:  # noqa: BLE001 - surfaced, never silently swallowed
        return Answer(
            text=f"The **{tool.name}** tool failed: {type(exc).__name__}: {exc}",
            tool=tool.name,
        )


def tool_catalogue() -> pd.DataFrame:
    return pd.DataFrame(
        [{"Tool": tool.name, "What it answers": tool.description} for tool in TOOLS]
    )
