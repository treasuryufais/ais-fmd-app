"""
Weighted evidence scoring (M18) and the confidence gate.

The tests that matter most are the *conflict* ones. The whole reason scoring
replaced the heuristic rule chain is that a chain cannot represent "two signals
disagree, so I am unsure" -- it returns whichever rule was listed first, at full
confidence. These assert that disagreement lowers confidence and routes the row
to a human instead.
"""

from __future__ import annotations

import pytest

from ais_fmd.domain.categorize.merchants import MerchantMemory, MerchantRule
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.scoring import (
    AUTO_APPLY_THRESHOLD,
    CardAssignment,
    CardRegistry,
    card_number,
    score,
)


def wells(merchant: str, purchase: str = "09/16", card: str = "1234") -> str:
    """A realistic Wells Fargo line, with the run-of-spaces padding real ones have."""
    return (
        f"PURCHASE                      AUTHORIZED ON   {purchase} {merchant}"
        f"              GAINESVILLE   FL  S304257782821355   CARD {card}"
    )


def record(details: str, amount: float, date: str = "2025-09-18") -> dict:
    return {"details": details, "amount": amount, "transaction_date": date, "account": "Wells Fargo"}


# --- Card extraction ---------------------------------------------------------

def test_card_number_reads_the_last_four():
    assert card_number(wells("PUBLIX", card="5718")) == "5718"


def test_card_number_absent_is_none():
    assert card_number("ZELLE FROM JANE DOE ON 07/08 REF # ABC123") is None


# --- The conflict treasury actually reported ---------------------------------

def test_meeting_food_outscores_the_cardholder_default():
    """
    Treasury's rule, in their words: meeting food is identified by timing and
    amount, not by whose card paid -- cards got handed out messily enough that
    GBM food has been bought on the wrong one. Food on a Tuesday must therefore
    beat the Membership cardholder default, and beat it confidently enough to
    auto-apply.
    """
    result = score(
        record(wells("PUBLIX #1560", "09/16", card="5718"), -159.80),  # 09/16 is a Tuesday
        cards=CardRegistry(),
    )
    assert result.winner == 8, "timing + food merchant must outrank the card"
    assert result.is_confident, f"expected auto-apply, got {result.confidence}"


def test_cardholder_default_holds_outside_the_meeting_window():
    """The other half: with no meeting-food signal, the card is the best evidence."""
    result = score(
        record(wells("BUSCH GARDENS", "10/14", card="5718"), -485.27, "2025-10-16"),
        cards=CardRegistry(),
    )
    assert result.winner == 5
    assert result.is_confident


def test_a_genuine_conflict_is_flagged_rather_than_guessed():
    """
    A bar charge on the President's card. Membership (bar merchant) and
    President (cardholder) are both defensible and neither dominates, so this
    must land in the review queue -- not be silently booked to whichever rule
    happened to run first.
    """
    result = score(
        record(wells("MACDINTONS", "10/02", card="3568"), -120.00),
        cards=CardRegistry(),
    )
    assert not result.is_confident
    assert result.conflicting(), "a contested result must name its rival"


def test_conflict_is_visible_in_the_explanation():
    result = score(record(wells("MACDINTONS", "10/02", card="3568"), -120.00), cards=CardRegistry())
    explanation = result.explain()
    assert "contested by" in explanation
    assert "%" in explanation, "a treasurer needs the confidence figure, not just a label"


# --- Confidence behaviour ----------------------------------------------------

def test_a_tie_collapses_confidence():
    """Two exactly balanced readings must not clear the gate."""
    registry = CardRegistry({"9999": CardAssignment(13, "Someone", verified=True)})
    # Bar merchant (→5, 2.5) against a verified card (→13, 2.5): dead even.
    result = score(record(wells("MACDINTONS", "10/02", card="9999"), -80.00), cards=registry)
    assert result.dominance == pytest.approx(0.5)
    assert not result.is_confident


def test_uncorroborated_weak_evidence_does_not_clear_the_gate():
    """A lone food-merchant keyword is not enough to book money on."""
    result = score(
        record(wells("PUBLIX #1560", "09/20", card="1113"), -40.00, "2025-09-22"),  # Saturday
        cards=CardRegistry(),
    )
    assert not result.is_confident


def test_no_signal_yields_no_winner():
    result = score(record(wells("SOMEPLACE ODD", "10/14", card="1113"), -50.00), cards=CardRegistry())
    assert result.winner is None
    assert result.confidence == 0.0
    assert not result.is_confident


def test_confidence_never_exceeds_one():
    """Piling on agreeing signals must saturate, not run away."""
    stacked = score(
        record(wells("PUBLIX #1560", "09/16", card="5718"), -500.00),
        cards=CardRegistry(),
    )
    assert 0.0 <= stacked.confidence <= 1.0


