"""
Module M19 -- learning from human labels.

Three things live here, in the order they matter:

  1. `fit_weights`    -- replace hand-picked signal weights with measured ones
  2. `calibrate`      -- choose the auto-apply threshold from data, not by guess
  3. `evaluate`       -- the harness that makes "continually improving" checkable

WHY NOT A NEURAL NETWORK. The organisation books ~447 transactions a year across
7 committees in active use, and only ~20% of those need human review -- roughly
100 new labels a year. A tabular classifier over five low-dimensional features
at that volume is a logistic-regression problem, not a deep-learning one; a
network would memorise. The 590 historical labels imported from past treasurers'
ledgers are enough to fit weights, and nowhere near enough to train a net.

Explainability is also a hard requirement, not a preference: a treasurer has to
defend a categorisation to an auditor. Fitted weights can be printed and argued
with. This module deliberately keeps that property -- everything it learns is a
number attached to a named, human-readable signal.

WHAT DOES NOT TRANSFER ACROSS ERAS. Card numbers. The 2022-2023 ledger's cards
belong to officers who have graduated; today's roster is different. Fitting a
card weight across both eras would produce nonsense, so card-derived signals are
scoped by era and excluded from cross-era fitting. Merchant and description
shapes do transfer and are the bulk of the signal.

SELECTION BIAS -- the trap this module cannot fix on its own. If the model
auto-applies confident rows and humans only ever label the uncertain ones, the
label set drifts toward hard cases and the fitted weights get worse on easy
ones. `evaluate` reports the split so the bias is at least visible; the fix is
to spot-check a random sample of auto-applied rows, which is why
`labeled_examples.source` distinguishes 'spot-check' from 'review'.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .scoring import CardRegistry, collect_signals

# Laplace smoothing: a signal seen a handful of times should not read as
# certainty. Higher values pull rare signals harder toward the base rate.
SMOOTHING = 2.0

# Below this many observations a signal is reported but not trusted for fitting.
MIN_OBSERVATIONS = 3


@dataclass
class SignalStat:
    """How often a named signal coincided with the committee it votes for."""

    name: str
    committee_id: int
    fired: int = 0
    agreed: int = 0

    @property
    def precision(self) -> float:
        """Smoothed share of firings where the human agreed with this signal."""
        return (self.agreed + SMOOTHING * 0.5) / (self.fired + SMOOTHING)

    @property
    def trustworthy(self) -> bool:
        return self.fired >= MIN_OBSERVATIONS

    @property
    def learned_weight(self) -> float:
        """
        Precision mapped onto the module's weight scale.

        A signal that is right half the time carries no information about which
        committee is correct, so it lands near zero. One that is always right
        approaches the top of the scale. The log-odds form keeps a very reliable
        signal from being merely twice a coin-flip signal.
        """
        precision = min(max(self.precision, 0.01), 0.99)
        odds = precision / (1 - precision)
        return max(0.0, round(math.log(odds) * 1.5, 3))


@dataclass
class FittedWeights:
    """Learned weights plus everything needed to judge whether to trust them."""

    stats: dict[tuple[str, int], SignalStat] = field(default_factory=dict)
    labels_used: int = 0
    era: str | None = None

    def weight_for(self, name: str, committee_id: int, fallback: float) -> float:
        stat = self.stats.get((name, committee_id))
        if stat is None or not stat.trustworthy:
            return fallback
        return stat.learned_weight

    def table(self) -> list[SignalStat]:
        return sorted(
            self.stats.values(), key=lambda s: (-s.fired, s.name, s.committee_id)
        )

    def summary_line(self) -> str:
        trusted = sum(1 for s in self.stats.values() if s.trustworthy)
        return (
            f"{self.labels_used} labels; {len(self.stats)} distinct signals, "
            f"{trusted} seen at least {MIN_OBSERVATIONS} times."
        )


def _signal_name(reason: str) -> str:
    """
    Collapse a signal's human-readable reason to a stable family name.

    `collect_signals` embeds specifics in its reasons ("Purchased on a Tuesday",
    "Card 5718 (Grant) — confirmed"). Fitting needs the family, not the
    instance, or every card would be its own single-observation signal.
    """
    lowered = reason.lower()
    if lowered.startswith("card "):
        return "card"
    if lowered.startswith("a human mapped"):
        return "merchant-memory"
    if "gbm signature" in lowered:
        return "food-on-meeting-weekday"
    if lowered.startswith("purchased on a"):
        return "meeting-weekday"
    if lowered.startswith("bar or liquor"):
        return "bar-merchant"
    if lowered.startswith("food merchant"):
        return "food-merchant"
    if lowered.startswith("catering-sized"):
        return "catering-sized"
    if lowered.startswith("too small"):
        return "incidental-sized"
    return lowered[:40]


def fit_weights(
    labels: list[dict],
    *,
    cards: CardRegistry | None = None,
    include_card_signals: bool = False,
) -> FittedWeights:
    """
    Measure how often each signal family agrees with the human label.

    `include_card_signals` defaults to False because card assignments do not
    survive an officer handover -- see the module docstring. Turn it on only
    when every label comes from the same era as the registry.
    """
    fitted = FittedWeights()
    registry = cards or CardRegistry()

    for label in labels:
        committee_id = label.get("committee_id")
        if committee_id is None:
            continue
        fitted.labels_used += 1

        record = {
            "details": label.get("details"),
            "amount": label.get("amount"),
            "transaction_date": label.get("transaction_date"),
            "account": label.get("account"),
        }
        for signal in collect_signals(record, cards=registry):
            name = _signal_name(signal.reason)
            if name == "card" and not include_card_signals:
                continue
            key = (name, signal.committee_id)
            stat = fitted.stats.get(key)
            if stat is None:
                stat = SignalStat(name=name, committee_id=signal.committee_id)
                fitted.stats[key] = stat
            stat.fired += 1
            if signal.committee_id == int(committee_id):
                stat.agreed += 1

    return fitted


@dataclass
class ThresholdPoint:
    """Precision and coverage if the auto-apply bar were set here."""

    threshold: float
    applied: int
    correct: int
    total: int

    @property
    def precision(self) -> float:
        return self.correct / self.applied if self.applied else 0.0

    @property
    def coverage(self) -> float:
        return self.applied / self.total if self.total else 0.0


# Committee pairs where a "mistake" is really the chart of accounts changing
# under the labels, not the model being wrong.
#
# Found by reading the rows rather than assuming: 35 examples labelled
# Membership in 2023 have descriptions like "AIS FORMAL 2023", "FORMAL PARTY",
# "FORMAL FEE FOR ...". Committee 18 (Formal) did not exist then -- it is absent
# from the Fall 2022 and Spring 2023 budget lists in the same workbook -- so
# formal ticket income was booked to Membership. Today's rule books it to
# Formal, which is correct now and counts as an error against a 2023 label.
#
# Scoring these as failures would push someone to "fix" a rule that is right.
KNOWN_TAXONOMY_DRIFT: dict[tuple[int, int], str] = {
    (18, 5): "Committee 18 (Formal) post-dates these labels; formal income was booked to Membership.",
    (17, 3): "Outgoing transfers were booked to Transfers before a Refunded bucket existed.",
}


@dataclass
class Evaluation:
    """What the scorer got right, and where it is being asked to guess."""

    points: list[ThresholdPoint] = field(default_factory=list)
    by_committee: dict[int, tuple[int, int]] = field(default_factory=dict)
    unscored: int = 0
    total: int = 0
    source_mix: dict[str, int] = field(default_factory=dict)
    drift: int = 0  # mismatches attributable to a changed chart of accounts

    def at(self, threshold: float) -> ThresholdPoint | None:
        for point in self.points:
            if abs(point.threshold - threshold) < 1e-9:
                return point
        return None

    def recommend(self, minimum_precision: float = 0.95) -> ThresholdPoint | None:
        """
        The lowest bar that still meets the precision floor.

        Lowest, not highest: among thresholds that are accurate enough, the one
        that auto-applies the most is the one that saves the most work.
        """
        eligible = [p for p in self.points if p.applied and p.precision >= minimum_precision]
        return min(eligible, key=lambda p: p.threshold) if eligible else None

    def selection_bias_warning(self) -> str:
        """
        Flag a label set drawn only from rows the model found hard.

        Fitting on those alone teaches the model the world is harder than it is
        and degrades it on the easy majority.
        """
        review = self.source_mix.get("review", 0)
        spot = self.source_mix.get("spot-check", 0)
        if review and not spot:
            return (
                f"All {review} queue-sourced labels came from rows the model "
                f"flagged. None are spot-checks of auto-applied rows, so accuracy "
                f"on the confident majority is unmeasured."
            )
        return ""


def _as_record(label: dict) -> dict:
    return {
        "details": label.get("details"),
        "amount": label.get("amount"),
        "transaction_date": label.get("transaction_date"),
        "account": label.get("account"),
    }


def predict(
    labels: list[dict], *, cards: CardRegistry | None = None
) -> list[tuple[int | None, float, str]]:
    """
    Run the **whole pipeline** over labeled rows: (committee, confidence, source).

    Evaluating `score()` alone was the first version of this and it was
    misleading: it reported that 91% of the historical ledger produced "no
    signal", when in truth those rows are dues, transfers and refunds that the
    exact rules resolve before scoring is ever consulted. Measuring one tier of
    a four-tier pipeline says nothing about the pipeline.
    """
    from .pipeline import categorize_records

    run = categorize_records([_as_record(label) for label in labels], cards=cards)
    out: list[tuple[int | None, float, str]] = []
    for index, classification in enumerate(run.classifications):
        if classification.is_assigned:
            out.append((classification.committee_id, classification.confidence, classification.source))
            continue
        # Held back by the gate, but it still has an opinion worth measuring.
        proposal = run.proposals.get(index)
        if proposal is not None and proposal.winner is not None:
            out.append((proposal.winner, proposal.confidence, "proposed"))
        else:
            out.append((None, 0.0, "none"))
    return out


def evaluate(
    labels: list[dict],
    *,
    cards: CardRegistry | None = None,
    thresholds: tuple[float, ...] = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95),
) -> Evaluation:
    """
    Run the pipeline over every labeled example and compare against the human.

    This is the harness that makes "is it getting better?" a question with an
    answer. Without it, a change to the weights is an opinion.
    """
    result = Evaluation(total=len(labels))
    counts = {t: [0, 0] for t in thresholds}
    per_committee: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    predictions = predict(labels, cards=cards)

    for label, (predicted, confidence, _source) in zip(labels, predictions):
        result.source_mix[str(label.get("source") or "unknown")] = (
            result.source_mix.get(str(label.get("source") or "unknown"), 0) + 1
        )
        truth = label.get("committee_id")
        if truth is None:
            continue
        truth = int(truth)

        if predicted is None:
            result.unscored += 1
            continue

        # A prediction that is correct under today's chart of accounts but
        # disagrees with a label written under an older one is not an error, and
        # counting it as one would argue for breaking a working rule.
        if (predicted, truth) in KNOWN_TAXONOMY_DRIFT:
            result.drift += 1
            continue

        per_committee[truth][1] += 1
        if predicted == truth:
            per_committee[truth][0] += 1

        for threshold in thresholds:
            if confidence >= threshold:
                counts[threshold][0] += 1
                if predicted == truth:
                    counts[threshold][1] += 1

    result.points = [
        ThresholdPoint(
            threshold=t, applied=counts[t][0], correct=counts[t][1], total=len(labels)
        )
        for t in thresholds
    ]
    result.by_committee = {cid: (v[0], v[1]) for cid, v in per_committee.items()}
    return result


def confusion(labels: list[dict], *, cards: CardRegistry | None = None) -> Counter:
    """(predicted, actual) pairs the pipeline gets wrong -- what to fix next."""
    wrong: Counter = Counter()
    for label, (predicted, _confidence, _source) in zip(
        labels, predict(labels, cards=cards)
    ):
        truth = label.get("committee_id")
        if truth is None or predicted is None:
            continue
        if predicted != int(truth) and (predicted, int(truth)) not in KNOWN_TAXONOMY_DRIFT:
            wrong[(predicted, int(truth))] += 1
    return wrong
