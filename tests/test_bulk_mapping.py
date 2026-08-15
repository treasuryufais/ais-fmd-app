"""
Bulk merchant mapping (M4b / P3) and the rule-ordering guarantee it exposed.

The ordering tests matter most. Bulk mapping introduces a hazard the row-by-row
review queue does not have: one confirmation writes a rule that applies to every
row sharing that merchant, including rows a deterministic rule already handles
correctly. Real data made it concrete -- two Publix purchases were on the
consulting card.
"""

from __future__ import annotations

import csv
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from ais_fmd.domain.categorize import bulk
from ais_fmd.domain.categorize.merchants import MerchantMemory, MerchantRule
from ais_fmd.domain.categorize.pipeline import categorize_records

ROOT = Path(__file__).resolve().parent.parent


def record(details: str, amount: float, date: str = "2025-09-17") -> dict:
    return {
        "details": details,
        "amount": amount,
        "transaction_date": date,
        "account": "Wells Fargo",
    }


# --- Grouping ----------------------------------------------------------------

def test_store_number_variants_collapse_into_one_decision():
    """The whole point: one decision, not one per store."""
    rows = [
        record("PURCHASE AUTHORIZED ON 09/17 CHIPOTLE 1462 GAINESVILLE FL", -42.10),
        record("PURCHASE AUTHORIZED ON 10/02 CHIPOTLE 1893 GAINESVILLE FL", -38.55),
        record("PURCHASE AUTHORIZED ON 10/09 CHIPOTLE 1462 GAINESVILLE FL", -51.00),
    ]
    groups = bulk.group_residual(rows)
    assert len(groups) == 1
    assert groups[0].row_count == 3


def test_display_name_is_the_merchant_not_the_statement_prefix():
    """
    Every raw row starts "PURCHASE AUTHORIZED ON <date>", which identifies
    nothing. The label must be the merchant.
    """
    groups = bulk.group_residual(
        [record("PURCHASE AUTHORIZED ON 09/13 PUBLIX #1234 GAINESVILLE FL", -27.31)]
    )
    assert "publix" in groups[0].display_name.lower()
    assert "authorized" not in groups[0].display_name.lower()


def test_member_transfers_never_become_merchant_groups():
    """A rule keyed on a member's name would mis-categorise everything they pay."""
    rows = [record("ZELLE FROM JANE DOE ON 07/08 REF # ABC123", 20.0)]
    groups = bulk.group_residual(rows)
    assert all(group.kind != bulk.KIND_MERCHANT for group in groups)


# --- Memo extraction ---------------------------------------------------------

@pytest.mark.parametrize(
    "details,expected",
    [
        ("ZELLE FROM GILES GREENE ON 09/20 REF # BACQZL9WFMD1 GILES GREENE HEADSHOT", "headshot"),
        ("ZELLE FROM SEB ON 11/01 REF # BACBGEVAAPVX HOODIE  TSHIRT  SEBASTIA", "merch"),
        ("ZELLE FROM ANON ON 09/20 REF # BACFAPGQWS2V", ""),
    ],
)
def test_memo_theme_reads_what_the_payment_was_for(details, expected):
    """
    These 120 rows were invisible to merchant memory by design, and nothing
    else was reading the memo the member typed.
    """
    assert bulk.memo_theme(details) == expected


def test_memo_keywords_survive_run_together_words():
    """Real memos concatenate: 'PEI CHI WANGHEADSHOT'."""
    assert bulk.memo_theme("ZELLE FROM X ON 09/20 REF # AB1 PEI CHI WANGHEADSHOT") == "headshot"


# --- Proposals ---------------------------------------------------------------

def test_bar_merchant_proposes_membership():
    groups = bulk.group_residual(
        [record("PURCHASE AUTHORIZED ON 09/17 MACDINTONS GAINESVILLE FL", -88.00)]
    )
    assert bulk.propose(groups[0]).committee_id == 5


def test_food_on_meeting_weekdays_proposes_meeting_food():
    # 2025-09-16 and 2025-09-17 are a Tuesday and a Wednesday.
    rows = [
        record("PURCHASE AUTHORIZED ON 09/16 PUBLIX GAINESVILLE FL", -40.0, "2025-09-18"),
        record("PURCHASE AUTHORIZED ON 09/17 PUBLIX GAINESVILLE FL", -35.0, "2025-09-19"),
    ]
    proposal = bulk.propose(bulk.group_residual(rows)[0])
    assert proposal.committee_id == 8
    assert not proposal.needs_decision


