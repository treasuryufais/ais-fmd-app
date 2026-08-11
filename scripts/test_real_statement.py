"""
Test the parser + categorizer against a real bank statement.

Personal names are masked in all output -- this prints diagnostics, not a
transaction listing.

Usage:
    python scripts/test_real_statement.py "<path to csv>"
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.domain.categorize.merchants import merchant_key
from ais_fmd.domain.categorize.pipeline import categorize_frame
from ais_fmd.domain.money import parse_amount
from ais_fmd.domain.parsers import wells_fargo

# Mask "ZELLE FROM FIRST LAST" style personal names.
_NAME = re.compile(r"(ZELLE (?:FROM|TO)\s+)([A-Z][A-Z .'-]+?)(\s+(?:ON|REF)\b)", re.I)


def mask(text: object) -> str:
    return _NAME.sub(lambda m: f"{m.group(1)}<NAME>{m.group(3)}", str(text))


def main(path: str) -> int:
    source = Path(path)
    print(f"file: {source.name}")

    # --- What the ORIGINAL code would have done -----------------------------
    raw_headerless = pd.read_csv(source, header=None)
    print(f"\nraw shape (header=None): {raw_headerless.shape}")
    print("row 0 (would be the header):", [str(v)[:14] for v in raw_headerless.iloc[0].tolist()])

    print("\n--- what the ORIGINAL positional code would have read ---")
    legacy_amounts = [parse_amount(v) for v in raw_headerless.iloc[1:, 1]]
    zeros = sum(1 for a in legacy_amounts if a is None or a == 0)
    print(f"  iloc[:,1] as amount -> {zeros} of {len(legacy_amounts)} unusable/zero")
    print(f"  iloc[:,4] as details -> sample: {raw_headerless.iloc[1, 4]!r}")

    # --- What the NEW parser does -------------------------------------------
    print("\n--- new parser ---")
    result = wells_fargo.parse(raw_headerless, source_file=source.name)
    print(f"  ok             : {result.ok}")
    print(f"  rows parsed    : {result.row_count}")
    print(f"  rows rejected  : {result.rejected_rows}")
    for err in result.errors:
        print(f"  ERROR   : {err}")
    for warn in result.warnings:
        print(f"  WARNING : {warn}")

    if not result.ok:
        return 1

    rows = result.rows
    amounts = pd.to_numeric(rows["amount"], errors="coerce")
    print(f"\n  date range     : {rows['transaction_date'].min()} .. {rows['transaction_date'].max()}")
    print(f"  credits        : {int((amounts > 0).sum())}  totalling ${amounts[amounts > 0].sum():,.2f}")
    print(f"  debits         : {int((amounts < 0).sum())}  totalling ${amounts[amounts < 0].sum():,.2f}")
    print(f"  zero amounts   : {int((amounts == 0).sum())}")
    print(f"  net            : ${amounts.sum():,.2f}")
    print("\n  sample parsed rows (names masked):")
    for _, row in rows.head(4).iterrows():
        print(f"    {row['transaction_date']}  {row['amount']:>10.2f}  {mask(row['details'])[:78]}")

    # --- Categorization ------------------------------------------------------
    print("\n--- categorization (no model: sandbox) ---")
    categorized, run = categorize_frame(rows)
    counts = run.counts_by_source
    print(f"  {run.summary_line()}")
    print(f"  resolved locally : {run.rows_resolved_locally}")
    print(f"  would hit model  : {run.rows_sent_to_model}")
    print(f"  coverage         : {run.coverage:.1f}%")

    by_committee = categorized["budget_category"].value_counts(dropna=False)
    print("\n  assignments by committee id:")
    for committee_id, count in by_committee.items():
        print(f"    {committee_id if pd.notna(committee_id) else 'unassigned':>12} : {count}")

    print("\n  top rules fired:")
    for rule, count in categorized["match_rule"].replace("", "(none)").value_counts().head(8).items():
        print(f"    {count:>4}  {rule[:70]}")

    # --- Merchant-key coverage on the residual -------------------------------
    residual = categorized[categorized["budget_category"].isna()]
    keys = [merchant_key(d) for d in residual["details"]]
    learnable = sum(1 for k in keys if k)
    print(f"\n  residual rows          : {len(residual)}")
    print(f"  with a learnable key   : {learnable}")
    distinct = len({k for k in keys if k})
    print(f"  distinct merchants     : {distinct}")
    print(f"  -> mapping those {distinct} merchants once would clear {learnable} rows permanently")

    print("\n  most common unresolved merchants:")
    counter = pd.Series([k for k in keys if k]).value_counts().head(12)
    for key, count in counter.items():
        print(f"    {count:>4}  {key[:60]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
