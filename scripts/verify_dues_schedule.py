"""
Prove the per-term dues schedule against the real statement data.

Two things need proving, and unit tests can only do the first in miniature:

  1. **Behaviour preservation.** Seeding every term with the rates `rule_dues`
     used as a constant must reproduce the old classifications exactly. Anything
     else means the refactor moved money.
  2. **The failure it exists to prevent.** Rates change, nobody updates the app,
     and because dues are matched on an *exact* amount every dues row silently
     stops being recognised.

It also reports incoming transfers sitting at a documented handbook rate the app
does not currently know about -- the evidence a treasurer needs to fill the rates
in. It never writes: pass `--db` a copy if you want to be certain.

    .venv/Scripts/python scripts/verify_dues_schedule.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from ais_fmd.domain.categorize import pipeline
from ais_fmd.domain.categorize.predicates import DUES_AMOUNTS, is_venmo_or_zelle
from ais_fmd.domain.dues import format_rates, schedule_from_terms
from ais_fmd.domain.money import parse_amount
from ais_fmd.domain.terms import attach_semester

DEFAULT_DB = Path(__file__).resolve().parents[1] / "sandbox_data" / "ais_fmd_sandbox.db"

# From the VP Treasury Handbook's rate history ($20/$40 -> $25/$40 -> $30/$50 ->
# $35/$52.50). Rates the current constant does NOT include.
HANDBOOK_RATES_NOT_IN_USE = [Decimal(x) for x in ("20.00", "25.00", "30.00", "40.00", "50.00")]


def load(db: Path) -> tuple[list[dict], pd.DataFrame]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in connection.execute("SELECT * FROM transactions")]
        terms = pd.read_sql_query("SELECT * FROM terms ORDER BY start_date", connection)
    finally:
        connection.close()
    return rows, terms


def check_behaviour_preserved(rows: list[dict], terms: pd.DataFrame) -> bool:
    """Seed every term with the old constant; classifications must not move."""
    seeded = terms.copy()
    seeded["dues_rates"] = format_rates(DUES_AMOUNTS)
    seeded["dues_rates_verified"] = 0

    baseline = pipeline.categorize_records(rows)
    after = pipeline.categorize_records(rows, dues=schedule_from_terms(seeded))

    print("[1] behaviour preservation")
    print(f"    baseline : {baseline.summary_line()}")
    print(f"    seeded   : {after.summary_line()}")

    diffs = [
        (i, a, b)
        for i, (a, b) in enumerate(zip(baseline.classifications, after.classifications))
        if (a.committee_id, a.source) != (b.committee_id, b.source)
    ]
    if diffs:
        print(f"    FAIL: {len(diffs)} classifications changed")
        for i, a, b in diffs[:10]:
            print(f"      row {i}: {a.committee_id}/{a.source} -> {b.committee_id}/{b.source}")
        return False
    print(f"    OK: 0 of {len(rows)} classifications changed")
    return True


def demonstrate_the_time_bomb(rows: list[dict], terms: pd.DataFrame) -> None:
    """Show what an unannounced rate change does, and that the schedule fixes it."""
    baseline = pipeline.categorize_records(rows)
    dues_rows = [r for r, c in zip(rows, baseline.classifications) if c.committee_id == 1]
    if not dues_rows:
        print("\n[2] no dues rows in this database; skipping")
        return

    future = terms[terms["end_date"] >= "2026-08-01"]
    if future.empty:
        print("\n[2] no future term to test against; skipping")
        return
    term = future.iloc[0]
    when = str(term["start_date"])[:10]

    raised = []
    for row in dues_rows:
        copy = dict(row)
        copy["transaction_date"] = when
        copy["amount"] = 40.00 if float(row["amount"]) < 40 else 60.00
        raised.append(copy)

    stale = pipeline.categorize_records(raised)
    recognised = sum(1 for c in stale.classifications if c.committee_id == 1)
    print(f"\n[2] the failure being fixed ({term['Semester']} raises rates to 40 / 60)")
    print(f"    with rates left as a constant : {recognised}/{len(raised)} still recognised as dues")

    updated = terms.copy()
    updated["dues_rates"] = format_rates(DUES_AMOUNTS)
    updated.loc[updated["TermID"] == term["TermID"], "dues_rates"] = "40.00,60.00"
    fixed = pipeline.categorize_records(raised, dues=schedule_from_terms(updated))
    recovered = sum(1 for c in fixed.classifications if c.committee_id == 1)
    print(f"    with the term's rates recorded: {recovered}/{len(raised)} recognised")


def report_unrecognised_handbook_rates(rows: list[dict], terms: pd.DataFrame) -> None:
    """Incoming transfers sitting at a handbook rate the app does not know."""
    frame = pd.DataFrame(rows)
    if frame.empty:
        return
    frame["_amt"] = [parse_amount(v) for v in frame["amount"]]
    incoming = frame[
        [
            amount is not None and amount > 0 and is_venmo_or_zelle(details, account)
            for amount, details, account in zip(
                frame["_amt"], frame["details"], frame.get("account", [None] * len(frame))
            )
        ]
    ].copy()
    matched = incoming[incoming["_amt"].isin(HANDBOOK_RATES_NOT_IN_USE)]
    if matched.empty:
        print("\n[3] no incoming transfers at an unrecognised handbook rate")
        return

    uncategorized = matched[matched["budget_category"].isna()]
    print("\n[3] incoming transfers at a documented handbook rate the app does not know")
    print(f"    total          : {len(matched):4} rows, {float(matched['_amt'].sum()):,.2f}")
    print(f"    uncategorized  : {len(uncategorized):4} rows, {float(uncategorized['_amt'].sum()):,.2f}")
    if uncategorized.empty:
        return
    tagged = attach_semester(uncategorized, terms)
    pivot = tagged.pivot_table(
        index="Semester", columns="_amt", values="transactionid",
        aggfunc="count", fill_value=0,
    )
    print("\n    uncategorized, by term and amount:")
    for line in pivot.to_string().splitlines():
        print(f"      {line}")
    print(
        "\n    These are individual incoming transfers at amounts the handbook "
        "lists\n    as dues rates. Whether they ARE dues is a treasurer's call -- "
        "record the\n    term's real rates and they categorize themselves."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}")
        return 1

    rows, terms = load(args.db)
    print(f"{len(rows)} transactions, {len(terms)} terms, from {args.db.name}\n")

    ok = check_behaviour_preserved(rows, terms)
    demonstrate_the_time_bomb(rows, terms)
    report_unrecognised_handbook_rates(rows, terms)

    print("\nOK" if ok else "\nFAILED: the schedule is not behaviour-preserving")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
