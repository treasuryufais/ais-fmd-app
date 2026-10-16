"""
The numbers on a VP's committee page.

A VP's committee can own several budget lines (Membership owns Membership and
Passport), so the page adds them together and shows how far through the term
they are. Everything here takes plain DataFrames and numbers and returns plain
values -- no Streamlit import, so it can be tested without a browser.
"""

from __future__ import annotations

import pandas as pd

from . import budgets as budget_domain
from .money import safe_percent
from .terms import date_range_for_semester

# Below this share of the term, spending so far says little about the end of the
# term (one early purchase would be multiplied fifty-fold), so no projection is
# offered rather than a misleading one.
MIN_ELAPSED_FOR_PROJECTION = 10.0


def rollup(summary: pd.DataFrame, names: list[str]) -> dict:
    """
    Add together the budget lines called `names` from a `budget_vs_actual` frame.

    `percent` is None when the combined budget is zero (nothing to divide by),
    the same convention `safe_percent` uses everywhere else.
    """
    rows = summary[summary["Committee_Name"].isin(names)]
    budget = float(rows["Budget"].sum())
    spent = float(rows["Spent"].sum())
    return {
        "budget": budget,
        "spent": spent,
        "remaining": budget - spent,
        "percent": safe_percent(spent, budget),
    }


def status_for(percent: float | None, spent: float) -> str:
    """
    on track / approaching / over, for a combined figure.

    The thresholds deliberately match `budgets._status_for_row`, which colours
    the org-wide dashboard, so one committee never reads "on track" on one page
    and "approaching" on another. A test compares the two across a range.
    """
    if percent is None or pd.isna(percent):
        return "unbudgeted" if spent > 0 else "no budget"
    if percent > 100:
        return "over"
    if percent >= 85:
        return "approaching"
    return "on track"


def term_elapsed_percent(
    df_terms: pd.DataFrame, semester: str, *, as_of: pd.Timestamp | None = None
) -> float | None:
    """How far through the term `as_of` (default: today) is, 0-100. None if unknown."""
    window = date_range_for_semester(df_terms, semester)
    if window is None:
        return None
    start, end = window
    total_days = (end - start).days
    if total_days <= 0:
        return None
    now = as_of if as_of is not None else pd.Timestamp.today().normalize()
    elapsed_days = min(max((now - start).days, 0), total_days)
    return elapsed_days / total_days * 100


def projected_spend(spent: float, elapsed_percent: float | None) -> float | None:
    """
    Where spending lands at the end of the term if the pace so far continues.

    A straight line: spent so far divided by the share of the term used. None
    when it is too early in the term to say (see MIN_ELAPSED_FOR_PROJECTION).
    """
    if elapsed_percent is None or elapsed_percent < MIN_ELAPSED_FOR_PROJECTION:
        return None
    return spent / (elapsed_percent / 100)


def historical(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    names: list[str],
) -> pd.DataFrame:
    """
    Budget and spend per semester for several budget lines added together.

    `historical_budget_vs_actual` takes one line at a time; a committee that owns
    two lines needs them summed per semester. Columns match that function's, so
    the same chart draws it.
    """
    columns = ["Semester", "Budget", "Spent", "% Spent"]
    frames = [
        budget_domain.historical_budget_vs_actual(df_transactions, df_budgets, df_terms, name)
        for name in names
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=columns)

    out = (
        pd.concat(frames)
        .groupby("Semester", sort=False, as_index=False)[["Budget", "Spent"]]
        .sum()
    )
    out["% Spent"] = [
        safe_percent(spent, budget) for spent, budget in zip(out["Spent"], out["Budget"])
    ]
    return out[columns]


def budget_flow(
    expense_split: pd.DataFrame, budget: float, *, itemised: int = 5
) -> tuple[list[str], list[float], list[str]]:
    """
    Steps for a waterfall that runs Budget -> biggest spending -> what Remains.

    `expense_split` is `budgets.categorize_flow`'s Category/Amount frame, largest
    first. Everything past the `itemised` biggest purposes is carried in one
    "Other" step, so the steps always add up to the real remaining balance rather
    than to whatever the itemised bars happen to reach -- the same fix the
    org-wide waterfall needed. The last step is a Plotly "total", drawn wherever
    the bars before it land.
    """
    top = expense_split.head(itemised)
    other = float(expense_split["Amount"].sum()) - float(top["Amount"].sum())

    labels = ["Budget"] + top["Category"].tolist()
    values = [float(budget)] + [-float(amount) for amount in top["Amount"]]
    if round(other, 2) != 0:
        labels.append("Other")
        values.append(-other)
    labels.append("Remaining")
    values.append(0.0)

    measures = ["relative"] * (len(labels) - 1) + ["total"]
    return labels, values, measures