def test_food_off_meeting_weekdays_refuses_to_guess():
    """
    The honest outcome. A Saturday Publix run is not meeting food, and which
    line it belongs to is a decision about real money.
    """
    rows = [
        record("PURCHASE AUTHORIZED ON 09/20 PUBLIX GAINESVILLE FL", -40.0, "2025-09-22"),
        record("PURCHASE AUTHORIZED ON 09/21 PUBLIX GAINESVILLE FL", -35.0, "2025-09-23"),
    ]
    proposal = bulk.propose(bulk.group_residual(rows)[0])
    assert proposal.committee_id is None
    assert proposal.needs_decision


def test_bank_deposits_are_never_auto_assigned():
    """$15,400 of deposits must not be booked by a keyword."""
    rows = [record("MOBILE DEPOSIT : REF NUMBER :408071", 1500.0) for _ in range(3)]
    proposal = bulk.propose(bulk.group_residual(rows)[0])
    assert proposal.committee_id is None
    assert proposal.needs_decision


def test_returns_propose_refunded():
    rows = [record("PURCHASE RETURN AUTHORIZED ON 04/12 AMAZON.COM", 37.39)]
    assert bulk.propose(bulk.group_residual(rows)[0]).committee_id == 17


def test_proposal_carries_row_count_and_total():
    rows = [
        record("PURCHASE AUTHORIZED ON 09/16 PUBLIX GAINESVILLE FL", -40.0),
        record("PURCHASE AUTHORIZED ON 09/17 PUBLIX GAINESVILLE FL", -35.0),
    ]
    proposal = bulk.propose(bulk.group_residual(rows)[0])
    assert proposal.row_count == 2
    assert proposal.total_amount == Decimal("-75.00")


# --- Override detection ------------------------------------------------------

def test_report_warns_when_a_mapping_would_rebook_classified_rows():
    """
    The hazard bulk mapping introduces and the review queue does not.

    One Publix row is on the consulting card, so a rule already books it to
    Consulting. Mapping the merchant to Meeting Food would silently move it.
    """
    classified = record("PURCHASE AUTHORIZED ON 09/16 PUBLIX CARD 8408 GAINESVILLE FL", -20.0)
    residual = [record("PURCHASE AUTHORIZED ON 09/20 PUBLIX GAINESVILLE FL", -40.0, "2025-09-22")]

    report = bulk.build_report(
        residual,
        all_records=[classified, *residual],
        committee_ids=[7, None],
    )
    proposal = report.proposals[0]
    assert proposal.already_classified == {7: 1}

    from dataclasses import replace

    as_food = replace(proposal, committee_id=8, confidence=0.9)
    assert as_food.override_count == 1
    assert "Consulting" in as_food.override_warning()


def test_no_warning_when_the_mapping_agrees_with_existing_classifications():
    rows = [record("PURCHASE AUTHORIZED ON 09/20 PUBLIX GAINESVILLE FL", -40.0)]
    report = bulk.build_report(rows, all_records=rows, committee_ids=[8])
    from dataclasses import replace

    proposal = replace(report.proposals[0], committee_id=8)
    assert proposal.override_count == 0
    assert proposal.override_warning() == ""


# --- Rule ordering (the fix bulk mapping forced) ------------------------------

def test_exact_rules_outrank_merchant_memory():
    """
    REGRESSION. Merchant memory used to run ahead of every rule, so a learned
    mapping could override `rule_consulting` -- which the spec says wins
    outright: "card 8408 ... categorize immediately. Ignore all remaining rules."
    """
    memory = MerchantMemory(
        [MerchantRule(key="publix", canonical_name="Publix", committee_id=8, purpose="Meeting Food")]
    )
    rows = [record("PURCHASE AUTHORIZED ON 09/16 PUBLIX CARD 8408 GAINESVILLE FL", -20.0)]
    run = categorize_records(rows, memory)
    assert run.classifications[0].committee_id == 7, "merchant memory overrode the card rule"
    assert run.classifications[0].source == "rule"


