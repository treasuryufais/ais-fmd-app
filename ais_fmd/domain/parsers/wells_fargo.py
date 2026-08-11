"""
Wells Fargo checking statement parser.

Real Wells Fargo exports come in at least two shapes:

  A. Labelled, five columns:
        DATE, DESCRIPTION, AMOUNT, CHECK #, STATUS
  B. Headerless, five columns:
        date, amount, "*", (blank), description

Note that the amount and description columns are in *opposite* positions in the
two layouts. The original code assumed B unconditionally and read column 1 as
the amount -- on a shape-A file that is the description, and every row imported
as $0.00.

The first version of this parser also failed on a shape-A file, and failed
*silently*: it reported success while producing $4.4 trillion in credits and a
description column reading "Posted" on every row. Three things went wrong, and
each is now defended against explicitly:

  1. Layout scoring used the permissive `parse_amount`, which finds a number
     anywhere in a string -- so a description containing "ON 07/08" scored as a
     perfect amount column. Detection now uses `is_amount_like`, which requires
     the whole cell to be a number.

  2. The header row was never detected, so labels could not be used even when
     present. A header row is now promoted, which makes shape A unambiguous.

  3. Nothing checked whether the *result* was plausible. A description column is
     nearly all-distinct; a status column has two values. Detection now scores
     distinctness, so "Posted" repeated 890 times can never win the description
     slot.
"""

from __future__ import annotations

import warnings

import pandas as pd

from ...config.categories import ACCOUNT_WELLS_FARGO
from ..money import is_amount_like, parse_amount, to_float
from .base import ParsedStatement, empty_frame, find_column, normalize_header

MIN_COLUMNS = 3
# Fraction of rows that must parse cleanly for a layout to be believed.
CONFIDENCE_THRESHOLD = 0.6
# A description column is nearly all-distinct. A status flag is not.
MIN_DETAILS_SCORE = 0.25


def parse(df_raw: pd.DataFrame, source_file: str = "") -> ParsedStatement:
    result = ParsedStatement(rows=empty_frame(), source_file=source_file)

    if df_raw is None or df_raw.empty:
        return result.fail("The file contains no rows.")
    if df_raw.shape[1] < MIN_COLUMNS:
        return result.fail(
            f"Expected at least {MIN_COLUMNS} columns, found {df_raw.shape[1]}. "
            f"This does not look like a Wells Fargo export."
        )

    frame, promoted = _promote_header(df_raw)
    if promoted:
        result.warnings.append(
            "Detected a header row; columns were matched by label rather than position."
        )

    layout = _detect_layout(frame)
    if layout is None:
        return result.fail(
            "Could not confidently identify the date, amount and description "
            "columns. Checked labelled headers, the standard headerless layout, "
            "and a content scan. Nothing was imported. "
            f"Columns seen: {[str(c) for c in frame.columns]}"
        )

    date_col, amount_col, details_col, note = layout
    if note:
        result.warnings.append(note)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        dates = pd.to_datetime(frame[date_col], errors="coerce")

    amounts = [parse_amount(value) for value in frame[amount_col]]
    details = frame[details_col].fillna("").astype(str).str.strip()

    records: list[dict] = []
    rejected = 0
    for position in range(len(frame)):
        date_value = dates.iloc[position]
        amount_value = amounts[position]
        detail_text = details.iloc[position]

        if pd.isna(date_value) and amount_value is None and not detail_text:
            continue
        if pd.isna(date_value) or amount_value is None or not detail_text:
            rejected += 1
            continue

        records.append(
            {
                "transaction_date": date_value.strftime("%Y-%m-%d"),
                "amount": to_float(amount_value),
                "details": detail_text,
                "account": ACCOUNT_WELLS_FARGO,
            }
        )

    result.rejected_rows = rejected
    if rejected:
        result.warnings.append(
            f"{rejected} row(s) had an unreadable date, amount or description and "
            f"were skipped rather than imported as $0.00."
        )
    if not records:
        return result.fail("No usable transactions found in the file.")

    parsed_ratio = len(records) / max(len(frame), 1)
    if parsed_ratio < CONFIDENCE_THRESHOLD:
        return result.fail(
            f"Only {parsed_ratio:.0%} of rows parsed cleanly, which suggests the "
            f"column layout has changed. Nothing was imported."
        )

    rows = pd.DataFrame(records)

    problem = _implausible(rows)
    if problem:
        return result.fail(
            f"{problem} This almost always means the wrong columns were read, so "
            f"nothing was imported. Please check the file and report the layout."
        )

    result.rows = rows
    return result


# --- Header handling ---------------------------------------------------------

