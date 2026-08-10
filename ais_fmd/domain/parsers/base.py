"""
Shared statement-parsing types.

FINDING F5. The original read Wells Fargo columns by index -- iloc[:,0],
iloc[:,1], iloc[:,4] with a fallback to the last column -- and validated only
that there were at least three columns. Combined with `numeric_amount` returning
0.0 on any parse failure, a bank changing its export layout would silently
ingest a stream of real $0.00 transactions.

Parsers here validate shape before extracting, and every row that cannot be
parsed is counted and reported rather than coerced into a plausible-looking zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

REQUIRED_COLUMNS = ("transaction_date", "amount", "details", "account")


@dataclass
class ParsedStatement:
    """The outcome of parsing an uploaded statement."""

    rows: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REQUIRED_COLUMNS))
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected_rows: int = 0
    source_file: str = ""

    @property
    def ok(self) -> bool:
        """A statement is usable when it produced rows and raised no hard error."""
        return not self.errors and not self.rows.empty

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def fail(self, message: str) -> "ParsedStatement":
        self.errors.append(message)
        return self


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in REQUIRED_COLUMNS})


def normalize_header(value: object) -> str:
    """Lowercase, de-nbsp, collapse whitespace -- for header matching."""
    return str(value).replace("\xa0", " ").strip().lower()


def find_column(columns: dict[str, str], *needles: str, require_all: bool = False) -> str | None:
    """
    Find a source column by keyword.

    `columns` maps normalized header -> original header.
    With require_all, every needle must appear in the header.
    """
    for normalized, original in columns.items():
        if require_all:
            if all(needle in normalized for needle in needles):
                return original
        else:
            if any(needle in normalized for needle in needles):
                return original
    return None
