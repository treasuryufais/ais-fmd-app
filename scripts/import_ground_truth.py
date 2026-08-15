"""
Import human-labeled history from rendered Drive workbooks (M19).

    python scripts/import_ground_truth.py <rendered.txt> [...] --era 2022-2023

Each input is a JSON file of the shape {"fileContent": "...markdown..."} -- what
the Drive connector returns for a spreadsheet. Plain markdown is accepted too.

WHY THIS MATTERS. Until this ran, the project had no human labels at all:
`transactions.budget_category` was written by the categorizer itself, so fitting
weights to it would have trained the model on its own output. These workbooks
were filled in by past treasurers, which makes them the only ground truth
available.

Idempotent: rows are keyed on (date, amount, details), so re-running imports
nothing new rather than duplicating labels and over-weighting them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.config.categories import committee_name
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.parsers.ledger import parse_workbook


def load_content(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict) and "fileContent" in parsed:
        return str(parsed["fileContent"])
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument(
        "--era",
        default="unknown",
        help="Scopes features that do not survive an officer handover (card numbers).",
    )
    parser.add_argument("--actor", default="ground-truth-import")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing.")
    args = parser.parse_args(argv)

    all_examples: list[dict] = []
    for path in args.files:
        if not path.exists():
            print(f"No such file: {path}", file=sys.stderr)
            return 2
        result = parse_workbook(
            load_content(path), source_ref=path.name, era=args.era
        )
        print(f"{path.name}: {result.summary_line()}")
        if result.skipped_unknown_committee:
            for name, count in sorted(
                result.skipped_unknown_committee.items(), key=lambda kv: -kv[1]
            ):
                print(f"    unrecognised committee {name!r}: {count} row(s) skipped")
        all_examples.extend(result.examples)

    # The same rows appear in more than one workbook; collapse across files too.
    deduped: dict[str, dict] = {}
    for example in all_examples:
        deduped.setdefault(example["natural_key"], example)
    examples = list(deduped.values())

    print()
    print(f"{len(examples)} unique labeled rows across {len(args.files)} file(s)")
    distribution = Counter(committee_name(e["committee_id"]) for e in examples)
    for name, count in distribution.most_common():
        print(f"  {name:<26} {count}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    backend = SqliteBackend()
    before = len(backend.fetch_labeled_examples())
    result = backend.insert_labeled_examples(examples, args.actor)
    after = len(backend.fetch_labeled_examples())

    if result.error:
        print(f"\nFAILED: {result.error}", file=sys.stderr)
        return 1

    print(
        f"\nlabeled_examples: {before} -> {after} "
        f"(inserted {result.updated}, already present {result.unchanged})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