def _promote_header(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """
    If row 0 holds column labels rather than data, promote it.

    Files are read with header=None so that a genuinely headerless export does
    not lose its first transaction. That means a labelled file arrives with its
    header sitting in row 0, and it has to be recognised here.
    """
    if df_raw.empty:
        return df_raw, False

    first = df_raw.iloc[0].tolist()

    # A header row has no dates and no numbers, and at least two wordy cells.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        any_dates = pd.to_datetime(pd.Series(first), errors="coerce").notna().any()
    any_amounts = any(is_amount_like(value) for value in first)
    wordy = sum(1 for value in first if any(ch.isalpha() for ch in str(value)))

    if any_dates or any_amounts or wordy < 2:
        return df_raw, False

    promoted = df_raw.iloc[1:].copy()
    promoted.columns = [str(value).strip() for value in first]
    return promoted.reset_index(drop=True), True


# --- Layout detection --------------------------------------------------------

def _detect_layout(frame: pd.DataFrame) -> tuple[object, object, object, str] | None:
    """Identify (date_col, amount_col, details_col, note)."""
    columns = {normalize_header(c): c for c in frame.columns}
    date_col = find_column(columns, "date")
    amount_col = find_column(columns, "amount")
    details_col = find_column(columns, "description", "details", "payee", "memo")

    if date_col is not None and amount_col is not None and details_col is not None:
        # Labels found -- but still verify they hold what they claim, so a file
        # with misleading headers cannot slip through.
        if (
            _date_ratio(frame[date_col]) >= CONFIDENCE_THRESHOLD
            and _amount_ratio(frame[amount_col]) >= CONFIDENCE_THRESHOLD
            and _details_score(frame[details_col]) >= MIN_DETAILS_SCORE
        ):
            return date_col, amount_col, details_col, ""

    positional = _score_positional(frame)
    if positional is not None:
        return positional

    return _scan_by_content(frame)


def _score_positional(frame: pd.DataFrame) -> tuple[object, object, object, str] | None:
    """Verify the canonical headerless layout: date, amount, *, blank, description."""
    if frame.shape[1] < 5:
        return None
    date_col, amount_col, details_col = frame.columns[0], frame.columns[1], frame.columns[4]

    if _date_ratio(frame[date_col]) < CONFIDENCE_THRESHOLD:
        return None
    if _amount_ratio(frame[amount_col]) < CONFIDENCE_THRESHOLD:
        return None
    if _details_score(frame[details_col]) < MIN_DETAILS_SCORE:
        return None
    return date_col, amount_col, details_col, ""


def _scan_by_content(frame: pd.DataFrame) -> tuple[object, object, object, str] | None:
    """Last resort: pick the best-scoring column for each role."""
    date_scores = {c: _date_ratio(frame[c]) for c in frame.columns}
    amount_scores = {c: _amount_ratio(frame[c]) for c in frame.columns}
    details_scores = {c: _details_score(frame[c]) for c in frame.columns}

    date_col = max(date_scores, key=lambda c: date_scores[c])
    if date_scores[date_col] < CONFIDENCE_THRESHOLD:
        return None

    amount_candidates = {c: s for c, s in amount_scores.items() if c != date_col}
    if not amount_candidates:
        return None
    amount_col = max(amount_candidates, key=lambda c: amount_candidates[c])
    if amount_candidates[amount_col] < CONFIDENCE_THRESHOLD:
        return None

    details_candidates = {
        c: s for c, s in details_scores.items() if c not in (date_col, amount_col)
    }
    if not details_candidates:
        return None
    details_col = max(details_candidates, key=lambda c: details_candidates[c])
    if details_candidates[details_col] < MIN_DETAILS_SCORE:
        return None

    return (
        date_col,
        amount_col,
        details_col,
        "Column positions differed from the standard export; columns were "
        "identified by content. Please spot-check the preview before confirming.",
    )


# --- Column scoring ----------------------------------------------------------

def _date_ratio(series: pd.Series) -> float:
    if not len(series):
        return 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        parsed = pd.to_datetime(series, errors="coerce")
    return float(parsed.notna().mean())


def _amount_ratio(series: pd.Series) -> float:
    """Strict: the whole cell must be a number (see money.is_amount_like)."""
    if not len(series):
        return 0.0
    return sum(is_amount_like(value) for value in series) / len(series)


def _details_score(series: pd.Series) -> float:
    """
    How much this column looks like transaction descriptions.

    Two signals multiplied: does it contain words, and are the values distinct?
    A STATUS column is all words but has two distinct values across hundreds of
    rows, which is what let "Posted" win the description slot before.
    """
    if not len(series):
        return 0.0
    values = series.fillna("").astype(str).str.strip()
    letters = float(values.str.contains(r"[A-Za-z]", regex=True, na=False).mean())
    distinct = values.nunique() / len(values)
    # Distinctness saturates at 25% -- descriptions repeat sometimes, statuses
    # repeat always.
    return letters * min(1.0, distinct * 4)


# --- Result plausibility -----------------------------------------------------

def _implausible(rows: pd.DataFrame) -> str | None:
    """
    A final guard on the parsed output, independent of how columns were chosen.

    Detection can be confidently wrong; this catches results that no real
    statement would produce.
    """
    if len(rows) < 10:
        return None

    distinct_details = rows["details"].nunique()
    if distinct_details <= 2:
        return (
            f"All {len(rows)} rows share only {distinct_details} distinct "
            f"description(s), so the description column looks like a status flag."
        )

    amounts = pd.to_numeric(rows["amount"], errors="coerce")
    if amounts.abs().max() > 1e9:
        return (
            f"The largest amount read was ${amounts.abs().max():,.0f}, which is "
            f"not a plausible transaction."
        )

    return None