def test_merchant_memory_still_outranks_heuristic_rules():
    """
    The other half. A confirmed mapping for a specific merchant should beat a
    keyword guess -- that is the entire value of learning it.
    """
    memory = MerchantMemory(
        [MerchantRule(key="publix gainesville", canonical_name="Publix", committee_id=13, purpose="Merch")]
    )
    # A Tuesday food purchase the meeting-food heuristic would otherwise claim.
    rows = [record("PURCHASE AUTHORIZED ON 09/16 PUBLIX GAINESVILLE FL", -20.0, "2025-09-18")]
    run = categorize_records(rows, memory)
    assert run.classifications[0].committee_id == 13
    assert run.classifications[0].source == "merchant"


def test_dues_and_refunds_are_unaffected_by_merchant_rules():
    memory = MerchantMemory(
        [MerchantRule(key="zelle", canonical_name="Zelle", committee_id=13, purpose="Merch")]
    )
    rows = [
        record("ZELLE FROM JANE DOE ON 07/08 REF # A1", 35.00),
        record("ZELLE TO JOHN SMITH ON 07/09 REF # A2", -50.00),
    ]
    run = categorize_records(rows, memory)
    assert run.classifications[0].committee_id == 1   # dues
    assert run.classifications[1].committee_id == 17  # refund


# --- The apply path ----------------------------------------------------------

@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "propose_merchants", ROOT / "scripts" / "propose_merchants.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=script_fieldnames())
        writer.writeheader()
        writer.writerows(rows)


def script_fieldnames() -> list[str]:
    return [
        "confirm", "committee_id", "committee", "confidence", "rows",
        "total_amount", "would_rebook", "kind", "key", "display_name",
        "basis", "warning",
    ]


def _row(**overrides) -> dict:
    base = {
        "confirm": "", "committee_id": "8", "committee": "Meeting Food",
        "confidence": "0.85", "rows": "3", "total_amount": "-10.00",
        "would_rebook": "", "kind": "merchant", "key": "publix gainesville",
        "display_name": "publix gainesville", "basis": "", "warning": "",
    }
    base.update(overrides)
    return base


def test_apply_ignores_rows_that_were_not_confirmed(script, tmp_path):
    """Nothing is written by default. Confirmation is opt-in, never opt-out."""
    path = tmp_path / "p.csv"
    _write_csv(path, [_row(), _row(key="chipotle")])
    accepted, complaints = script._confirmed_rows(path)
    assert accepted == []
    assert complaints == []


def test_apply_refuses_an_unknown_committee_id(script, tmp_path):
    path = tmp_path / "p.csv"
    _write_csv(path, [_row(confirm="y", committee_id="999")])
    accepted, complaints = script._confirmed_rows(path)
    assert accepted == []
    assert any("not a known committee" in c for c in complaints)


def test_apply_refuses_a_confirmed_row_with_no_committee(script, tmp_path):
    path = tmp_path / "p.csv"
    _write_csv(path, [_row(confirm="y", committee_id="")])
    accepted, complaints = script._confirmed_rows(path)
    assert accepted == []
    assert any("blank" in c for c in complaints)


def test_apply_refuses_memo_themes_as_merchant_rules(script, tmp_path):
    """A memo theme needs a rule in predicates.py, not a row in merchants."""
    path = tmp_path / "p.csv"
    _write_csv(path, [_row(confirm="y", kind="transfer-memo", key="merch", committee_id="13")])
    accepted, complaints = script._confirmed_rows(path)
    assert accepted == []
    assert any("cannot be stored as a merchant rule" in c for c in complaints)


def test_apply_accepts_a_properly_confirmed_row(script, tmp_path):
    path = tmp_path / "p.csv"
    _write_csv(path, [_row(confirm="y", committee_id="8")])
    accepted, complaints = script._confirmed_rows(path)
    assert complaints == []
    assert accepted == [
        {
            "merchant_key": "publix gainesville",
            "canonical_name": "publix gainesville",
            "committee_id": 8,
            "purpose": "Meeting Food",
            "source": "bulk-confirmed",
        }
    ]


@pytest.mark.parametrize("marker", ["y", "Y", "yes", "1", "true"])
def test_apply_accepts_the_usual_ways_of_writing_yes(script, tmp_path, marker):
    path = tmp_path / "p.csv"
    _write_csv(path, [_row(confirm=marker)])
    accepted, _ = script._confirmed_rows(path)
    assert len(accepted) == 1
