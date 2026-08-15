"""
Budget vs actual, variance, and burn rate.

Pulled out of Financial_Dashboard.py, where it was interleaved with layout code
and could not be tested. Every function here takes DataFrames and returns
DataFrames -- no Streamlit import anywhere in this module.
"""

from __future__ import annotations

import pandas as pd

from ..config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from .money import safe_percent
from .terms import attach_semester, prepare_terms

# Committee spend is "money out" -- negative amounts, reported as positive.
EXPENSE = "expense"
INCOME = "income"


def _committee_names(series: pd.Series) -> pd.Series:
    return series.map(committee_name)


def spending_by_committee(
    df_transactions: pd.DataFrame,
    *,
    budgeted_only: bool = True,
) -> pd.DataFrame:
    """Total expense per committee. Columns: Committee_Name, Spent."""
    empty = pd.DataFrame(columns=["Committee_Name", "Spent"])
    if df_transactions.empty:
        return empty

    expenses = df_transactions[df_transactions["amount"] < 0].copy()
    if budgeted_only:
        expenses = expenses[expenses["budget_category"].isin(BUDGETED_COMMITTEE_IDS)]
    if expenses.empty:
        return empty

    expenses["Spent"] = expenses["amount"].abs()
    expenses["Committee_Name"] = _committee_names(expenses["budget_category"])
    return (
        expenses.groupby("Committee_Name", as_index=False)["Spent"]
        .sum()
        .sort_values("Spent", ascending=False)
    )


