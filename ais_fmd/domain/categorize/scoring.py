"""
Module M18 -- weighted evidence scoring.

WHY THIS REPLACES THE HEURISTIC RULE CHAIN.

`predicates.RULES` is first-match-wins, so the *order* of the rules encodes
every tie-break. That broke in practice: the Membership card rule and the
meeting-food rule both fired on the same rows, and picking a winner meant
hand-ordering them from one fact a treasurer happened to mention (cards got
handed out messily, so meeting food has been bought on the wrong card). A rule
chain cannot express "card says Membership, weekday and merchant say Meeting
Food, so I am genuinely unsure" -- it just silently returns whichever rule was
listed first, at full confidence.

Scoring can. Every signal votes for a committee with a weight; the winner is
the highest total, and **confidence falls when signals conflict**. Two strong
opposing signals produce a low-confidence result that routes to the review
queue instead of being quietly booked. That is the behaviour that was actually
wanted: resolve the clear majority automatically, flag the genuine outliers.

CONFIDENCE IS NOT A RULE CONSTANT. In `predicates.py` each rule carries a fixed
number (0.9, 0.85) that never changes regardless of what else is true about the
row. Here confidence is computed per transaction from two things:

  * evidence strength -- how much total weight landed on the winner
  * dominance         -- how decisively it beat the runner-up

so the same rule firing on a clean row and on a contested row yields different
confidence, which is the entire point.

A NOTE ON LEARNING FROM HISTORY. The obvious next step is to learn these
weights from previously-categorized transactions. That is deliberately NOT done
here, because in this database `budget_category` was written by
`categorize_frame` at import time (see `scripts/load_real_statement.py`), not by
a human. Learning from it would train the model on its own output and harden
existing mistakes into "evidence". `CardRegistry` therefore holds only
treasury-confirmed assignments, and any history-derived affinity must be marked
`verified=False` so it can never outweigh a confirmed one. Real ground truth
has to come from a human working the review queue, or from the historical
ledgers in Drive -- not from this table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...config.categories import COMMITTEE_BY_ID, committee_name
from ..money import parse_amount
from .predicates import (
    COMMITTEE_PURPOSE,
    Classification,
    looks_like_bar,
    looks_like_food_merchant,
    weekday_from_details,
)

MEETING_WEEKDAYS = frozenset({"Tuesday", "Wednesday"})

# Labels are tagged with the officer cohort they were decided under, because
# card assignments do not survive a handover. Bump this when the roster turns
# over so old card weights are not fitted against new cardholders.
CURRENT_ERA = "2024-2026"

# Auto-apply at or above this; anything below routes to the review queue with
# its proposal and evidence attached. Tuned against the real statement -- see
# `scripts/tune_scoring.py` for the coverage/√ risk trade-off at other values.
AUTO_APPLY_THRESHOLD = 0.75

# Weight scale: what "one unit" of evidence means.
#   3.0  confirmed by a human or by treasury documentation
#   2.0  strong structural signal (bar merchant, catering-sized food purchase)
#   1.0  ordinary corroboration (food keyword, meeting weekday)
#   0.5  weak hint
W_MERCHANT_MEMORY = 6.0   # a human already decided this exact merchant
W_CARD_VERIFIED = 2.5     # treasury doc names the cardholder's committee
W_CARD_UNVERIFIED = 0.6   # inferred only, must never beat a verified signal
W_BAR_MERCHANT = 2.5
W_FOOD_MERCHANT = 1.5
W_MEETING_WEEKDAY = 2.0
W_CATERING_SIZED = 1.0    # a large food purchase looks like feeding a GBM
W_INCIDENTAL_SIZED = 0.75  # a very small one does not

# Food AND a meeting weekday corroborate each other: either alone is weak, but
# together they are the signature of a GBM food run. Treasury was explicit that
# meeting food is identified by timing and amount rather than by whose card
# paid, so this conjunction has to be able to out-vote a cardholder default --
# see `test_meeting_food_outscores_the_cardholder_default`.
W_MEETING_CONJUNCTION = 2.0

# A food purchase at or above this reads as catering for a meeting rather than
# an incidental snack run.
CATERING_MINIMUM = 75.0
INCIDENTAL_MAXIMUM = 12.0

# Evidence at or above this counts as "fully corroborated"; more does not raise
# confidence further. Keeps a pile-on of weak signals from reading as certainty.
EVIDENCE_SATURATION = 3.0

_CARD_RE = re.compile(r"\bcard\s+(\d{3,5})\b", re.I)


def card_number(details: object) -> str | None:
    """The last-4 of the card used, when the description carries one."""
    if details is None:
        return None
    match = _CARD_RE.search(str(details))
    return match.group(1) if match else None


@dataclass(frozen=True)
class CardAssignment:
    committee_id: int
    holder: str = ""
    verified: bool = False

    @property
    def weight(self) -> float:
        return W_CARD_VERIFIED if self.verified else W_CARD_UNVERIFIED


# Confirmed against treasury's "Categorization Architecture" doc -- the document
# this categorizer was originally specced from. These are the only card
# assignments with a documented owner; every other card in the real statement is
# undocumented and contributes no card signal at all (see HANDOFF P3).
#
# Card 8408 is handled by `rule_consulting` as a certainty and never reaches
# scoring; it is listed here only so the registry is a complete picture.
CONFIRMED_CARDS: dict[str, CardAssignment] = {
    "8313": CardAssignment(5, "Annalee", verified=True),
    "5718": CardAssignment(5, "Grant", verified=True),
    "3568": CardAssignment(4, "Trent", verified=True),
    "8408": CardAssignment(7, "Salena", verified=True),
}


class CardRegistry:
    """
    Card -> committee, with provenance.

    Unverified entries are accepted so history-derived affinities can be tried,
    but they carry a quarter of the weight and are labelled in the evidence, so
    a reviewer can always see whether a suggestion rests on documentation or on
    a guess.
    """

    def __init__(self, assignments: dict[str, CardAssignment] | None = None) -> None:
        self._cards = dict(assignments if assignments is not None else CONFIRMED_CARDS)

    def get(self, card: str | None) -> CardAssignment | None:
        return self._cards.get(card) if card else None

    def with_unverified(self, card: str, committee_id: int, holder: str = "") -> "CardRegistry":
        """A copy with one extra inferred assignment. Never overwrites a confirmed one."""
        if card in self._cards and self._cards[card].verified:
            return self
        merged = dict(self._cards)
        merged[card] = CardAssignment(committee_id, holder, verified=False)
        return CardRegistry(merged)


@dataclass(frozen=True)
class Signal:
    """One piece of evidence voting for one committee."""

    committee_id: int
    weight: float
    reason: str

    def describe(self) -> str:
        return f"{self.reason} (+{self.weight:.2f} → {committee_name(self.committee_id)})"


@dataclass
class ScoredResult:
    """The full picture: what won, by how much, and on what evidence."""

    signals: list[Signal] = field(default_factory=list)
    totals: dict[int, float] = field(default_factory=dict)

    @property
    def ranked(self) -> list[tuple[int, float]]:
        return sorted(self.totals.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def winner(self) -> int | None:
        ranked = self.ranked
        return ranked[0][0] if ranked else None

    @property
    def top_score(self) -> float:
        ranked = self.ranked
        return ranked[0][1] if ranked else 0.0

    @property
    def runner_up_score(self) -> float:
        ranked = self.ranked
        return ranked[1][1] if len(ranked) > 1 else 0.0

    @property
    def dominance(self) -> float:
        """
        How decisively the winner beat the runner-up, in [0.5, 1.0].

        Measured as the winner's margin *relative to its own score*, not as its
        share of the contested total. The share form was tried first and was
        too punishing: a well-corroborated winner (three agreeing signals) that
        happened to attract one weak dissenter fell below the threshold and got
        flagged, even though the evidence was one-sided. Margin-relative keeps
        a decisive win decisive while still collapsing to 0.5 on a true tie --
        which is what pushes a genuine conflict into the review queue.
        """
        if self.top_score <= 0:
            return 0.0
        margin = (self.top_score - self.runner_up_score) / self.top_score
        return 0.5 + 0.5 * margin

    @property
    def evidence_strength(self) -> float:
        """Absolute corroboration, capped so weak signals cannot pile up into certainty."""
        return min(1.0, self.top_score / EVIDENCE_SATURATION)

    @property
    def confidence(self) -> float:
        return round(self.evidence_strength * self.dominance, 4)

    @property
    def is_confident(self) -> bool:
        return self.winner is not None and self.confidence >= AUTO_APPLY_THRESHOLD

    def conflicting(self) -> list[tuple[int, float]]:
        """Runner-up candidates with real support -- what makes this contested."""
        return [(cid, score) for cid, score in self.ranked[1:] if score > 0]

    def explain(self) -> str:
        """One line a treasurer can act on, naming the competing reading."""
        if self.winner is None:
            return "No signal could be read from this transaction."
        parts = [
            f"{committee_name(self.winner)} scored {self.top_score:.2f}"
            f" (confidence {self.confidence:.0%})"
        ]
        contest = self.conflicting()
        if contest:
            rival, score = contest[0]
            parts.append(f"contested by {committee_name(rival)} at {score:.2f}")
        return "; ".join(parts) + "."

    def as_classification(self) -> Classification:
        """The scored outcome in the shape the rest of the pipeline speaks."""
        if self.winner is None:
            from .predicates import UNMATCHED

            return UNMATCHED
        return Classification(
            committee_id=self.winner,
            purpose=COMMITTEE_PURPOSE.get(self.winner, "Misc."),
            rule=f"Scored: {self.explain()}",
            confidence=self.confidence,
            source="scored",
        )


def collect_signals(
    record: dict,
    *,
    cards: CardRegistry | None = None,
    remembered_committee: int | None = None,
) -> list[Signal]:
    """
    Every piece of evidence this transaction offers.

    Deliberately additive and order-independent: unlike a rule chain, adding a
    signal here cannot change what an unrelated signal does.
    """
    registry = cards or CardRegistry()
    details = record.get("details")
    signals: list[Signal] = []

    if remembered_committee is not None:
        signals.append(
            Signal(remembered_committee, W_MERCHANT_MEMORY, "A human mapped this merchant")
        )

    assignment = registry.get(card_number(details))
    if assignment is not None:
        label = "confirmed by treasury doc" if assignment.verified else "inferred, unconfirmed"
        holder = f" ({assignment.holder})" if assignment.holder else ""
        signals.append(
            Signal(
                assignment.committee_id,
                assignment.weight,
                f"Card {card_number(details)}{holder} — {label}",
            )
        )

    is_bar = looks_like_bar(details)
    is_food = looks_like_food_merchant(details)

    if is_bar:
        signals.append(Signal(5, W_BAR_MERCHANT, "Bar or liquor merchant"))

    if is_food and not is_bar:
        signals.append(Signal(8, W_FOOD_MERCHANT, "Food merchant"))

        weekday = weekday_from_details(details, record.get("transaction_date"))
        if weekday in MEETING_WEEKDAYS:
            signals.append(Signal(8, W_MEETING_WEEKDAY, f"Purchased on a {weekday}"))
            signals.append(
                Signal(
                    8,
                    W_MEETING_CONJUNCTION,
                    f"Food on a {weekday} is the GBM signature",
                )
            )

        amount = parse_amount(record.get("amount"))
        if amount is not None:
            magnitude = abs(float(amount))
            if magnitude >= CATERING_MINIMUM:
                signals.append(
                    Signal(8, W_CATERING_SIZED, f"Catering-sized (${magnitude:,.2f})")
                )
            elif magnitude <= INCIDENTAL_MAXIMUM:
                # Too small to be feeding a chapter meeting -- nudges away from 8
                # by supporting the social/incidental reading instead.
                signals.append(
                    Signal(5, W_INCIDENTAL_SIZED, f"Too small for catering (${magnitude:,.2f})")
                )

    return signals


def score(
    record: dict,
    *,
    cards: CardRegistry | None = None,
    remembered_committee: int | None = None,
) -> ScoredResult:
    """Collect evidence and tally it per committee."""
    signals = collect_signals(
        record, cards=cards, remembered_committee=remembered_committee
    )
    totals: dict[int, float] = {}
    for signal in signals:
        if signal.committee_id not in COMMITTEE_BY_ID:
            continue
        totals[signal.committee_id] = totals.get(signal.committee_id, 0.0) + signal.weight
    return ScoredResult(signals=signals, totals=totals)
