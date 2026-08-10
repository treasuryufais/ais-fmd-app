"""
Venmo statement parser.

Venmo exports carry a preamble, a header row, transaction rows, and a footer
("Account Statement - (@UFAIS)"). The details string is assembled from the
transaction ID, note, sender and recipient -- the ID is what makes Venmo rows
naturally unique, which is why the original dedupe key accidentally worked for
Venmo and failed for Wells Fargo.
"""

from __future__ import annotations

import pandas as pd

from ...config.categories import ACCOUNT_VENMO
from ..money import parse_amount, to_float
from .base import ParsedStatement, empty_frame, find_column, normalize_header

FOOTER_MARKERS = ("account statement", "beginning balance", "ending balance")


def parse(df_raw: pd.DataFrame, source_file: str = "") -> ParsedStatement:
    result = ParsedStatement(rows=empty_frame(), source_file=source_file)

    if df_raw is None or df_raw.empty:
        return result.fail("The file contains no rows.")

    # Venmo pads the sheet with unnamed columns and a preamble; find the real
    # header by locating the row that contains a recognisable set of labels.
    frame = _locate_header(df_raw)
    if frame is None:
        return result.fail(
            "Could not find a Venmo header row. Expected columns including "
            "'Datetime' (or 'Date') and 'Amount (total)'."
        )

    columns = {normalize_header(c): c for c in frame.columns}
    date_col = find_column(columns, "datetime", "date")
    amount_col = find_column(columns, "amount", "total", require_all=True) or find_column(
        columns, "amount"
    )

    missing = [
        label
        for label, column in (("date", date_col), ("amount", amount_col))
        if column is None
    ]
    if missing:
        return result.fail(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Found: {', '.join(str(c) for c in frame.columns)}"
        )

    note_col = find_column(columns, "note")
    id_col = find_column(columns, "id")
    from_col = columns.get("from")
    to_col = columns.get("to")

    details = _build_details(frame, [id_col, note_col, from_col, to_col])
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    amounts = [parse_amount(value) for value in frame[amount_col]]

    records: list[dict] = []
    rejected = 0
    for position in range(len(frame)):
        detail_text = details.iloc[position]
        if any(marker in detail_text.lower() for marker in FOOTER_MARKERS):
            continue

        date_value = dates.iloc[position]
        amount_value = amounts[position]

        if pd.isna(date_value) and amount_value is None and not detail_text.strip():
            continue  # genuinely blank padding row
        if pd.isna(date_value) or amount_value is None:
            rejected += 1
            continue

        records.append(
            {
                "transaction_date": date_value.strftime("%Y-%m-%d"),
                "amount": to_float(amount_value),
                "details": detail_text.strip(),
                "account": ACCOUNT_VENMO,
            }
        )

    result.rejected_rows = rejected
    if rejected:
        result.warnings.append(
            f"{rejected} row(s) had an unreadable date or amount and were skipped. "
            f"They were not imported as $0.00."
        )
    if not records:
        return result.fail("No usable transactions found in the file.")

    result.rows = pd.DataFrame(records)
    return result


def _locate_header(df_raw: pd.DataFrame) -> pd.DataFrame | None:
    """Return a frame whose columns are the real Venmo headers."""
    columns = {normalize_header(c) for c in df_raw.columns}
    if any("amount" in c for c in columns) and any(
        "date" in c or "datetime" in c for c in columns
    ):
        return df_raw

    # Header is buried in the body: scan the first rows for it.
    for position in range(min(len(df_raw), 10)):
        row = [normalize_header(v) for v in df_raw.iloc[position].tolist()]
        if any("amount" in v for v in row) and any("date" in v for v in row):
            reframed = df_raw.iloc[position + 1 :].copy()
            reframed.columns = [str(v) for v in df_raw.iloc[position].tolist()]
            return reframed.reset_index(drop=True)
    return None


def _build_details(frame: pd.DataFrame, columns: list[str | None]) -> pd.Series:
    """Join the identifying columns with ' | ', skipping ones the file lacks."""
    present = [c for c in columns if c is not None and c in frame.columns]
    if not present:
        return pd.Series([""] * len(frame), index=frame.index)

    parts = [frame[column].fillna("").astype(str).str.strip() for column in present]
    combined = parts[0]
    for part in parts[1:]:
        combined = combined + " | " + part
    return combined
