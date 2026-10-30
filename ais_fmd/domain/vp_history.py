"""
"Where were you at this point in past terms?"

The My Committee page says "95% of the budget is spent and 38% of the term has
passed." That is half a comparison: it does not say whether 95% at that moment is
unusual for this committee. This module supplies the other half by finding how
much the committee had spent at the same fraction of each earlier term.

Two decisions worth knowing about:

  * "Now" is the earlier of today and the date the data reaches. If statements
    were last uploaded on Oct 1 and it is Oct 12, comparing against Oct 12 would
    make this term look artificially low, because Oct 1-12 are not in the data.
  * Terms are lined up by fraction of the term elapsed, not by date, so a long
    term and a short one still compare like for like. Each term's spending is
    also shown against that term's own budget, since budgets differ per term.

Descriptive only -- what happened before. Forecasting belongs to the Scenario
Planner team; this table is the sort of thing a forecast could take as input.

No Streamlit here, so it is testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..config.categories import committee_name
from . import vp_metrics
from .money import safe_percent
from .terms import date_range_for_semester, ordered_semesters

MAX_TERMS = 4

# Within this many percentage points of the usual, the sentence calls it "about
# the same" rather than "more" or "less".
SIMILAR_WITHIN = 10.0

TABLE_COLUMNS = ["Semester", "Same season", "Spent by this point", "Of its budget", "Ended the term at"]


@dataclass(frozen=True)
class PastComparison:
    semester: str
    elapsed_percent: float  # how far through the current term the data reaches
    as_of: pd.Timestamp
    current_spent: float
    current_percent: float | None  # of the current term's budget
    table: pd.DataFrame  # one row per past term, most recent first


def _season(semester: str) -> str:
    """'Fall 2025' -> 'Fall'."""
    parts = str(semester).split()
    return parts[0].title() if parts else ""


def _spent_between(expenses: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    if expenses.empty:
        return 0.0
    dates = pd.to_datetime(expenses["transaction_date"], errors="coerce").dt.normalize()
    return float(expenses.loc[dates.between(start.normalize(), end.normalize()), "amount"].abs().sum())


def same_point_in_past_terms(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    line_ids: tuple[int, ...],
    semester: str,
    *,
    through: pd.Timestamp | None,
    today: pd.Timestamp,
    max_terms: int = MAX_TERMS,
) -> PastComparison | None:
    """
    The committee's spending at the same point in each of its last few terms, or
    None when there is nothing meaningful to compare (data does not reach the
    term yet, too early in the term, or no earlier terms with data).

    Takes the whole ledger and filters to `line_ids` itself, so what it reports
    cannot depend on the caller having scoped the data first.
    """
    if through is None or not line_ids:
        return None

    as_of = min(pd.Timestamp(today).normalize(), pd.Timestamp(through).normalize())
    elapsed = vp_metrics.term_elapsed_percent(df_terms, semester, as_of=as_of)
    if elapsed is None or elapsed < vp_metrics.MIN_ELAPSED_FOR_PROJECTION:
        return None

    window = date_range_for_semester(df_terms, semester)
    if window is None:
        return None

    names = [committee_name(cid) for cid in line_ids]
    history = vp_metrics.historical(df_transactions, df_budgets, df_terms, names).set_index("Semester")

    mine = (
        df_transactions
        if df_transactions.empty
        else df_transactions[df_transactions["budget_category"].isin(line_ids)]
    )
    expenses = mine[mine["amount"] < 0] if not mine.empty else mine

    current_start, _ = window
    current_spent = _spent_between(expenses, current_start, as_of)
    current_budget = float(history.loc[semester, "Budget"]) if semester in history.index else 0.0
    current_percent = safe_percent(current_spent, current_budget)

    order = ordered_semesters(df_terms)
    earlier = order[: order.index(semester)] if semester in order else []

    current_season = _season(semester)
    rows = []
    for past in reversed(earlier):  # most recent first
        past_window = date_range_for_semester(df_terms, past)
        if past_window is None or past not in history.index:
            continue
        start, end = past_window
        budget = float(history.loc[past, "Budget"])
        final = float(history.loc[past, "Spent"])
        if final <= 0:
            # A term with no spending at all says nothing about pace, and it is
            # indistinguishable from a term whose data was never loaded. Counting
            # it would pull "what is usual" toward zero.
            continue

        point = start + pd.Timedelta(days=round(elapsed / 100 * (end - start).days))
        by_point = _spent_between(expenses, start, point)
        rows.append(
            {
                "Semester": past,
                "Same season": _season(past) == current_season,
                "Spent by this point": by_point,
                "Of its budget": safe_percent(by_point, budget),
                "Ended the term at": safe_percent(final, budget),
            }
        )
        if len(rows) >= max_terms:
            break

    if not rows:
        return None

    return PastComparison(
        semester=semester,
        elapsed_percent=elapsed,
        as_of=as_of,
        current_spent=current_spent,
        current_percent=current_percent,
        table=pd.DataFrame(rows, columns=TABLE_COLUMNS),
    )


def compare_to_usual(comparison: PastComparison | None) -> dict | None:
    """
    How this term compares with what is usual for this point: the usual figure
    (the median of the pool), this term's, the pool's label, and a verdict --
    "more", "same" or "less" (within `SIMILAR_WITHIN` points counts as the same).

    Prefers terms in the same season, since Fall and Spring behave differently,
    and falls back to every past term when there is no earlier one in the season.
    """
    if comparison is None or comparison.current_percent is None or comparison.table.empty:
        return None

    same = comparison.table[comparison.table["Same season"]]
    pool, label = (
        (same, f"past {_season(comparison.semester)} terms")
        if not same.empty
        else (comparison.table, "past terms")
    )
    values = pool["Of its budget"].dropna()
    if values.empty:
        return None

    typical = float(values.median())
    current = float(comparison.current_percent)
    gap = current - typical
    verdict = "same" if abs(gap) <= SIMILAR_WITHIN else ("more" if gap > 0 else "less")
    return {"typical": typical, "current": current, "label": label, "verdict": verdict}


VERDICT_TAGS = {"more": "More than usual", "same": "About usual", "less": "Less than usual"}


def comparison_sentence(comparison: PastComparison | None) -> str | None:
    """One plain sentence: what is usual for this point, and how this term compares."""
    usual = compare_to_usual(comparison)
    if usual is None:
        return None
    relation = {
        "same": "about the same as usual",
        "more": "more than usual for this point",
        "less": "less than usual for this point",
    }[usual["verdict"]]
    return (
        f"At this point in {usual['label']} your committee had typically spent about "
        f"{usual['typical']:.0f}% of its budget. You are at {usual['current']:.0f}%, which is {relation}."
    )


def short_sentence(comparison: PastComparison | None) -> str | None:
    """The headline under the tag: two short facts, no judgement words."""
    usual = compare_to_usual(comparison)
    if usual is None:
        return None
    return (
        f"{usual['current']:.0f}% of your budget is used. By this point, "
        f"{usual['label']} had used about {usual['typical']:.0f}%."
    )


def chart_frame(comparison: PastComparison) -> pd.DataFrame:
    """
    Term | Used | Now for the bar chart: this term first, then past terms, most
    recent first. Terms with no budget (no percentage) are left out of the chart;
    they are still in the numbers table.
    """
    rows = [{"Term": comparison.semester, "Used": comparison.current_percent, "Now": True}]
    rows += [
        {"Term": row["Semester"], "Used": row["Of its budget"], "Now": False}
        for _, row in comparison.table.iterrows()
    ]
    frame = pd.DataFrame(rows, columns=["Term", "Used", "Now"])
    return frame[frame["Used"].notna()].reset_index(drop=True)


def display_frame(comparison: PastComparison) -> pd.DataFrame:
    """The table a VP sees: this term first, then past terms, most recent first."""
    def ended(value: object) -> str:
        """'79%', or a dash for this term (not over yet) and for an unknown figure."""
        return "—" if value is None or pd.isna(value) else f"{value:.0f}%"

    now = {
        "Term": f"{comparison.semester} (now)",
        "Spent by this point": comparison.current_spent,
        "Of its budget": comparison.current_percent,
        "Ended the term at": "—",
    }
    past = [
        {
            "Term": row["Semester"] + (" · same season" if row["Same season"] else ""),
            "Spent by this point": row["Spent by this point"],
            "Of its budget": row["Of its budget"],
            "Ended the term at": ended(row["Ended the term at"]),
        }
        for _, row in comparison.table.iterrows()
    ]
    return pd.DataFrame([now, *past])
