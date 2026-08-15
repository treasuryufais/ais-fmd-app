"""
Bulk merchant mapping (P3) -- propose, review, then apply.

Three steps, deliberately separate, because the middle one is a human's.

    # 1. propose -- reads only, writes a CSV you can open in a spreadsheet
    python scripts/propose_merchants.py

    # 2. review  -- open merchant_proposals.csv, set `confirm` to y on the rows
    #               you agree with, and correct `committee_id` where it is wrong

    # 3. apply   -- writes ONLY the confirmed rows into the merchants table
    python scripts/propose_merchants.py --apply merchant_proposals.csv

Why it is not one step: a merchant->committee mapping decides which committee
real spending is charged against, for every future statement. That is the same
class of decision as the disputed purpose mappings in HANDOFF §8. The tool does
the grouping, the arithmetic and the suggesting; a treasurer does the deciding.

`--apply` refuses rows without an explicit confirmation, and refuses a committee
id it does not recognise, so a typo cannot silently book money somewhere odd.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.config.categories import COMMITTEE_BY_ID, committee_name
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.categorize import bulk
from ais_fmd.domain.categorize.merchants import MerchantMemory
from ais_fmd.domain.categorize.pipeline import categorize_records

DEFAULT_OUTPUT = "merchant_proposals.csv"

FIELDNAMES = [
    "confirm",
    "committee_id",
    "committee",
    "confidence",
    "rows",
    "total_amount",
    "would_rebook",
    "kind",
    "key",
    "display_name",
    "basis",
    "warning",
]


def classify_all(backend: SqliteBackend) -> tuple[list[dict], list[dict], object]:
    """Returns (all records, residual records, run)."""
    transactions = backend.fetch_transactions()
    memory = MerchantMemory.from_records(backend.fetch_merchants().to_dict("records"))
    records = transactions.to_dict("records")
    run = categorize_records(records, memory)
    residual = [
        record
        for record, classification in zip(records, run.classifications)
        if not classification.is_assigned
    ]
    return records, residual, run


def command_propose(output_path: Path) -> int:
    backend = SqliteBackend()
    records, residual, run = classify_all(backend)

    print(run.summary_line())
    if not residual:
        print("\nNothing unresolved. Merchant memory has nothing left to learn.")
        return 0

    report = bulk.build_report(
        residual,
        all_records=records,
        committee_ids=[c.committee_id for c in run.classifications],
    )
    print(report.summary_line())

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for proposal in report.proposals:
            writer.writerow(
                {
                    # Pre-tick nothing. Every row is an explicit human decision.
                    "confirm": "",
                    "committee_id": proposal.committee_id or "",
                    "committee": proposal.committee,
                    "confidence": f"{proposal.confidence:.2f}",
                    "rows": proposal.row_count,
                    "total_amount": f"{proposal.total_amount:.2f}",
                    "would_rebook": proposal.override_count or "",
                    "kind": proposal.kind,
                    "key": proposal.key,
                    "display_name": proposal.display_name,
                    "basis": proposal.basis,
                    "warning": proposal.override_warning(),
                }
            )

    print(f"\nWrote {len(report.proposals)} proposals to {output_path}")
    print(
        f"If every proposal were confirmed, {report.rows_covered_if_confirmed} of "
        f"{report.residual_rows} unresolved rows would resolve permanently."
    )

    memo_proposals = [p for p in report.proposals if p.kind == bulk.KIND_MEMO]

    print("\nHighest-value decisions (by rows affected):")
    for proposal in sorted(report.proposals, key=lambda p: -p.row_count)[:12]:
        flag = "  " if not proposal.needs_decision else "??"
        target = proposal.committee or "(undecided)"
        print(
            f"  {flag} {proposal.row_count:3} rows  {proposal.total_amount:>11,.2f}  "
            f"{proposal.display_name[:34]:34}  -> {target}"
        )

    if report.conflicting:
        print(
            "\nWARNING -- these mappings would change rows that are ALREADY "
            "classified.\nMerchant memory is consulted before the deterministic "
            "rules, so it wins over them:"
        )
        for proposal in report.conflicting:
            print(f"  {proposal.display_name[:30]:30}  {proposal.override_warning()}")

    if memo_proposals:
        print(
            f"\n{len(memo_proposals)} group(s) are incoming-transfer memos, not merchants. "
            "These cannot\nbe stored as merchant rules -- a member's name must never become "
            "a rule. They\nneed a deterministic memo rule in predicates.py instead. Listed "
            "here as findings:"
        )
        for proposal in memo_proposals:
            target = proposal.committee or "(undecided)"
            print(
                f"  {proposal.row_count:3} rows  {proposal.total_amount:>10,.2f}  "
                f"memo mentions '{proposal.key}'  -> {target}"
            )

    print(
        "\nNext: open the CSV, put 'y' in `confirm` for the rows you agree with "
        "(correct `committee_id` first where it is blank or wrong), then run:\n"
        f"  python scripts/propose_merchants.py --apply {output_path}"
    )
    return 0


def _confirmed_rows(path: Path) -> tuple[list[dict], list[str]]:
    """Parse the reviewed CSV. Returns (accepted rows, complaints)."""
    accepted: list[dict] = []
    complaints: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            if str(row.get("confirm", "")).strip().lower() not in {"y", "yes", "1", "true"}:
                continue

            raw_id = str(row.get("committee_id", "")).strip()
            if not raw_id:
                complaints.append(f"line {line_number}: confirmed but committee_id is blank")
                continue
            try:
                committee_id = int(float(raw_id))
            except ValueError:
                complaints.append(f"line {line_number}: committee_id {raw_id!r} is not a number")
                continue
            if committee_id not in COMMITTEE_BY_ID:
                complaints.append(
                    f"line {line_number}: committee_id {committee_id} is not a known committee"
                )
                continue

            key = str(row.get("key", "")).strip()
            if not key:
                complaints.append(f"line {line_number}: confirmed but key is blank")
                continue
            if str(row.get("kind", "")).strip() != bulk.KIND_MERCHANT:
                # Memo themes are not merchant keys; they would need a rule, not
                # a row in the merchants table. Refuse rather than write junk.
                complaints.append(
                    f"line {line_number}: kind={row.get('kind')!r} cannot be stored as a "
                    f"merchant rule (only '{bulk.KIND_MERCHANT}' can)"
                )
                continue

            accepted.append(
                {
                    "merchant_key": key,
                    "canonical_name": str(row.get("display_name") or key).strip(),
                    "committee_id": committee_id,
                    "purpose": bulk.COMMITTEE_PURPOSE.get(committee_id, ""),
                    "source": "bulk-confirmed",
                }
            )
    return accepted, complaints


def command_apply(path: Path, actor: str, dry_run: bool) -> int:
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 2

    accepted, complaints = _confirmed_rows(path)

    for complaint in complaints:
        print(f"REFUSED  {complaint}", file=sys.stderr)

    if not accepted:
        print("\nNothing confirmed. No rules written.")
        return 1 if complaints else 0

    print(f"{len(accepted)} confirmed mapping(s):")
    for rule in accepted:
        print(
            f"  {rule['merchant_key']:34} -> {rule['committee_id']:>2} "
            f"{committee_name(rule['committee_id'])}"
        )

    if dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    backend = SqliteBackend()
    before = len(backend.fetch_merchants())
    result = backend.upsert_merchants(accepted, actor)
    after = len(backend.fetch_merchants())

    print(f"\nmerchants table: {before} -> {after} rules (updated={result.updated})")
    if getattr(result, "message", ""):
        print(f"  {result.message}")

    # Show the effect, which is the only number that matters.
    _records, _residual, run = classify_all(backend)
    print(f"\nAfter applying: {run.summary_line()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--apply",
        metavar="CSV",
        help="Write the confirmed rows of a reviewed proposals CSV into the merchants table.",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUTPUT, help=f"Proposal file to write (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument("--actor", default="bulk-mapping-script")
    parser.add_argument(
        "--dry-run", action="store_true", help="With --apply: validate and print, write nothing."
    )
    args = parser.parse_args(argv)

    if args.apply:
        return command_apply(Path(args.apply), args.actor, args.dry_run)
    return command_propose(Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
