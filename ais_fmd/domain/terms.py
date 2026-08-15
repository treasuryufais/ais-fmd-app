"""
Academic term / semester mapping.

FINDING (performance). `get_semester()` was defined identically in three files
and scanned the entire terms table once per transaction row, invoked via
`.apply()` across the whole table on every Streamlit re-run. Since Streamlit
re-runs the whole script on every widget interaction, this was the dominant
compute cost in the app.

One vectorised implementation, using an IntervalIndex, replaces all three.

FINDING (performance, second pass). The vectorised version above was still
O(rows) in *Python*: `get_indexer` returned integer positions, and those were
then turned back into names with a per-row `lookup.iloc[pos]` list
comprehension. `.iloc` on a Series is a slow scalar path, so a 892-row table
spent ~6 ms there per call -- and callers invoke this once per semester inside
loops. Taking through the backing numpy array instead is ~500x faster.

`semester_index` rebuilds an IntervalIndex from the terms table on every call
(~2.7 ms). The terms table is tiny and changes rarely, so the built index is
memoized on the *values* of the terms table -- not on object identity, which
would go stale the moment a caller passed a fresh copy of the same data.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

_SEASONS = ("fall", "spring", "summer", "winter")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Memo for `semester_index`, keyed on the terms table's contents. Bounded
# because a long-lived server would otherwise accumulate one entry per edit.
_INDEX_CACHE: dict[tuple, pd.Series] = {}
_INDEX_CACHE_LIMIT = 32


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


def _terms_fingerprint(df_terms: pd.DataFrame) -> tuple | None:
    """
    A hashable summary of the terms table, or None if it cannot be summarised.

    Only the three columns the index is built from participate, so unrelated
    edits (renaming a term's owner, say) do not needlessly evict the entry.
    Returning None disables caching rather than risking a wrong hit.
    """
    required = ("Semester", "start_date", "end_date")
    if df_terms is None or not all(column in df_terms.columns for column in required):
        return None
    try:
        return tuple(
            (str(semester), str(start), str(end))
            for semester, start, end in zip(
                df_terms["Semester"], df_terms["start_date"], df_terms["end_date"]
            )
        )
    except TypeError:
        return None


def semester_index(df_terms: pd.DataFrame) -> pd.Series:
    """
    An IntervalIndex-backed lookup from date -> semester name.

    Overlapping terms would make the interval index ambiguous, so overlaps are
    resolved by keeping the earliest-starting term, matching the old behaviour
    of `semesters.iloc[0]`.
    """
    fingerprint = _terms_fingerprint(df_terms)
    if fingerprint is not None:
        cached = _INDEX_CACHE.get(fingerprint)
        if cached is not None:
            return cached

    terms = prepare_terms(df_terms)

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
        built = pd.Series(dtype="object")
    else:
        built = pd.Series(names, index=pd.IntervalIndex(intervals))

    if fingerprint is not None:
        if len(_INDEX_CACHE) >= _INDEX_CACHE_LIMIT:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[fingerprint] = built
    return built


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
    # Take through the backing array, not `.iloc` per row: `get_indexer` marks
    # misses with -1, which would silently wrap around to the last term, so the
    # misses are masked back to None afterwards.
    names = lookup.to_numpy()
    values = np.where(positions >= 0, names.take(positions), None)
    out[column] = values
    return out


def ordered_semesters(df_terms: pd.DataFrame) -> list[str]:
    """Semester names, chronologically. The canonical ordering for filters."""
    terms = prepare_terms(df_terms)
    if terms.empty:
        return []
    return terms["Semester"].dropna().tolist()


def default_semester(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
) -> str | None:
    """
    The semester a page should open on: the most recent one with data in it.

    Defaulting to the last *defined* term lands the user on an empty page
    whenever terms are defined ahead of time -- which is normal, since terms are
    usually set up for the year in advance. The most recent term that actually
    contains transactions is almost always what someone wants to see.
    """
    order = ordered_semesters(df_terms)
    if not order:
        return None
    if df_transactions is None or df_transactions.empty:
        return order[-1]

    tagged = attach_semester(df_transactions, df_terms)
    populated = set(tagged["Semester"].dropna().unique())
    for semester in reversed(order):
        if semester in populated:
            return semester
    return order[-1]


def default_semester_index(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
) -> int:
    """Position of `default_semester` within `ordered_semesters`, for selectbox index."""
    order = ordered_semesters(df_terms)
    if not order:
        return 0
    target = default_semester(df_transactions, df_terms)
    return order.index(target) if target in order else len(order) - 1


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
