"""
Files a VP can download.

Everything a VP downloads is something they can already see on screen, so an
export is no more sensitive than the page it comes from. What needs care is not
what is in the file but what the file can *do* when opened.

A bank description is free text, and some of it comes from outside the
organisation: the memo on a payment someone sent. A cell that starts with `=`,
`+`, `-` or `@` is read by Excel and Google Sheets as a formula, so a memo such as
`=HYPERLINK(...)` would run when a VP opens their export. `csv_safe` defuses
that by putting a quote in front, which spreadsheets show as plain text.
"""

from __future__ import annotations

import re

import pandas as pd

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: object) -> object:
    """Text that a spreadsheet would run as a formula, made inert. Anything else unchanged."""
    if isinstance(value, str) and value.startswith(_FORMULA_STARTS):
        return "'" + value
    return value


def transactions_csv(table: pd.DataFrame) -> bytes:
    """
    The table as CSV bytes, ready for a download button.

    Numbers stay numbers -- a negative amount starts with `-` but is not text, so
    it is left alone -- while text columns are passed through `csv_safe`. UTF-8
    with a byte-order mark, because without it Excel misreads accented characters.
    """
    safe = table.copy()
    # Every column, not just `object` ones: on newer pandas text has its own
    # string dtype, and a dtype check would quietly skip exactly the columns that
    # need it. `csv_safe` leaves anything that is not text untouched.
    for column in safe.columns:
        safe[column] = safe[column].map(csv_safe)
    return safe.to_csv(index=False).encode("utf-8-sig")


def slug(text: str) -> str:
    """'Professional Development' -> 'professional-development', for file names."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return cleaned or "committee"
