"""
Academic term / semester mapping.

FINDING (performance). `get_semester()` was defined identically in three files
and scanned the entire terms table once per transaction row, invoked via
`.apply()` across the whole table on every Streamlit re-run. Since Streamlit
re-runs the whole script on every widget interaction, this was the dominant
compute cost in the app.

One vectorised implementation, using an IntervalIndex, replaces all three.
"""

from __future__ import annotations

import re

import pandas as pd

_SEASONS = ("fall", "spring", "summer", "winter")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def prepare_terms(df_terms: pd.DataFrame) -> pd.DataFrame:
    """Parse dates and sort chronologically. Idempotent."""
    if df_terms.empty:
        return df_terms.assign(
            start_date=pd.Series(dtype="datetime64[ns]"),
            end_date=pd.Series(dtype="datetime64[ns]"),
        )
    out = df_terms.copy()
    out["start_date"] = pd.to_datetime(out["start_date"], errors="coerce")
    out["end_date"] = pd.to_datetime(out["end_date"], errors="coerce")
    return out.dropna(subset=["start_date", "end_date"]).sort_values("start_date")


def semester_index(df_terms: pd.DataFrame) -> pd.Series:
    """
    An IntervalIndex-backed lookup from date -> semester name.

    Overlapping terms would make the interval index ambiguous, so overlaps are
    resolved by keeping the earliest-starting term, matching the old behaviour
    of `semesters.iloc[0]`.
    """
    terms = prepare_terms(df_terms)
    if terms.empty:
        return pd.Series(dtype="object")

    intervals: list[pd.Interval] = []
    names: list[str] = []
    last_end: pd.Timestamp | None = None
    for _, row in terms.iterrows():
        start, end = row["start_date"], row["end_date"]
        if last_end is not None and start <= last_end:
            start = last_end + pd.Timedelta(days=1)
        if start > end:
            continue
        intervals.append(pd.Interval(start, end, closed="both"))
        names.append(row["Semester"])
        last_end = end

    if not intervals:
        return pd.Series(dtype="object")
    return pd.Series(names, index=pd.IntervalIndex(intervals))


def attach_semester(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    *,
    column: str = "Semester",
    date_column: str = "transaction_date",
) -> pd.DataFrame:
    """Add a semester column to a transactions frame in one vectorised pass."""
    out = df_transactions.copy()
    if out.empty:
        out[column] = pd.Series(dtype="object")
        return out

    lookup = semester_index(df_terms)
    dates = pd.to_datetime(out[date_column], errors="coerce")
    if lookup.empty:
        out[column] = None
        return out

    positions = lookup.index.get_indexer(dates)
    values = [lookup.iloc[pos] if pos != -1 else None for pos in positions]
    out[column] = values
    return out


def ordered_semesters(df_terms: pd.DataFrame) -> list[str]:
    """Semester names, chronologically. The canonical ordering for filters."""
    terms = prepare_terms(df_terms)
    if terms.empty:
        return []
    return terms["Semester"].dropna().tolist()


def previous_semester(df_terms: pd.DataFrame, current: str) -> str | None:
    order = ordered_semesters(df_terms)
    if current not in order:
        return None
    index = order.index(current)
    return order[index - 1] if index > 0 else None


def term_id_for_semester(df_terms: pd.DataFrame, semester: str) -> str | None:
    match = df_terms[df_terms["Semester"] == semester]
    return None if match.empty else str(match.iloc[0]["TermID"])


def date_range_for_semester(
    df_terms: pd.DataFrame, semester: str
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    terms = prepare_terms(df_terms)
    match = terms[terms["Semester"] == semester]
    if match.empty:
        return None
    row = match.iloc[0]
    return row["start_date"], row["end_date"]


def validate_semester_name(name: str) -> tuple[bool, str]:
    """(is_valid, message) for the Add Term form."""
    text = (name or "").strip()
    if not text:
        return False, "Semester name is required."
    if not any(text.lower().startswith(season) for season in _SEASONS):
        return False, "Start with Fall, Spring, Summer, or Winter."
    if not _YEAR_RE.search(text):
        return False, "Include a 4-digit year."
    return True, "Valid format."