# --- Provenance: confirmed beats inferred ------------------------------------

def test_an_unverified_card_cannot_outweigh_a_verified_one():
    """
    History-derived card affinities are inferred from this database's own
    labels, which were themselves written by the categorizer. They must never
    carry the authority of treasury's documented assignments.
    """
    registry = CardRegistry().with_unverified("7193", 8, "unknown")
    verified = score(record(wells("SOMEWHERE", "10/14", card="5718"), -50.00), cards=registry)
    inferred = score(record(wells("SOMEWHERE", "10/14", card="7193"), -50.00), cards=registry)
    assert verified.top_score > inferred.top_score
    assert verified.is_confident
    assert not inferred.is_confident, "an inferred card alone must not auto-apply"


def test_with_unverified_never_overwrites_a_confirmed_assignment():
    registry = CardRegistry().with_unverified("5718", 99, "bogus")
    assignment = registry.get("5718")
    assert assignment.committee_id == 5, "treasury's documented owner must win"
    assert assignment.verified


def test_human_merchant_mapping_outranks_the_card_default():
    """
    A treasurer's explicit decision is the strongest evidence available, so the
    pipeline short-circuits on it rather than letting scoring weigh it. As a
    weighted signal it tied against a full meeting-food reading and got itself
    flagged -- sending a row a human had already ruled on back to their queue.
    """
    memory = MerchantMemory(
        [
            MerchantRule(
                key="somewhere gainesville",
                canonical_name="Somewhere",
                committee_id=13,
                purpose="Merch",
            )
        ]
    )
    run = categorize_records([record(wells("SOMEWHERE", "10/14", card="5718"), -50.00)], memory)
    assert run.classifications[0].committee_id == 13
    assert run.classifications[0].source == "merchant"


# --- The gate, end to end ----------------------------------------------------

def test_low_confidence_rows_are_held_back_but_keep_their_proposal():
    """
    The behaviour that was asked for: don't book the uncertain ones, but don't
    throw the reasoning away either -- the queue should show its work.
    """
    rows = [record(wells("MACDINTONS", "10/02", card="3568"), -120.00)]
    run = categorize_records(rows, MerchantMemory())

    assert not run.classifications[0].is_assigned, "a contested row must not be booked"
    assert 0 in run.held_for_review
    assert run.held_for_review[0].winner is not None, "the proposal must survive"


def test_confident_rows_are_applied_and_carry_their_confidence():
    rows = [record(wells("PUBLIX #1560", "09/16", card="5718"), -159.80)]
    run = categorize_records(rows, MerchantMemory())

    assert run.classifications[0].committee_id == 8
    assert run.classifications[0].source == "scored"
    assert run.classifications[0].confidence >= AUTO_APPLY_THRESHOLD


def test_threshold_is_tunable_per_run():
    """Raising the bar must move rows into review, not change what wins."""
    rows = [record(wells("BUSCH GARDENS", "10/14", card="5718"), -485.27, "2025-10-16")]
    lenient = categorize_records(rows, MerchantMemory(), threshold=0.5)
    strict = categorize_records(rows, MerchantMemory(), threshold=0.99)
    assert lenient.classifications[0].is_assigned
    assert not strict.classifications[0].is_assigned


# --- Exact rules still short-circuit -----------------------------------------

def test_exact_rules_are_never_outvoted_by_scoring():
    """
    Card 8408 is a certainty per the spec ("ignore all remaining rules"), and
    dues/refund amounts are unambiguous. Scoring must not get a say.
    """
    consulting = record(wells("PUBLIX #1560", "09/16", card="8408"), -20.00)
    run = categorize_records([consulting], MerchantMemory())
    assert run.classifications[0].committee_id == 7
    assert run.classifications[0].source == "rule"


def test_dues_still_resolve_exactly():
    dues = {
        "details": "ZELLE FROM JANE DOE ON 07/08 REF # A1",
        "amount": 35.00,
        "transaction_date": "2025-09-18",
        "account": "Wells Fargo",
    }
    run = categorize_records([dues], MerchantMemory())
    assert run.classifications[0].committee_id == 1


def test_merchant_memory_still_beats_a_heuristic_reading():
    """
    Note the key: `merchant_key` reduces the full description to
    "publix gainesville", so a rule keyed on bare "publix" would never match.
    Getting this wrong in an earlier draft of this test made merchant memory
    look broken when it was the lookup that missed.
    """
    memory = MerchantMemory(
        [
            MerchantRule(
                key="publix gainesville",
                canonical_name="Publix",
                committee_id=13,
                purpose="Merch",
            )
        ]
    )
    rows = [record(wells("PUBLIX", "09/16", card="1113"), -20.00)]
    run = categorize_records(rows, memory)
    assert run.classifications[0].committee_id == 13
