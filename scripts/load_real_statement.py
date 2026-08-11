"""
End-to-end test of the upload path against a real statement.

Exercises exactly what the Treasury page does -- read, parse, categorize,
deduplicate, insert atomically -- then re-imports the same file to prove
deduplication holds, and checks the audit trail.

    python scripts/load_real_statement.py "<csv>"                 # temp db, verify only
    python scripts/load_real_statement.py "<csv>" --into-sandbox  # load into the app

Personal names are never printed.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, COMMITTEES
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.categorize.merchants import MerchantMemory
from ais_fmd.domain.categorize.pipeline import categorize_frame
from ais_fmd.domain.dedupe import split_new_and_duplicate
from ais_fmd.domain.parsers import wells_fargo

ACTOR = "real-statement-test@sandbox.local"


def bootstrap_reference_data(backend: SqliteBackend, rows: pd.DataFrame) -> None:
    """Committees, plus terms covering the statement's date range."""
    with backend._connect() as connection:  # noqa: SLF001
        connection.executemany(
            "INSERT OR REPLACE INTO committees "
            "(CommitteeID, Committee_Name, Committee_Type) VALUES (?, ?, ?)",
            [(c.id, c.name, c.kind) for c in COMMITTEES],
        )
        connection.commit()

    dates = pd.to_datetime(rows["transaction_date"])
    start, end = dates.min().date(), dates.max().date()

    # Fall runs Aug-Dec, Spring Jan-May. Generate terms covering the whole range.
    year = start.year
    while year <= end.year:
        for term_id, label, (s_month, s_day), (e_month, e_day) in (
            (f"SP{str(year)[2:]}", f"Spring {year}", (1, 5), (5, 15)),
            (f"FA{str(year)[2:]}", f"Fall {year}", (8, 15), (12, 20)),
        ):
            backend.insert_term(
                term_id,
                label,
                date(year, s_month, s_day).isoformat(),
                date(year, e_month, e_day).isoformat(),
                ACTOR,
            )
            backend.upsert_budgets(
                term_id, {cid: 1500.0 for cid in BUDGETED_COMMITTEE_IDS}, ACTOR
            )
        year += 1

    # Summer gap so nothing falls outside a term.
    year = start.year
    while year <= end.year:
        backend.insert_term(
            f"SU{str(year)[2:]}",
            f"Summer {year}",
            date(year, 5, 16).isoformat(),
            date(year, 8, 14).isoformat(),
            ACTOR,
        )
        year += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--into-sandbox", action="store_true")
    args = parser.parse_args()

    source = Path(args.csv)

    if args.into_sandbox:
        backend = SqliteBackend()
        backend.reset()
        print(f"target: sandbox database ({backend.path})")
    else:
        tmp = Path(tempfile.mkdtemp()) / "verify.db"
        backend = SqliteBackend(tmp)
        print(f"target: temporary database ({tmp})")

    # --- 1. Read exactly as the Treasury page does -------------------------
    raw = pd.read_csv(source, header=None)
    print(f"\n[1] read           : {raw.shape[0]} rows x {raw.shape[1]} cols")

    # --- 2. Parse -----------------------------------------------------------
    parsed = wells_fargo.parse(raw, source_file=source.name)
    print(f"[2] parsed         : ok={parsed.ok} rows={parsed.row_count} rejected={parsed.rejected_rows}")
    for warning in parsed.warnings:
        print(f"    warning        : {warning}")
    for error in parsed.errors:
        print(f"    ERROR          : {error}")
    if not parsed.ok:
        return 1

    bootstrap_reference_data(backend, parsed.rows)

    # --- 3. Categorize ------------------------------------------------------
    memory = MerchantMemory.from_records(backend.fetch_merchants().to_dict("records"))
    categorized, run = categorize_frame(parsed.rows, memory)
    print(f"[3] categorized    : {run.summary_line()}")
    print(f"    coverage       : {run.coverage:.1f}%  (model calls: {run.rows_sent_to_model} rows)")

    # --- 4. Deduplicate -----------------------------------------------------
    staged = categorized.to_dict("records")
    existing = backend.fetch_transactions()
    dedupe = split_new_and_duplicate(
        staged, existing.to_dict("records") if not existing.empty else []
    )
    print(f"[4] dedupe         : {len(dedupe.new)} new, {len(dedupe.duplicates)} duplicate")

    # --- 5. Insert ----------------------------------------------------------
    receipt = backend.insert_transactions(dedupe.new, source.name, ACTOR)
    print(f"[5] insert         : ok={receipt.ok} inserted={receipt.inserted} skipped={receipt.duplicates_skipped}")
    if receipt.error:
        print(f"    ERROR          : {receipt.error}")
        return 1

    # --- 6. Re-import the same file (must be a no-op) -----------------------
    existing2 = backend.fetch_transactions()
    dedupe2 = split_new_and_duplicate(staged, existing2.to_dict("records"))
    receipt2 = backend.insert_transactions(dedupe2.new, source.name, ACTOR)
    total_after = len(backend.fetch_transactions())
    print(f"[6] re-import      : {len(dedupe2.new)} rows offered, {receipt2.inserted} inserted")
    print(f"    total in db    : {total_after} (unchanged: {total_after == receipt.inserted})")

    # --- 7. Verify ----------------------------------------------------------
    stored = backend.fetch_transactions()
    amounts = pd.to_numeric(stored["amount"])
    audit = backend.fetch_audit(100_000)
    print("\n[7] verification")
    print(f"    transactions   : {len(stored)}")
    print(f"    credits        : {(amounts > 0).sum()} totalling ${amounts[amounts > 0].sum():,.2f}")
    print(f"    debits         : {(amounts < 0).sum()} totalling ${amounts[amounts < 0].sum():,.2f}")
    print(f"    net            : ${amounts.sum():,.2f}")
    print(f"    zero amounts   : {(amounts == 0).sum()}")
    print(f"    categorized    : {stored['budget_category'].notna().sum()}")
    print(f"    audit entries  : {len(audit)}")
    print(f"    distinct details: {stored['details'].nunique()}")

    checks = {
        "no zero-amount rows": (amounts == 0).sum() == 0,
        "both credits and debits present": (amounts > 0).any() and (amounts < 0).any(),
        "descriptions are distinct": stored["details"].nunique() > len(stored) * 0.5,
        "re-import inserted nothing": receipt2.inserted == 0,
        "audit row per insert": len(audit[audit["action"] == "insert"]) == receipt.inserted,
        "no absurd magnitudes": amounts.abs().max() < 1e6,
    }
    print("\n    checks:")
    for label, passed in checks.items():
        print(f"      [{'PASS' if passed else 'FAIL'}] {label}")

    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
