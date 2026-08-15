"""
Spot-checking auto-applied rows (M20).

The problem this exists for, stated plainly: **the app measures coverage, not
accuracy.** It reports how many rows got an answer. Nothing measures how many of
those answers were right.

Every human label in the system today came from one of two places -- a
historical ledger, or a row the categorizer flagged as uncertain. Both are
biased samples. Fitting weights on rows the model already found hard teaches it
that the world is harder than it is, and says nothing at all about the confident
majority it books without asking. `Evaluation.selection_bias_warning` reports
that, but reporting a gap does not close it.

Closing it needs labels drawn from rows the model was *sure* about. That is what
this module samples. The sample is:

  * **Stratified by tier and committee**, not uniform. A uniform sample of 40
    rows out of 604 would be ~27 exact-rule rows and a handful of scored ones,
    which is the wrong way round -- the scored tier is where the doubt lives.
    Every stratum that exists gets at least one row.
  * **Deterministic given a seed**, so a review can be paused, resumed, and
    re-derived months later, and so two people checking "the sample" are looking
    at the same rows.
  * **Ordered largest-amount-first within a stratum.** A misbooked $2,000 wire
    matters more than a misbooked $6 coffee, and a reviewer with limited time
    should spend it where the money is.

What this module does NOT do is decide anything. It selects rows and records
what the model said about each; a human supplies the verdict.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from ...config.categories import committee_label
from ..money import parse_amount

# Labels produced by confirming a spot-check carry this source, which is what
# `learning.Evaluation.selection_bias_warning` looks for.
SPOT_CHECK_SOURCE = "spot-check"


@dataclass(frozen=True)
class SpotCheckRow:
    """One auto-applied row, selected for a human to confirm or correct."""

    index: int
    record: dict
    committee_id: int
    committee: str
    rule: str
    confidence: float
    source: str
    amount: Decimal | None

    @property
    def stratum(self) -> tuple[str, int]:
        return (self.source, self.committee_id)

    @property
    def details(self) -> str:
        return " ".join(str(self.record.get("details") or "").split())


@dataclass
class SpotCheckSample:
    rows: list[SpotCheckRow] = field(default_factory=list)
    population: int = 0
    strata: dict[tuple[str, int], int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.rows)

    def summary_line(self) -> str:
        if not self.population:
            return "No auto-applied rows to check."
        share = self.size / self.population * 100
        return (
            f"{self.size} of {self.population} auto-applied rows "
            f"({share:.1f}%) across {len(self.strata)} strata."
        )

    def coverage_note(self) -> str:
        """What this sample can and cannot tell you, in one line."""
        if self.size < 30:
            return (
                f"{self.size} rows is a smoke test, not a measurement -- it can "
                f"find a systematic error but cannot put a number on accuracy."
            )
        return (
            f"{self.size} rows supports a rough accuracy estimate "
            f"(+/- roughly {100 / self.size ** 0.5:.0f} points)."
        )


def _stable_rank(record: dict, index: int, seed: int) -> str:
    """
    A deterministic pseudo-random ordering key.

    Hashing the row's own identity rather than calling a random number generator
    means the sample survives a re-run, a different Python version, and rows
    being added to the table -- an existing row keeps its rank.
    """
    identity = "|".join(
        str(record.get(field_name) or "")
        for field_name in ("transactionid", "natural_key", "transaction_date", "details", "amount")
    ) or str(index)
    digest = hashlib.sha256(f"{seed}:{identity}".encode("utf-8")).hexdigest()
    return digest


def build_sample(
    records: list[dict],
    classifications: list,
    *,
    size: int = 40,
    seed: int = 1,
    largest_first: int = 1,
) -> SpotCheckSample:
    """
    Choose `size` auto-applied rows to check, stratified by tier and committee.

    `largest_first` reserves that many slots per stratum for its biggest
    amounts; the rest of each stratum's allocation is drawn pseudo-randomly.
    Both halves matter: the big rows are where an error costs the most, the
    random ones are the only part that supports an unbiased estimate.
    """
    assigned: list[SpotCheckRow] = []
    for index, (record, classification) in enumerate(zip(records, classifications)):
        if classification is None or not classification.is_assigned:
            continue
        # Only tiers the machine decided on its own. A merchant mapping is
        # already a human's explicit decision -- checking it would just be
        # asking the same person the same question twice.
        if classification.source not in ("rule", "scored"):
            continue
        assigned.append(
            SpotCheckRow(
                index=index,
                record=record,
                committee_id=classification.committee_id,
                committee=committee_label(classification.committee_id),
                rule=classification.rule,
                confidence=classification.confidence,
                source=classification.source,
                amount=parse_amount(record.get("amount")),
            )
        )

    if not assigned:
        return SpotCheckSample()

    buckets: dict[tuple[str, int], list[SpotCheckRow]] = {}
    for row in assigned:
        buckets.setdefault(row.stratum, []).append(row)

    strata_sizes = {key: len(rows) for key, rows in buckets.items()}
    allocation = _allocate(strata_sizes, min(size, len(assigned)))

    chosen: list[SpotCheckRow] = []
    for key, quota in allocation.items():
        if quota <= 0:
            continue
        pool = buckets[key]
        by_amount = sorted(
            pool, key=lambda r: (-(abs(r.amount) if r.amount is not None else Decimal(0)), r.index)
        )
        take_big = min(largest_first, quota, len(by_amount))
        picked = by_amount[:take_big]
        picked_indexes = {r.index for r in picked}

        remainder = [r for r in pool if r.index not in picked_indexes]
        remainder.sort(key=lambda r: _stable_rank(r.record, r.index, seed))
        picked.extend(remainder[: quota - take_big])
        chosen.extend(picked)

    chosen.sort(key=lambda r: (r.source, r.committee_id, -(abs(r.amount) if r.amount else Decimal(0))))
    return SpotCheckSample(rows=chosen, population=len(assigned), strata=strata_sizes)


def _allocate(strata_sizes: dict, total: int) -> dict:
    """
    Proportional allocation with a floor of one per stratum.

    A stratum with three rows still gets checked. Without the floor, a rare
    committee -- exactly where a systematic misbooking hides longest, because
    nobody looks at it -- would never be sampled at all.
    """
    keys = sorted(strata_sizes, key=lambda k: (-strata_sizes[k], str(k)))
    if total <= len(keys):
        return {key: (1 if position < total else 0) for position, key in enumerate(keys)}

    population = sum(strata_sizes.values())
    allocation = {key: 1 for key in keys}
    remaining = total - len(keys)

    # Largest-remainder apportionment over what is left after the floor.
    shares = {
        key: (strata_sizes[key] - 1) / (population - len(keys)) if population > len(keys) else 0
        for key in keys
    }
    exact = {key: shares[key] * remaining for key in keys}
    for key in keys:
        whole = int(exact[key])
        capped = min(whole, strata_sizes[key] - allocation[key])
        allocation[key] += capped
        remaining -= capped

    for key in sorted(keys, key=lambda k: -(exact[k] - int(exact[k]))):
        if remaining <= 0:
            break
        if allocation[key] < strata_sizes[key]:
            allocation[key] += 1
            remaining -= 1

    return allocation


def natural_key(row: SpotCheckRow) -> str:
    """
    Stable identity for a spot-check, so re-applying a review file adds nothing.

    Without this the label carries a NULL `natural_key`, and SQLite permits any
    number of NULLs in a UNIQUE column -- so `ON CONFLICT (natural_key) DO
    NOTHING` never fires and every re-run silently doubles the training set,
    over-weighting whatever was applied twice. Same shape as the trap in
    `ledger.natural_key`.

    Keyed on the transaction, not on the verdict: one spot-check per row. A
    second check of the same row is therefore a no-op rather than a duplicate,
    and `insert_labeled_examples` reports it as unchanged so it is visible.
    """
    identity = row.record.get("transactionid")
    if identity in (None, ""):
        identity = "|".join(
            str(row.record.get(field_name) or "")
            for field_name in ("transaction_date", "amount", "details")
        )
    basis = f"{SPOT_CHECK_SOURCE}|{identity}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _storable(value: object) -> object:
    """
    Reduce a record field to something SQLite will bind.

    Records reach here via `DataFrame.to_dict("records")`, so dates arrive as
    pandas Timestamps and numbers as numpy scalars. Neither binds, and the
    failure surfaces as an opaque "type 'Timestamp' is not supported" at insert
    time rather than anywhere near the cause.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    item = getattr(value, "item", None)  # numpy scalar
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return str(value)


