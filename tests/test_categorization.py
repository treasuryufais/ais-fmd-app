"""
Categorization rules and pipeline.

These are the highest-value tests in the repository: pure functions, no mocking,
and they encode the business rules that decide where money is booked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.domain.categorize.merchants import MerchantMemory, merchant_key
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.predicates import (
    DuesSchedule,
    DuesWindow,
    classify_deterministic,
    classify_exact,
    extract_purchase_date,
    looks_like_bar,
    looks_like_food_merchant,
    weekday_from_details,
)


def wells(merchant: str, purchase: str = "09/16", card: str = "1234") -> str:
    return f"PURCHASE AUTHORIZED ON {purchase} {merchant} GAINESVILLE FL S3045 CARD {card}"


def venmo(note: str, sender: str = "Ava Mitchell") -> str:
    return f"3421987654321098 | {note} | {sender} | UF AIS"


# --- Field extraction --------------------------------------------------------

def test_extract_purchase_date():
    assert extract_purchase_date(wells("PUBLIX", "09/16")) == "09/16"
    assert extract_purchase_date("no marker here") == ""
    assert extract_purchase_date(None) == ""


def test_weekday_uses_the_embedded_purchase_date_not_the_posting_date():
    """
    A Tuesday grocery run often posts on Thursday. The rule must read the date
    embedded in the description, which is why the original went looking for it.
    """
    # 2025-09-16 was a Tuesday; posting date deliberately differs.
    details = wells("PUBLIX", "09/16")
    assert weekday_from_details(details, "2025-09-18") == "Tuesday"


def test_weekday_is_unknown_without_an_embedded_date():
    assert weekday_from_details("DEPOSIT", "2025-09-18") == "Unknown"


# --- Merchant classification -------------------------------------------------

@pytest.mark.parametrize("merchant", ["PUBLIX", "CHIPOTLE", "PIESANOS", "HANA SUSHI"])
def test_food_merchants_recognised(merchant):
    assert looks_like_food_merchant(wells(merchant))


@pytest.mark.parametrize("merchant", ["MACDINTONS PUB", "SALTY DOG SALOON", "TOTAL WINE"])
def test_bar_merchants_recognised(merchant):
    assert looks_like_bar(wells(merchant))


def test_grocery_stores_are_never_read_as_bars():
    """'PUBLIX ... BAR HARBOR' must not trip the generic bar keyword."""
    assert not looks_like_bar(wells("PUBLIX SUPER MARKET"))
    assert not looks_like_bar(wells("WALMART SUPERCENTER"))


# --- Rules -------------------------------------------------------------------

def test_outgoing_transfer_is_no_longer_booked_to_refunded():
    """
    Treasury's ruling: a reimbursement is the committee's expenditure.

    The old rule booked every negative Venmo/Zelle to 17 (Refunded), which is
    `kind="ledger"` and therefore outside budget-vs-actual -- so reimbursed
    spending never counted against the budget of the committee that spent it.
    With no committee named, the row is now a question rather than an answer.
    """
    result = classify_deterministic(
        {"amount": -45.0, "details": venmo("Reimbursement"), "account": "Venmo"}
    )
    assert result.committee_id is None
    assert "committee" in result.rule.lower(), "the queue needs the reason in words"


def test_outgoing_transfer_takes_the_committee_its_memo_names():
    """The memo is what makes a reimbursement bookable."""
    result = classify_deterministic(
        {"amount": -120.0, "details": venmo("reimbursing hoodie order"), "account": "Venmo"}
    )
    assert result.committee_id == 13
    assert result.purpose == "Merch"


def test_dues_rule_matches_exact_amounts_only():
    """
    The amount rule on its own, with no memo to help it.

    The memo deliberately says nothing about dues: `rule_dues_memo` would
    otherwise catch the off-rate row and this would stop testing the amount
    matching it exists to test. That the memo rule *does* catch it is asserted
    in `test_treasury_decisions.py`.
    """
    dues = {"amount": 35.00, "details": venmo("payment"), "account": "Venmo"}
    assert classify_deterministic(dues).committee_id == 1

    not_dues = {**dues, "amount": 36.00}
    assert classify_deterministic(not_dues).committee_id != 1


def test_formal_memo_books_formal_in_both_directions():
    """
    A formal memo means Formal whichever way the money moved.

    Previously the negative case was caught by `rule_refund` and booked to
    Refunded, so reimbursing someone who fronted a formal cost dropped out of
    the Formal budget. Both directions are now the same committee.
    """
    formal = {"amount": 55.0, "details": venmo("Spring formal ticket"), "account": "Venmo"}
    assert classify_deterministic(formal).committee_id == 18

    negative = {**formal, "amount": -55.0}
    assert classify_deterministic(negative).committee_id == 18


def test_consulting_card_rule():
    result = classify_deterministic(
        {"amount": -120.0, "details": wells("ZOOM.US", card="8408"), "account": "Wells Fargo"}
    )
    assert result.committee_id == 7


def test_membership_officer_card_rule():
    """
    Card 8313 (Annalee) and card 5718 (Grant) are Membership by cardholder
    assignment -- confirmed against treasury's "Categorization Architecture"
    doc. A grocery run on that card is Membership even though nothing about
    the merchant says so, the same way "card 8408" alone settles Consulting.
    """
    for card in ("8313", "5718"):
        result = classify_deterministic(
            {"amount": -20.58, "details": wells("PUBLIX GAINESVILLE", "10/26", card=card),
             "account": "Wells Fargo"}
        )
        assert result.committee_id == 5, f"card {card} should resolve to Membership"
        assert result.source == "rule"


def test_membership_card_rule_does_not_fire_on_other_cards():
    result = classify_deterministic(
        {"amount": -20.58, "details": wells("PUBLIX GAINESVILLE", "10/26", card="1113"),
         "account": "Wells Fargo"}
    )
    assert result.committee_id is None


def test_meeting_food_wins_over_officer_card_default():
    """
    REGRESSION. Card 5718/8313 is Membership by default, but treasury reported
    that meeting food has repeatedly been bought on the wrong person's card
    due to card-issuance problems. A Tuesday/Wednesday food purchase on the
    Membership card must stay Meeting Food, not flip to Membership.
    """
    record = {
        "amount": -159.80,
        "details": wells("PUBLIX SUPER MAR", "09/16", card="5718"),  # a Tuesday
        "account": "Wells Fargo",
        "transaction_date": "2025-09-18",
    }
    result = classify_deterministic(record)
    assert result.committee_id == 8, "meeting-food timing must outrank the card default"


def test_officer_card_default_still_fires_off_meeting_weekdays():
    """The other half: outside the meeting-food window, the card default holds."""
    record = {
        "amount": -20.58,
        "details": wells("PUBLIX GAINESVILLE", "10/26", card="5718"),  # a Saturday
        "account": "Wells Fargo",
        "transaction_date": "2025-10-28",
    }
    result = classify_deterministic(record)
    assert result.committee_id == 5


def test_meeting_food_runs_locally_without_a_model():
    """
    The original delegated this to the language model even though everything
    needed to decide it was local. Running it in Python is what lets the
    pipeline stop sending most rows to a model.
    """
    result = classify_deterministic(
        {
            "amount": -142.30,
            "details": wells("PUBLIX SUPER MAR", "09/16"),  # Tuesday
            "transaction_date": "2025-09-18",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id == 8
    assert result.source == "rule"


def test_meeting_food_does_not_fire_on_a_non_meeting_weekday():
    result = classify_deterministic(
        {
            "amount": -142.30,
            "details": wells("PUBLIX SUPER MAR", "09/19"),  # Friday
            "transaction_date": "2025-09-19",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id != 8


def test_bar_on_a_tuesday_is_membership_not_meeting_food():
    result = classify_deterministic(
        {
            "amount": -210.00,
            "details": wells("MACDINTONS PUB", "09/16"),
            "transaction_date": "2025-09-16",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id == 5


def test_memo_outranks_the_dues_amount():
    """
    The collision treasury warned about, asserted.

    From Fall 2026 dues are $50/$65 and formal payments land near them. A $50
    incoming transfer memoed "formal" matches the dues rate exactly, so the two
    rules genuinely both fire -- and the memo has to win, or formal ticket
    income is booked as dues.
    """
    schedule = DuesSchedule(
        [
            DuesWindow(
                term_id="FA26",
                semester="Fall 2026",
                start=date(2026, 8, 15),
                end=date(2026, 12, 18),
                rates=(Decimal("50.00"), Decimal("65.00")),
                verified=True,
            )
        ]
    )
    row = {
        "amount": 50.00,
        "details": venmo("formal ticket"),
        "account": "Venmo",
        "transaction_date": "2026-09-10",
    }
    assert classify_exact(row, dues=schedule).committee_id == 18

    # Same amount, same term, no memo: that one really is dues.
    plain = {**row, "details": venmo("")}
    assert classify_exact(plain, dues=schedule).committee_id == 1


# --- Merchant memory (M4) ----------------------------------------------------

def test_merchant_key_collapses_transaction_noise():
    first = merchant_key(wells("PUBLIX SUPER MAR", "09/16"))
    second = merchant_key(wells("PUBLIX SUPER MAR", "11/02"))
    assert first == second != ""


def test_merchant_key_strips_store_numbers():
    assert merchant_key(wells("CHIPOTLE 1462")) == merchant_key(wells("CHIPOTLE 1893"))


def test_merchant_key_skips_venmo_transfers():
    """Venmo details are person-specific -- learning them would be useless."""
    assert merchant_key(venmo("Fall dues", "Ava Mitchell")) == ""
    assert merchant_key(venmo("Fall dues", "Noah Patel")) == ""


def test_merchant_memory_round_trip():
    memory = MerchantMemory()
    details = wells("SWEETWATER BRANCH INN")
    assert memory.lookup(details) is None

    memory.remember(details, committee_id=14, purpose="Road Trip")
    found = memory.lookup(details)
    assert found is not None
    assert found.committee_id == 14
    assert found.source == "merchant"


def test_merchant_memory_generalises_to_new_transactions_of_the_same_merchant():
    memory = MerchantMemory()
    memory.remember(wells("SWEETWATER BRANCH INN", "01/04"), 14, "Road Trip")
    found = memory.lookup(wells("SWEETWATER BRANCH INN", "07/22"))
    assert found is not None and found.committee_id == 14


# --- Pipeline ordering -------------------------------------------------------

def test_pipeline_resolves_locally_and_sends_only_the_residual():
    records = [
        {"amount": 35.00, "details": venmo("dues"), "account": "Venmo"},
        {"amount": -120.0, "details": wells("ZOOM.US", card="8408"), "account": "Wells Fargo"},
        {"amount": -60.0, "details": wells("TOTALLY UNKNOWN VENDOR"), "account": "Wells Fargo"},
    ]
    run = categorize_records(records)
    assert run.rows_resolved_locally == 2
    assert run.rows_sent_to_model == 1
    # No model call happens in sandbox, so the residual stays unassigned.
    assert run.counts_by_source["none"] == 1


def test_merchant_memory_is_consulted_before_rules():
    """Memory is cheapest, so it must come first."""
    details = wells("PUBLIX SUPER MAR", "09/16")
    memory = MerchantMemory()
    memory.remember(details, committee_id=13, purpose="Merch")  # deliberately odd

    run = categorize_records(
        [{"amount": -50.0, "details": details, "transaction_date": "2025-09-16", "account": "Wells Fargo"}],
        memory,
    )
    assert run.classifications[0].committee_id == 13
    assert run.classifications[0].source == "merchant"


def test_pipeline_on_empty_input():
    run = categorize_records([])
    assert run.total == 0
    assert run.coverage == 0.0
