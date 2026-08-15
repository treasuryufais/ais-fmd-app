"""
Spot-check auto-applied rows -- sample, review, then apply.

The gap this closes: the app reports **coverage** (how many rows got an answer)
and nobody has ever measured **accuracy** on the rows it books without asking.
Every human label in the system came either from a historical ledger or from a
row the categorizer flagged as uncertain. Neither says anything about the
confident majority.

Three steps, deliberately separate, because the middle one is a human's.

    # 1. sample -- reads only, writes a CSV you can open in a spreadsheet
    python scripts/spot_check.py --size 40

    # 2. review -- open spot_check.csv. For each row, either leave `verdict`
    #              as `ok` if the committee is right, or set it to the correct
    #              committee id. Blank means "not reviewed" and is skipped.

    # 3. apply  -- writes the reviewed rows into labeled_examples as
    #              source='spot-check'
    python scripts/spot_check.py --apply spot_check.csv

    # at any time -- what the spot-checks have shown so far
    python scripts/spot_check.py --report

The sample is stratified by tier and committee with a floor of one per stratum,
and deterministic given `--seed`, so it can be paused and resumed and two people
reviewing "the sample" see the same rows. See `domain/categorize/spotcheck.py`
for why it is not a uniform random draw.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.config.categories import COMMITTEE_BY_ID, committee_label
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.categorize import spotcheck
from ais_fmd.domain.categorize.merchants import MerchantMemory
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.scoring import CURRENT_ERA
from ais_fmd.domain.dues import schedule_from_terms

DEFAULT_OUTPUT = "spot_check.csv"

FIELDNAMES = [
    "verdict",
    "model_committee_id",
    "model_committee",
    "amount",
    "transaction_date",
    "details",
    "tier",
    "confidence",
    "rule",
    "transaction_id",
]


def classify(backend: SqliteBackend):
    transactions = backend.fetch_transactions()
    records = transactions.to_dict("records")
    memory = MerchantMemory.from_records(backend.fetch_merchants().to_dict("records"))
    schedule = schedule_from_terms(backend.fetch_terms())
    run = categorize_records(records, memory, dues=schedule)
    return records, run


def do_sample(backend: SqliteBackend, output: Path, size: int, seed: int) -> int:
    records, run = classify(backend)
    sample = spotcheck.build_sample(records, run.classifications, size=size, seed=seed)

    if not sample.size:
        print("No auto-applied rows to check.")
        return 1

    print(sample.summary_line())
    print(sample.coverage_note())
    print()
    print("strata (tier, committee) -> population / sampled:")
    counts: dict = {}
    for row in sample.rows:
        counts[row.stratum] = counts.get(row.stratum, 0) + 1
    for key in sorted(sample.strata, key=lambda k: (-sample.strata[k], str(k))):
        tier, committee_id = key
        print(
            f"   {tier:7} {committee_label(committee_id):24} "
            f"{sample.strata[key]:5} / {counts.get(key, 0)}"
        )

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in sample.rows:
            writer.writerow(
                {
                    "verdict": "",
                    "model_committee_id": row.committee_id,
                    "model_committee": row.committee,
                    "amount": f"{row.amount:.2f}" if row.amount is not None else "",
                    "transaction_date": row.record.get("transaction_date") or "",
                    "details": row.details[:160],
                    "tier": row.source,
                    "confidence": f"{row.confidence:.2f}",
                    "rule": row.rule,
                    "transaction_id": row.record.get("transactionid") or "",
                }
            )

    print(f"\nWrote {output} ({sample.size} rows).")
    print(
        "Review it: leave `verdict` as `ok` where the committee is right, or put "
        "the correct\ncommittee id there. Blank rows are skipped. Then re-run "
        f"with --apply {output}."
    )
    return 0


def do_apply(backend: SqliteBackend, path: Path, actor: str, era: str) -> int:
    records, run = classify(backend)
    by_transaction = {
        str(record.get("transactionid")): (index, record)
        for index, record in enumerate(records)
    }

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    labels: list[dict] = []
    skipped = 0
    problems: list[str] = []

    for line, row in enumerate(rows, start=2):
        verdict = (row.get("verdict") or "").strip().lower()
        if not verdict:
            skipped += 1
            continue

        transaction_id = (row.get("transaction_id") or "").strip()
        found = by_transaction.get(transaction_id)
        if found is None:
            problems.append(f"line {line}: transaction {transaction_id or '(blank)'} not found")
            continue
        index, record = found

        classification = run.classifications[index]
        if not classification.is_assigned:
            problems.append(f"line {line}: transaction {transaction_id} is no longer auto-applied")
            continue

        if verdict in ("ok", "y", "yes", "correct"):
            committee_id = classification.committee_id
        else:
            try:
                committee_id = int(verdict)
            except ValueError:
                problems.append(
                    f"line {line}: verdict '{verdict}' is neither 'ok' nor a committee id"
                )
                continue
            if committee_id not in COMMITTEE_BY_ID:
                problems.append(f"line {line}: committee {committee_id} does not exist")
                continue

        sample_row = spotcheck.SpotCheckRow(
            index=index,
            record=record,
            committee_id=classification.committee_id,
            committee=committee_label(classification.committee_id),
            rule=classification.rule,
            confidence=classification.confidence,
            source=classification.source,
            amount=None,
        )
        labels.append(spotcheck.to_label(sample_row, committee_id, actor, era))

    if problems:
        print("Refusing to apply -- fix these first:")
        for problem in problems:
            print(f"   {problem}")
        return 1

    if not labels:
        print(f"Nothing to apply ({skipped} rows had a blank verdict).")
        return 1

    result = backend.insert_labeled_examples(labels, actor)
    if result.error:
        print(f"Failed: {result.error}")
        return 1

    disagreements = sum(1 for l in labels if l["committee_id"] != l["model_committee"])
    recorded = result.updated if result.updated else len(labels) - result.unchanged
    print(f"Reviewed {len(labels)} rows ({skipped} skipped as unreviewed).")
    print(f"   newly recorded        : {recorded}")
    print(f"   agreed with the model : {len(labels) - disagreements}")
    print(f"   corrected it          : {disagreements}")
    if result.unchanged:
        print(
            f"   already checked       : {result.unchanged} "
            f"(kept the original verdict -- one spot-check per row)"
        )
    return 0


def do_report(backend: SqliteBackend) -> int:
    labels = backend.fetch_labeled_examples().to_dict("records")
    stats = spotcheck.agreement(labels)
    if not stats["checked"]:
        print(
            "No spot-check labels yet.\n\n"
            "Until there are, accuracy on auto-applied rows is unmeasured: every\n"
            "label in the system comes either from a historical ledger or from a\n"
            "row the categorizer already flagged as uncertain."
        )
        return 1

    print(f"Spot-checked {stats['checked']} auto-applied rows.")
    print(f"   model agreed with the human: {stats['agreed']} ({stats['rate'] * 100:.1f}%)")
    print()
    print("by tier:")
    for source, entry in sorted(stats["by_source"].items()):
        rate = entry["agreed"] / entry["checked"] * 100 if entry["checked"] else 0.0
        print(f"   {source:8} {entry['agreed']:4}/{entry['checked']:<4} {rate:5.1f}%")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", type=Path, metavar="CSV")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument("--size", type=int, default=40)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--actor", default="spot-check")
    parser.add_argument("--era", default=CURRENT_ERA)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()

    backend = SqliteBackend(str(args.db)) if args.db else SqliteBackend()

    if args.report:
        return do_report(backend)
    if args.apply:
        if not args.apply.exists():
            print(f"No such file: {args.apply}")
            return 1
        return do_apply(backend, args.apply, args.actor, args.era)
    return do_sample(backend, args.output, args.size, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
