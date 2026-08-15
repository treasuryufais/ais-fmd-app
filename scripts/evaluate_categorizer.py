"""
Measure the categorizer against human labels (M19).

    python scripts/evaluate_categorizer.py
    python scripts/evaluate_categorizer.py --fit        # also print learned weights

Reports, from `labeled_examples`:
  * accuracy per confidence threshold, so the auto-apply bar is chosen from data
  * accuracy per committee, so it is obvious which class is weak
  * the confusion pairs it gets wrong most often
  * whether the label set is biased toward rows the model already found hard

Without this, any change to the weights is an opinion. With it, "did that help?"
has an answer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.config.categories import committee_name
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.categorize.learning import confusion, evaluate, fit_weights
from ais_fmd.domain.categorize.scoring import AUTO_APPLY_THRESHOLD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--fit", action="store_true", help="Print learned signal weights.")
    parser.add_argument(
        "--min-precision",
        type=float,
        default=0.95,
        help="Precision floor used to recommend a threshold (default 0.95).",
    )
    args = parser.parse_args(argv)

    backend = SqliteBackend()
    frame = backend.fetch_labeled_examples()
    if frame.empty:
        print(
            "No labeled examples. Import historical ledgers first:\n"
            "  python scripts/import_ground_truth.py <rendered.txt> --era 2022-2023"
        )
        return 1

    labels = frame.to_dict("records")
    print(f"{len(labels)} human-labeled examples")
    eras = frame["era"].dropna().unique().tolist()
    sources = frame["source"].value_counts().to_dict()
    print(f"  eras: {eras}   sources: {sources}")
    print()

    result = evaluate(labels)

    print("ACCURACY BY THRESHOLD")
    print(f"  {'bar':>6} {'applied':>8} {'correct':>8} {'precision':>10} {'coverage':>9}")
    for point in result.points:
        marker = "  <- current" if abs(point.threshold - AUTO_APPLY_THRESHOLD) < 1e-9 else ""
        print(
            f"  {point.threshold:>6.2f} {point.applied:>8} {point.correct:>8} "
            f"{point.precision:>9.1%} {point.coverage:>8.1%}{marker}"
        )

    recommended = result.recommend(args.min_precision)
    print()
    if recommended:
        print(
            f"Lowest bar meeting {args.min_precision:.0%} precision: "
            f"{recommended.threshold:.2f} "
            f"({recommended.precision:.1%} precise, {recommended.coverage:.1%} of rows)"
        )
    else:
        print(
            f"No threshold reaches {args.min_precision:.0%} precision on this label set. "
            f"The signals do not yet separate these classes -- more labels, or a new "
            f"signal, rather than a higher bar."
        )
    if result.unscored:
        print(f"{result.unscored} labeled rows produced no signal at all ({result.unscored/result.total:.0%}).")
    if result.drift:
        print(
            f"{result.drift} mismatches excluded as taxonomy drift — the prediction is "
            f"right under\n  today's chart of accounts but the label predates it "
            f"(see KNOWN_TAXONOMY_DRIFT)."
        )

    warning = result.selection_bias_warning()
    if warning:
        print()
        print(f"SELECTION BIAS: {warning}")

    print()
    print("ACCURACY BY COMMITTEE (of rows that scored at all)")
    for committee_id, (correct, total) in sorted(
        result.by_committee.items(), key=lambda kv: -kv[1][1]
    ):
        share = correct / total if total else 0.0
        print(f"  {committee_name(committee_id):<26} {correct:>4}/{total:<4} {share:>6.1%}")

    wrong = confusion(labels)
    if wrong:
        print()
        print("MOST COMMON MISTAKES (predicted -> actual)")
        for (predicted, actual), count in wrong.most_common(10):
            print(
                f"  {count:>4}x  {committee_name(predicted):<24} -> {committee_name(actual)}"
            )

    if args.fit:
        fitted = fit_weights(labels)
        print()
        print("LEARNED SIGNAL WEIGHTS")
        print(f"  {fitted.summary_line()}")
        print(f"  {'signal':<26} {'votes for':<24} {'fired':>6} {'agreed':>7} {'weight':>7}")
        for stat in fitted.table():
            trust = "" if stat.trustworthy else "  (too few)"
            print(
                f"  {stat.name:<26} {committee_name(stat.committee_id):<24} "
                f"{stat.fired:>6} {stat.agreed:>7} {stat.learned_weight:>7.2f}{trust}"
            )
        print()
        print(
            "  Card signals are excluded: cardholders change between eras, so a "
            "weight fitted\n  across them would be meaningless. Pass "
            "include_card_signals=True only within one era."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