def budget_vs_actual(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> pd.DataFrame:
    """
    Budget, Spent, Remaining and % Spent for every budgeted committee.

    Committees with a budget but no spend appear with Spent = 0; committees with
    spend but no budget appear with Budget = 0 and a null % Spent rather than an
    infinity (FINDING F12).
    """
    return _budget_vs_actual_tagged(
        attach_semester(df_transactions, df_terms), df_budgets, df_terms, semester
    )


def _budget_vs_actual_tagged(
    tagged: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> pd.DataFrame:
    """
    `budget_vs_actual` over a frame that already carries a Semester column.

    FINDING (performance). Tagging is O(rows), and `historical_budget_vs_actual`
    calls this once per semester -- so the whole transactions table was tagged
    once per term on every re-run. Splitting the tagging out lets the caller
    hoist it out of its loop.
    """
    columns = ["Committee_Name", "Budget", "Spent", "Remaining", "% Spent", "Status"]
    term_id = None
    if not df_terms.empty:
        match = df_terms[df_terms["Semester"] == semester]
        if not match.empty:
            term_id = match.iloc[0]["TermID"]

    budgets = pd.DataFrame(columns=["Committee_Name", "Budget"])
    if term_id is not None and not df_budgets.empty:
        rows = df_budgets[df_budgets["termid"] == term_id].copy()
        rows = rows[rows["committeeid"].isin(BUDGETED_COMMITTEE_IDS)]
        if not rows.empty:
            rows["Committee_Name"] = _committee_names(rows["committeeid"])
            budgets = rows.groupby("Committee_Name", as_index=False)["budget_amount"].sum()
            budgets = budgets.rename(columns={"budget_amount": "Budget"})

    scoped = tagged[tagged["Semester"] == semester]
    spending = spending_by_committee(scoped)

    combined = budgets.merge(spending, on="Committee_Name", how="outer")
    if combined.empty:
        return pd.DataFrame(columns=columns)

    combined["Budget"] = combined["Budget"].fillna(0.0)
    combined["Spent"] = combined["Spent"].fillna(0.0)
    combined["Remaining"] = combined["Budget"] - combined["Spent"]
    combined["% Spent"] = [
        safe_percent(spent, budget)
        for spent, budget in zip(combined["Spent"], combined["Budget"])
    ]
    combined["Status"] = combined.apply(_status_for_row, axis=1)
    return combined.sort_values("Spent", ascending=False)[columns].reset_index(drop=True)


def _status_for_row(row: pd.Series) -> str:
    """
    Semantic state, so charts can colour by meaning rather than by magnitude.

    The original chart put 40% spent and 110% spent on the same continuous blue
    ramp, so the one state a treasurer urgently needs to see did not stand out.
    """
    percent = row["% Spent"]
    if percent is None or pd.isna(percent):
        return "unbudgeted" if row["Spent"] > 0 else "no budget"
    if percent > 100:
        return "over"
    if percent >= 85:
        return "approaching"
    return "on track"


def semester_totals(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> dict[str, float]:
    """Headline figures for one semester."""
    tagged = attach_semester(df_transactions, df_terms)
    return _totals_for(tagged[tagged["Semester"] == semester])


def _totals_for(scoped: pd.DataFrame) -> dict[str, float]:
    if scoped.empty:
        return {"income": 0.0, "expenses": 0.0, "net": 0.0, "count": 0}

    income = float(scoped[scoped["amount"] > 0]["amount"].sum())
    expenses = float(abs(scoped[scoped["amount"] < 0]["amount"].sum()))
    return {
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "count": int(len(scoped)),
    }


def semester_totals_by_semester(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semesters: list[str],
) -> dict[str, dict[str, float]]:
    """
    `semester_totals` for many semesters, tagging the table exactly once.

    The Dashboard's sparklines need a figure per semester, and computing them
    one call at a time re-tagged the whole table on every iteration -- twice
    over, since the income and expense series were built in separate
    comprehensions that each recomputed the identical dict.
    """
    tagged = attach_semester(df_transactions, df_terms)
    if tagged.empty:
        return {semester: _totals_for(tagged) for semester in semesters}

    groups = dict(list(tagged.groupby("Semester", sort=False)))
    empty = tagged.iloc[0:0]
    return {semester: _totals_for(groups.get(semester, empty)) for semester in semesters}


def historical_budget_vs_actual(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    committee: str | None = None,
) -> pd.DataFrame:
    """
    Budget and spend per semester, chronologically ordered.

    FINDING (performance). This built a full per-committee `budget_vs_actual`
    table for every semester and then threw all but two column sums away --
    nine table builds, each with its own filter, groupby and merge, on every
    dashboard re-run. Only the sums are needed, and a sum over an outer merge
    of two per-committee tables equals the sum of each side taken separately
    (every committee appears at most once on each side). So both sides are
    aggregated in a single grouped pass instead.
    """
    terms = prepare_terms(df_terms)
    if terms.empty:
        return pd.DataFrame(columns=["Semester", "Budget", "Spent", "% Spent"])

    semesters = list(terms["Semester"])

    # Budget side: one row per (term, committee), summed per semester.
    budget_by_semester: dict[str, float] = {}
    if not df_budgets.empty:
        rows = df_budgets[df_budgets["committeeid"].isin(BUDGETED_COMMITTEE_IDS)]
        if committee:
            rows = rows[_committee_names(rows["committeeid"]) == committee]
        if not rows.empty:
            # First-match semester -> TermID, mirroring the per-semester lookup.
            term_ids = df_terms.drop_duplicates(subset=["Semester"]).set_index("Semester")["TermID"]
            per_term = rows.groupby("termid")["budget_amount"].sum()
            for semester in semesters:
                term_id = term_ids.get(semester)
                if term_id is not None:
                    budget_by_semester[semester] = float(per_term.get(term_id, 0.0))

    # Spend side: budgeted expenses only, summed per semester.
    spent_by_semester: dict[str, float] = {}
    tagged = attach_semester(df_transactions, df_terms)
    if not tagged.empty:
        expenses = tagged[
            (tagged["amount"] < 0) & tagged["budget_category"].isin(BUDGETED_COMMITTEE_IDS)
        ]
        if committee:
            expenses = expenses[_committee_names(expenses["budget_category"]) == committee]
        if not expenses.empty:
            spent_by_semester = (
                expenses["amount"].abs().groupby(expenses["Semester"]).sum().to_dict()
            )

    out = pd.DataFrame(
        [
            {
                "Semester": semester,
                "Budget": float(budget_by_semester.get(semester, 0.0)),
                "Spent": float(spent_by_semester.get(semester, 0.0)),
            }
            for semester in semesters
        ]
    )
    out["% Spent"] = [
        safe_percent(spent, budget) for spent, budget in zip(out["Spent"], out["Budget"])
    ]
    return out


def burn_rate_projection(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
    *,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Module M5. Project the date each committee exhausts its budget at the
    current pace, so overruns are visible before they happen rather than after.
    """
    from .terms import date_range_for_semester

    window = date_range_for_semester(df_terms, semester)
    summary = budget_vs_actual(df_transactions, df_budgets, df_terms, semester)
    columns = ["Committee_Name", "Budget", "Spent", "Daily Burn", "Projected Exhaustion", "Note"]
    if window is None or summary.empty:
        return pd.DataFrame(columns=columns)

    start, end = window
    now = as_of or pd.Timestamp.today().normalize()
    now = min(max(now, start), end)
    elapsed_days = max((now - start).days, 1)
    remaining_days = max((end - now).days, 0)

    rows = []
    for _, row in summary.iterrows():
        daily = row["Spent"] / elapsed_days
        note = ""
        exhaustion = None
        if row["Budget"] <= 0:
            note = "No budget allocated"
        elif daily <= 0:
            note = "No spending yet"
        else:
            days_left = (row["Budget"] - row["Spent"]) / daily
            if days_left < 0:
                note = "Already over budget"
            else:
                exhaustion = now + pd.Timedelta(days=float(days_left))
                if days_left > remaining_days:
                    note = "Finishes the term under budget"
                    exhaustion = None
                else:
                    note = "Projected to exhaust before term end"
        rows.append(
            {
                "Committee_Name": row["Committee_Name"],
                "Budget": row["Budget"],
                "Spent": row["Spent"],
                "Daily Burn": round(daily, 2),
                "Projected Exhaustion": exhaustion,
                "Note": note,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def categorize_flow(df: pd.DataFrame, direction: str) -> pd.DataFrame:
    """
    Income or expense grouped by purpose.

    The original built this with nested loops over a keyword dict, reassigning
    `.loc` on every match so the last keyword silently won. Grouping on the
    stored purpose is both correct and an order of magnitude less code.
    """
    if df.empty:
        return pd.DataFrame(columns=["Category", "Amount"])

    scoped = df[df["amount"] > 0] if direction == INCOME else df[df["amount"] < 0]
    if scoped.empty:
        return pd.DataFrame(columns=["Category", "Amount"])

    scoped = scoped.copy()
    scoped["Amount"] = scoped["amount"].abs()
    scoped["Category"] = scoped["purpose"].fillna("Uncategorized").replace("", "Uncategorized")
    return (
        scoped.groupby("Category", as_index=False)["Amount"]
        .sum()
        .sort_values("Amount", ascending=False)
        .reset_index(drop=True)
    )
