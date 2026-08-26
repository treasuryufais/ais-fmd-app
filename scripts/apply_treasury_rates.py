"""
Write treasury's confirmed dues rates into the terms table.

Rates live on the term as data (`terms.dues_rates`), so entering them is not a
code change -- but it is a change that moves money between committees, and it
happens across several rows at once. Same discipline as
`propose_merchants.py` and `spot_check.py`: the script shows what it would do,
you read it, then you pass `--apply`.

What it reports before writing:

  * every term it will touch, old rates -> new rates
  * any term treasury confirmed that this database does not have
  * how many transactions change classification as a result, split into
    newly-recognised dues and dues that stop being recognised

That last number is the one to read. Dues match on an *exact* amount, so a rate
correction does not just add rows -- it can remove them, and the Spring 2025
case in the real data (both rate pairs in circulation at once) is exactly how
that happens. A run that takes dues away is not necessarily wrong, but it should
never be a surprise.

    .venv/Scripts/python scripts/apply_treasury_rates.py            # dry run
    .venv/Scripts/python scripts/apply_treasury_rates.py --apply

Source of the rates: `ais_fmd/domain/dues.CONFIRMED_DUES_RATES`, which records
what treasury answered and when. Add to that dict, not to this script.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from ais_fmd.domain.categorize import pipeline
from ais_fmd.domain.dues import CONFIRMED_DUES_RATES, format_rates, parse_rates, schedule_from_terms

DEFAULT_DB = Path(__file__).resolve().parents[1] / "sandbox_data" / "ais_fmd_sandbox.db"
DUES_COMMITTEE = 1


def load(db: Path) -> tuple[list[dict], pd.DataFrame]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in connection.execute("SELECT * FROM transactions")]
        terms = pd.read_sql_query("SELECT * FROM terms ORDER BY start_date", connection)
    finally:
        connection.close()
    return rows, terms


def planned_changes(terms: pd.DataFrame) -> list[dict]:
    """One entry per term whose stored rates or verified flag would change."""
    changes: list[dict] = []
    for row in terms.to_dict("records"):
        semester = str(row.get("Semester") or "")
        if semester not in CONFIRMED_DUES_RATES:
            continue
        confirmed = CONFIRMED_DUES_RATES[semester]
        current = parse_rates(row.get("dues_rates"))
        verified = row.get("dues_rates_verified")
        already_verified = bool(0 if verified is None or pd.isna(verified) else int(verified))
        if current == confirmed and already_verified:
            continue
        changes.append(
            {
                "term_id": str(row.get("TermID") or ""),
                "semester": semester,
                "before": format_rates(current) if current else "(none)",
                "after": format_rates(confirmed),
                "was_verified": already_verified,
            }
        )
    return changes


def dues_count(rows: list[dict], terms: pd.DataFrame) -> set:
    """Natural keys of every row the pipeline currently calls dues."""
    run = pipeline.categorize_records(rows, dues=schedule_from_terms(terms))
    return {
        rows[i].get("natural_key") or rows[i].get("TransactionID") or i
        for i, item in enumerate(run.classifications)
        if item.committee_id == DUES_COMMITTEE
    }


def apply_changes(db: Path, changes: list[dict]) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.executemany(
            "UPDATE terms SET dues_rates = ?, dues_rates_verified = 1 WHERE TermID = ?",
            [(change["after"], change["term_id"]) for change in changes],
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the timestamped copy taken before writing",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}")
        return 1

    rows, terms = load(args.db)
    changes = planned_changes(terms)

    known = set(terms["Semester"].astype(str)) if "Semester" in terms.columns else set()
    missing = [s for s in CONFIRMED_DUES_RATES if s not in known]
    if missing:
        # Fall 2026 is the case that matters: if the term row does not exist,
        # its payments fall outside every window and silently take the default
        # rates, which is the failure this whole mechanism exists to prevent.
        print("Confirmed by treasury but ABSENT from this database:")
        for semester in missing:
            print(f"  {semester:<14} {format_rates(CONFIRMED_DUES_RATES[semester])}")
        print("  Create the term first (Treasury -> Terms), then re-run.\n")

    if not changes:
        print("Every confirmed term already carries its rates. Nothing to do.")
        return 0

    print(f"{len(changes)} term(s) to update in {args.db}:\n")
    for change in changes:
        flag = "" if change["was_verified"] else "  [was unconfirmed]"
        print(
            f"  {change['semester']:<14} {change['before']:>14}"
            f"  ->  {change['after']:<14}{flag}"
        )

    before = dues_count(rows, terms)
    after_terms = terms.copy()
    for change in changes:
        mask = after_terms["TermID"].astype(str) == change["term_id"]
        after_terms.loc[mask, "dues_rates"] = change["after"]
        after_terms.loc[mask, "dues_rates_verified"] = 1
    after = dues_count(rows, after_terms)

    gained, lost = after - before, before - after
    print(f"\nDues rows recognised: {len(before)} -> {len(after)}")
    print(f"  newly recognised : {len(gained)}")
    print(f"  no longer dues   : {len(lost)}")
    if lost:
        print(
            "  ^ read this before applying. Dues match an exact amount, so a rate\n"
            "    correction can take rows away as well as add them."
        )

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.db.with_name(f"{args.db.stem}.{stamp}.bak{args.db.suffix}")
        shutil.copy2(args.db, backup)
        print(f"\nBacked up to {backup}")

    apply_changes(args.db, changes)
    print(f"Updated {len(changes)} term(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
