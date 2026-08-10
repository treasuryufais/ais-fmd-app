"""
Wells Fargo checking statement parser.

The standard Wells Fargo CSV export is headerless with five columns:

    date, amount, "*", (blank), description

The original trusted those positions blindly. This parser verifies the shape
before extracting: column 0 must parse as dates, column 1 as money, and the
description column must actually contain text. If the layout does not hold, the
file is rejected with an explanation rather than imported as noise.
"""

from __future__ import annotations

import warnings

import pandas as pd

from ...config.categories import ACCOUNT_WELLS_FARGO
from ..money import parse_amount, to_float
from .base import ParsedStatement, empty_frame, find_column, normalize_header

MIN_COLUMNS = 3
# Fraction of rows that must parse cleanly for the layout to be believed.
CONFIDENCE_THRESHOLD = 0.6


def parse(df_raw: pd.DataFrame, source_file: str = "") -> ParsedStatement:
    result = ParsedStatement(rows=empty_frame(), source_file=source_file)

    if df_raw is None or df_raw.empty:
        return result.fail("The file contains no rows.")
    if df_raw.shape[1] < MIN_COLUMNS:
        return result.fail(
            f"Expected at least {MIN_COLUMNS} columns, found {df_raw.shape[1]}. "
            f"This does not look like a Wells Fargo export."
        )

    layout = _detect_layout(df_raw)
    if layout is None:
        return result.fail(
            "Could not identify the date, amount and description columns. "
            "Checked both the headerless export layout and a labelled header row. "
            "Nothing was imported -- please confirm the file is a Wells Fargo "
            "checking export."
        )

    date_col, amount_col, details_col, note = layout
    if note:
        result.warnings.append(note)

    dates = pd.to_datetime(df_raw[date_col], errors="coerce")
    amounts = [parse_amount(value) for value in df_raw[amount_col]]
    details = df_raw[details_col].fillna("").astype(str).str.strip()

    records: list[dict] = []
    rejected = 0
    for position in range(len(df_raw)):
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

    parsed_ratio = len(records) / max(len(df_raw), 1)
    if parsed_ratio < CONFIDENCE_THRESHOLD:
        return result.fail(
            f"Only {parsed_ratio:.0%} of rows parsed cleanly, which suggests the "
            f"column layout has changed. Nothing was imported. Review the file "
            f"and re-upload."
        )

    result.rows = pd.DataFrame(records)
    return result


def _detect_layout(df_raw: pd.DataFrame) -> tuple[object, object, object, str] | None:
    """
    Identify (date_col, amount_col, details_col, note).

    Tries a labelled header first, then the standard headerless positions,
    then a content-based scan.
    """
    columns = {normalize_header(c): c for c in df_raw.columns}
    date_col = find_column(columns, "date")
    amount_col = find_column(columns, "amount")
    details_col = find_column(columns, "description", "details", "payee")
    if date_col is not None and amount_col is not None and details_col is not None:
        return date_col, amount_col, details_col, ""

    positional = _score_positional(df_raw)
    if positional is not None:
        return positional

    return _scan_by_content(df_raw)


def _score_positional(df_raw: pd.DataFrame) -> tuple[object, object, object, str] | None:
    """Verify the canonical headerless layout actually holds."""
    if df_raw.shape[1] < 5:
        return None
    date_col, amount_col = df_raw.columns[0], df_raw.columns[1]
    details_col = df_raw.columns[4]

    if _date_ratio(df_raw[date_col]) < CONFIDENCE_THRESHOLD:
        return None
    if _amount_ratio(df_raw[amount_col]) < CONFIDENCE_THRESHOLD:
        return None
    if _text_ratio(df_raw[details_col]) < CONFIDENCE_THRESHOLD:
        return None
    return date_col, amount_col, details_col, ""


def _scan_by_content(df_raw: pd.DataFrame) -> tuple[object, object, object, str] | None:
    """Last resort: pick the best-scoring column for each role."""
    date_scores = {c: _date_ratio(df_raw[c]) for c in df_raw.columns}
    amount_scores = {c: _amount_ratio(df_raw[c]) for c in df_raw.columns}
    text_scores = {c: _text_ratio(df_raw[c]) for c in df_raw.columns}

    date_col = max(date_scores, key=lambda c: date_scores[c])
    if date_scores[date_col] < CONFIDENCE_THRESHOLD:
        return None

    amount_candidates = {c: s for c, s in amount_scores.items() if c != date_col}
    if not amount_candidates:
        return None
    amount_col = max(amount_candidates, key=lambda c: amount_candidates[c])
    if amount_candidates[amount_col] < CONFIDENCE_THRESHOLD:
        return None

    text_candidates = {c: s for c, s in text_scores.items() if c not in (date_col, amount_col)}
    if not text_candidates:
        return None
    details_col = max(text_candidates, key=lambda c: text_candidates[c])
    if text_candidates[details_col] < CONFIDENCE_THRESHOLD:
        return None

    return (
        date_col,
        amount_col,
        details_col,
        "Column positions differed from the standard export; columns were "
        "identified by content. Please spot-check the preview before confirming.",
    )


def _date_ratio(series: pd.Series) -> float:
    if not len(series):
        return 0.0
    # Layout probing deliberately feeds non-date columns through this, so the
    # "could not infer format" notice is expected noise rather than a signal.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        parsed = pd.to_datetime(series, errors="coerce")
    return float(parsed.notna().mean())


def _amount_ratio(series: pd.Series) -> float:
    if not len(series):
        return 0.0
    parsed = [parse_amount(value) is not None for value in series]
    return sum(parsed) / len(parsed)


def _text_ratio(series: pd.Series) -> float:
    if not len(series):
        return 0.0
    values = series.fillna("").astype(str).str.strip()
    # A description column has words, not just digits.
    has_letters = values.str.contains(r"[A-Za-z]", regex=True, na=False)
    return float(has_letters.mean())