def to_label(row: SpotCheckRow, committee_id: int, actor: str, era: str) -> dict:
    """
    Build the `labeled_examples` row for a completed spot-check.

    `model_committee` is recorded whether or not the human agreed, so agreement
    rate falls out of a single query rather than needing the sample kept around.
    """
    amount = row.amount
    if amount is None:
        amount = parse_amount(row.record.get("amount"))
    return {
        "source": SPOT_CHECK_SOURCE,
        "source_ref": f"spot-check:{row.source}",
        "era": era,
        "natural_key": natural_key(row),
        "transaction_id": _storable(row.record.get("transactionid")),
        "transaction_date": _storable(row.record.get("transaction_date")),
        "amount": float(amount) if amount is not None else None,
        "details": _storable(row.record.get("details")),
        "account": _storable(row.record.get("account")),
        "committee_id": int(committee_id),
        "purpose": _storable(row.record.get("purpose")),
        "model_committee": int(row.committee_id),
        "model_confidence": float(row.confidence),
        "model_source": row.source,
        "labeled_by": actor,
    }


def agreement(labels: list[dict]) -> dict:
    """
    How often the human agreed with the machine, over spot-check labels.

    This is the number the project does not currently have: accuracy on the rows
    the categorizer was confident enough to book without asking.
    """
    checked = [
        label for label in labels
        if label.get("source") == SPOT_CHECK_SOURCE and label.get("model_committee") is not None
    ]
    if not checked:
        return {"checked": 0, "agreed": 0, "rate": 0.0, "by_source": {}}

    by_source: dict[str, dict[str, int]] = {}
    agreed = 0
    for label in checked:
        source = str(label.get("model_source") or "unknown")
        entry = by_source.setdefault(source, {"checked": 0, "agreed": 0})
        entry["checked"] += 1
        if int(label["model_committee"]) == int(label["committee_id"]):
            entry["agreed"] += 1
            agreed += 1

    return {
        "checked": len(checked),
        "agreed": agreed,
        "rate": agreed / len(checked),
        "by_source": by_source,
    }
