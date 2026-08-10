"""
Categorization orchestration.

The order is the whole point:

    1. Merchant memory   -- free, instant, and the set grows with every correction
    2. Deterministic rules -- free, exact, and covers the well-understood cases
    3. The model          -- only what is genuinely left over

The original ran the model *first*, over every transaction, and then overrode
most of its answers with Python rules -- paying for answers it discarded. This
inverts that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .llm import LLMOutcome, classify_residual
from .merchants import MerchantMemory
from .predicates import UNMATCHED, Classification, classify_deterministic


@dataclass
class CategorizationRun:
    """Results plus the accounting needed to show what the run cost."""

    classifications: list[Classification] = field(default_factory=list)
    llm: LLMOutcome = field(default_factory=LLMOutcome)

    @property
    def total(self) -> int:
        return len(self.classifications)

    @property
    def counts_by_source(self) -> dict[str, int]:
        counts = {"merchant": 0, "rule": 0, "llm": 0, "none": 0}
        for item in self.classifications:
            counts[item.source] = counts.get(item.source, 0) + 1
        return counts

    @property
    def assigned(self) -> int:
        return sum(1 for item in self.classifications if item.is_assigned)

    @property
    def coverage(self) -> float:
        return (self.assigned / self.total * 100) if self.total else 0.0

    @property
    def rows_sent_to_model(self) -> int:
        return self.counts_by_source["none"] + self.counts_by_source["llm"]

    @property
    def rows_resolved_locally(self) -> int:
        return self.counts_by_source["merchant"] + self.counts_by_source["rule"]

    def summary_line(self) -> str:
        counts = self.counts_by_source
        return (
            f"{self.total} transactions: {counts['merchant']} from merchant memory, "
            f"{counts['rule']} by rule, {counts['llm']} by model, "
            f"{counts['none']} left for review."
        )


def categorize_records(
    records: list[dict],
    merchants: MerchantMemory | None = None,
) -> CategorizationRun:
    """Classify a list of transaction dicts."""
    memory = merchants or MerchantMemory()
    results: list[Classification] = [UNMATCHED] * len(records)
    residual: list[tuple[int, dict]] = []

    for index, record in enumerate(records):
        remembered = memory.lookup(record.get("details"))
        if remembered is not None:
            results[index] = remembered
            continue

        deterministic = classify_deterministic(record)
        if deterministic.is_assigned:
            results[index] = deterministic
            continue

        residual.append((index, record))

    outcome = classify_residual(residual)
    for index, classification in outcome.classifications.items():
        results[index] = classification

    return CategorizationRun(classifications=results, llm=outcome)


def categorize_frame(
    df: pd.DataFrame,
    merchants: MerchantMemory | None = None,
) -> tuple[pd.DataFrame, CategorizationRun]:
    """
    Categorize a parsed statement frame.

    Adds: budget_category, purpose, match_rule, match_confidence, match_source.

    FINDING F18 (fragility). The original realigned results with
    `df.loc[mask, col] = enhanced.loc[mask, col].values`, which was correct only
    because an upstream reset_index happened to leave the frames aligned. Results
    are built as plain lists indexed by position and assigned once, so there is
    no alignment to get wrong.
    """
    out = df.reset_index(drop=True).copy()
    if out.empty:
        for column in ("budget_category", "purpose", "match_rule", "match_confidence", "match_source"):
            out[column] = pd.Series(dtype="object")
        return out, CategorizationRun()

    records = out.to_dict("records")
    run = categorize_records(records, merchants)

    out["budget_category"] = [item.committee_id for item in run.classifications]
    out["purpose"] = [item.purpose for item in run.classifications]
    out["match_rule"] = [item.rule for item in run.classifications]
    out["match_confidence"] = [item.confidence for item in run.classifications]
    out["match_source"] = [item.source for item in run.classifications]
    return out, run