def _long_date(value: pd.Timestamp) -> str:
    """'Oct 22, 2026' -- without the platform-specific `%-d` that Windows rejects."""
    return f"{value:%b} {value.day}, {value.year}"


def data_freshness(
    df_transactions: pd.DataFrame, df_uploaded_files: pd.DataFrame
) -> dict[str, pd.Timestamp | None]:
    """
    How current the ledger is: the latest charge on record, and when statements
    were last uploaded.

    Deliberately about the whole ledger rather than one committee. A VP whose
    last charge was a month ago would otherwise read "no recent activity" as "the
    data is a month old"; the ledger-wide date says how far the statements reach,
    and a single date discloses nothing about any other committee.
    """
    through = None
    if df_transactions is not None and not df_transactions.empty:
        latest = pd.to_datetime(df_transactions["transaction_date"], errors="coerce").max()
        through = None if pd.isna(latest) else latest

    uploaded = None
    if (
        df_uploaded_files is not None
        and not df_uploaded_files.empty
        and "uploaded_at" in df_uploaded_files.columns
    ):
        latest = pd.to_datetime(df_uploaded_files["uploaded_at"], errors="coerce").max()
        uploaded = None if pd.isna(latest) else latest

    return {"through": through, "uploaded": uploaded}


def freshness_text(df_transactions: pd.DataFrame, df_uploaded_files: pd.DataFrame) -> str:
    """One plain sentence a VP can read to know whether the numbers are current."""
    fresh = data_freshness(df_transactions, df_uploaded_files)
    if fresh["through"] is None:
        return "No statements have been uploaded yet, so there are no figures to show."
    text = f"Includes charges through {_long_date(fresh['through'])}."
    if fresh["uploaded"] is not None:
        text += f" Statements last uploaded {_long_date(fresh['uploaded'])}."
    return text


def cumulative_spend(
    df_transactions: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    through: pd.Timestamp | None,
) -> pd.DataFrame:
    """
    Running total of money spent, one point per day from `start` to the last day
    the data reaches. Columns: date, spent.

    Only expenses (negative amounts) count, matching "Spent" everywhere else, and
    only rows dated inside [start, end]. The line stops at `through` rather than
    running flat to the end of the term: a flat tail would read as "spent nothing
    since", when the truth is "we don't know yet".
    """
    columns = ["date", "spent"]
    if through is None:
        return pd.DataFrame(columns=columns)

    last = min(pd.Timestamp(through).normalize(), pd.Timestamp(end).normalize())
    first = pd.Timestamp(start).normalize()
    if last < first:
        return pd.DataFrame(columns=columns)

    days = pd.date_range(first, last, freq="D")
    if df_transactions is None or df_transactions.empty:
        return pd.DataFrame({"date": days, "spent": 0.0})

    dates = pd.to_datetime(df_transactions["transaction_date"], errors="coerce").dt.normalize()
    expenses = df_transactions[(df_transactions["amount"] < 0) & dates.between(first, last)]
    daily = (
        expenses["amount"].abs().groupby(dates[expenses.index]).sum().reindex(days, fill_value=0.0)
    )
    return pd.DataFrame({"date": days, "spent": daily.cumsum().to_numpy()})


def pace_sentence(position: dict, elapsed: float | None, semester: str) -> str | None:
    """
    "62% of the budget is spent and 38% of Fall 2026 has passed. At this pace..."

    One place for the wording, shared by the page and the printable report so the
    two can never disagree. None when there is nothing meaningful to say (no term
    dates, or no budget to take a percentage of).
    """
    from .money import format_currency

    percent = position["percent"]
    if elapsed is None or percent is None:
        return None

    text = f"{percent:.0f}% of the budget is spent and {elapsed:.0f}% of {semester} has passed."
    projected = projected_spend(position["spent"], elapsed)
    if projected is not None:
        gap = position["budget"] - projected
        text += (
            f" At this pace the term ends with {format_currency(projected)} spent, "
            + (
                f"{format_currency(gap)} under budget."
                if gap >= 0
                else f"{format_currency(-gap)} over budget."
            )
        )
    return text
