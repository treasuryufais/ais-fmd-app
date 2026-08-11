"""
Money handling.

FINDING F18. Postgres stores `amount` as numeric, which is correct, but every
load converted it to float64 and every calculation stayed there. The dues rule
did `amt not in DUES_AMOUNTS` -- a float set-membership test, exactly the
comparison that fails on representation error.

The approach here: parse to Decimal at the boundary, quantise to cents, and
compare money against money through `equals_any` rather than `in`. DataFrames
still carry float64 because pandas and Plotly want it, but every value that
enters one has been through `parse_amount` first, so it is already a clean
2-decimal quantity rather than whatever the source file happened to contain.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

CENTS = Decimal("0.01")

# Matches an optionally-signed, optionally-comma-grouped number, including a
# leading minus that appears *after* a currency symbol ("$-12.34") and the
# accounting convention of parenthesised negatives ("(12.34)").
_NUMBER_RE = re.compile(r"(?P<sign>[+-])?\s*(?P<digits>\d[\d,]*(?:\.\d+)?)")

_JUNK_CHARS = ("′", "’", "\xa0", "$", " ")


def parse_amount(value: object) -> Decimal | None:
    """
    Parse a money value from a spreadsheet cell.

    Returns None when the value cannot be parsed.

    FINDING F5. The original `numeric_amount` returned 0.0 on any failure, so a
    misread column became a stream of real $0.00 transactions instead of an
    obvious error. Returning None lets the caller count and report the failures
    instead of silently ingesting them.
    """
    if value is None:
        return None

    if isinstance(value, Decimal):
        return value.quantize(CENTS, rounding=ROUND_HALF_UP)

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)

    text = str(value)
    for junk in _JUNK_CHARS:
        text = text.replace(junk, "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower() in {"nan", "none", "null", "-", "--"}:
        return None

    negative_by_parens = text.startswith("(") and text.endswith(")")
    if negative_by_parens:
        text = text[1:-1].strip()

    match = _NUMBER_RE.search(text)
    if not match:
        return None

    digits = match.group("digits").replace(",", "")
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        return None

    if match.group("sign") == "-" or negative_by_parens:
        amount = -amount

    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


_STRICT_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def is_amount_like(value: object) -> bool:
    """
    Strict test: is this cell *entirely* a money value?

    `parse_amount` is deliberately permissive -- it searches for a number
    anywhere in the text, which is right when extracting from a cell that may
    carry stray currency symbols or whitespace.

    That permissiveness is catastrophic when used to *identify* which column
    holds the amount. A bank description like
    "ZELLE FROM ... ON 07/08 REF # 0A50..." contains digits, so `parse_amount`
    happily returns 7.00 and a description column scores as a perfect amount
    column. Layout detection must use this instead.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, Decimal)):
        return True
    if isinstance(value, float):
        return not (math.isnan(value) or math.isinf(value))

    text = str(value)
    for junk in _JUNK_CHARS:
        text = text.replace(junk, "")
    text = text.replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if not text:
        return False
    return bool(_STRICT_NUMBER.match(text))


def to_float(value: Decimal | None) -> float | None:
    """Convert to float at the pandas/Plotly boundary, after quantisation."""
    if value is None:
        return None
    return float(value)


def equals_any(value: object, candidates: Iterable[Decimal]) -> bool:
    """
    Exact money comparison against a set of known amounts.

    Replaces the float `in` test in the dues rule.
    """
    parsed = parse_amount(value)
    if parsed is None:
        return False
    return any(parsed == candidate for candidate in candidates)


def format_currency(value: object, *, signed: bool = False) -> str:
    """$1,234.56 -- or "—" when there is nothing to show."""
    parsed = parse_amount(value)
    if parsed is None:
        return "—"
    sign = ""
    if signed and parsed > 0:
        sign = "+"
    elif parsed < 0:
        sign = "-"
    return f"{sign}${abs(parsed):,.2f}"


def format_delta(value: object) -> str | None:
    """
    Preformatted metric delta.

    FINDING F14. `st.metric(delta=income_delta)` rendered a bare 1234.56 directly
    beneath a value formatted as $1,234.56.
    """
    parsed = parse_amount(value)
    if parsed is None or parsed == 0:
        return None
    return format_currency(parsed, signed=True)


def safe_percent(numerator: object, denominator: object) -> float | None:
    """
    Percentage, or None when the denominator is zero or missing.

    FINDING F12. `Spent / budget_amount * 100` produced inf for a committee with
    a zero budget, which then flowed into `max(100, series.max() * 1.1)` and
    destroyed the axis range for every other committee on the chart.
    """
    num = parse_amount(numerator)
    den = parse_amount(denominator)
    if num is None or den is None or den == 0:
        return None
    return float(num / den * 100)
