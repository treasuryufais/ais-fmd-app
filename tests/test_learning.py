"""
Ground truth, learning, and generated model context (M19 / M20).

The theme of these tests is provenance. Everything here exists because the
project had no human labels at all -- `transactions.budget_category` was written
by the categorizer itself, so anything fitted to it would have been the model
learning from its own output. These assert the parts that keep that from
happening again: labels carry a source and an era, re-imports cannot inflate the
training set, and era-scoped features stay era-scoped.
"""

from __future__ import annotations

import pytest

from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.categorize.context import (
    build_context,
    build_prompt_for,
    contested_merchants,
    find_precedents,
    merchant_decisions,
)
from ais_fmd.domain.categorize.learning import (
    KNOWN_TAXONOMY_DRIFT,
    evaluate,
    fit_weights,
    predict,
)
from ais_fmd.domain.parsers.ledger import (
    markdown_tables,
    natural_key,
    parse_amount,
    parse_workbook,
)

WORKBOOK = """
| Date | Amount | Details | Budget | Purpose | Account |
| :-: | :-: | :-: | :-: | :-: | :-: |
| 2022-05-09 | \\-9.9 | PURCHASE AUTHORIZED ON 05/05 WIX.COM CA CARD 2949 | Technology | Misc. | Wells |
| 2022-09-11 | \\-42.10 | PURCHASE AUTHORIZED ON 09/11 PUBLIX #1560 GAINESVILLE FL CARD 0319 | Meeting Food | GBM Catering | Wells |
| 2023-01-06 | \\-120.00 | blank | Membership | Social Events | Cash |
| 2023-02-08 | 25.0 | ZELLE FROM A B ON 02/08 REF # X1 AIS FORMAL 2023 | Membership | Social Events | Wells |
| 2023-03-01 | \\-15.00 | PURCHASE AUTHORIZED ON 03/01 SOMEWHERE FL CARD 0319 | Mixer | Mixer | Wells |
"""


# --- Ledger parsing ----------------------------------------------------------

def test_parses_labeled_rows_from_a_rendered_workbook():
    result = parse_workbook(WORKBOOK, source_ref="test.txt", era="2022-2023")
    assert result.total_kept == 3  # blank details and the retired committee drop out
    details = " ".join(e["details"] for e in result.examples)
    assert "WIX.COM" in details
    assert {e["committee_id"] for e in result.examples} == {15, 8, 5}


def test_retired_committees_are_reported_not_silently_remapped():
    """
    'Mixer' has no equivalent in today's chart of accounts. Mapping it to
    something plausible would rewrite history; skipping it silently would hide
    that 41 real rows were dropped.
    """
    result = parse_workbook(WORKBOOK, source_ref="test.txt", era="2022-2023")
    assert result.skipped_unknown_committee.get("Mixer") == 1
    assert all(e["committee_id"] is not None for e in result.examples)


def test_rows_without_a_description_are_skipped():
    result = parse_workbook(WORKBOOK, source_ref="test.txt", era="2022-2023")
    assert result.skipped_no_details == 1


def test_markdown_escaping_is_stripped_from_amounts():
    """The Drive rendering escapes minus signs as '\\-9.9'."""
    assert parse_amount("\\-9.9") == -9.9
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("(50.00)") == -50.0
    assert parse_amount("blank") is None


def test_alignment_rows_are_not_treated_as_data():
    tables = markdown_tables(WORKBOOK)
    assert len(tables) == 1
    assert len(tables[0]) == 5


def test_natural_key_is_stable_and_content_addressed():
    a = natural_key("2022-05-09", -9.9, "WIX.COM CA")
    b = natural_key("2022-05-09", -9.9, "wix.com ca")  # case-insensitive
    c = natural_key("2022-05-09", -9.9, "SOMETHING ELSE")
    assert a == b
    assert a != c


def test_examples_carry_their_provenance():
    result = parse_workbook(WORKBOOK, source_ref="ledger.txt", era="2022-2023")
    example = result.examples[0]
    assert example["source"] == "ledger"
    assert example["source_ref"] == "ledger.txt"
    assert example["era"] == "2022-2023"


# --- Storage -----------------------------------------------------------------

def test_reimporting_the_same_ledger_adds_nothing():
    """
    Duplicated labels would silently over-weight whatever was re-imported,
    which is the quiet way a training set goes wrong.
    """
    backend = SqliteBackend()
    parsed = parse_workbook(WORKBOOK, source_ref="test.txt", era="2022-2023")

    first = backend.insert_labeled_examples(parsed.examples, "tester")
    second = backend.insert_labeled_examples(parsed.examples, "tester")

    assert first.updated == parsed.total_kept
    assert second.updated == 0
    assert second.unchanged == parsed.total_kept
    assert len(backend.fetch_labeled_examples()) == parsed.total_kept


def test_labels_from_the_queue_record_what_the_model_proposed():
    """The disagreement is the signal; storing only the answer discards it."""
    backend = SqliteBackend()
    backend.insert_labeled_examples(
        [
            {
                "source": "review",
                "era": "2024-2026",
                "transaction_id": 7,
                "details": "PURCHASE AUTHORIZED ON 09/16 PUBLIX CARD 5718",
                "amount": -20.0,
                "committee_id": 8,
                "model_committee": 5,
                "model_confidence": 0.42,
                "model_source": "proposed",
                "natural_key": "review:7",
            }
        ],
        "tester",
    )
    stored = backend.fetch_labeled_examples().iloc[0]
    assert stored["committee_id"] == 8
    assert stored["model_committee"] == 5
    assert stored["model_confidence"] == pytest.approx(0.42)


# --- Fitting -----------------------------------------------------------------

def _label(details: str, committee_id: int, amount: float = -50.0, date: str = "2025-09-18") -> dict:
    return {
        "details": details,
        "amount": amount,
        "transaction_date": date,
        "committee_id": committee_id,
        "source": "ledger",
    }


def test_card_signals_are_excluded_from_cross_era_fitting():
    """
    Cardholders change between eras, so a card weight fitted across them is
    meaningless. It must be opt-in, not the default.
    """
    labels = [_label("PURCHASE AUTHORIZED ON 09/16 SOMEWHERE CARD 5718", 5) for _ in range(5)]
    default = fit_weights(labels)
    opted_in = fit_weights(labels, include_card_signals=True)

    assert not any(name == "card" for name, _ in default.stats)
    assert any(name == "card" for name, _ in opted_in.stats)


def test_a_signal_that_is_usually_wrong_learns_a_low_weight():
    """
    The point of fitting. A bar merchant looks like Membership, but if humans
    keep filing those under Consulting the weight must fall.
    """
    labels = [_label(f"PURCHASE AUTHORIZED ON 09/16 MACDINTONS #{i} FL", 7) for i in range(10)]
    fitted = fit_weights(labels)
    bar = fitted.stats.get(("bar-merchant", 5))
    assert bar is not None
    assert bar.fired == 10
    assert bar.agreed == 0
    assert bar.learned_weight == 0.0


def test_a_reliable_signal_learns_a_high_weight():
    labels = [
        _label(f"PURCHASE AUTHORIZED ON 09/16 PUBLIX #{i} GAINESVILLE FL", 8) for i in range(10)
    ]
    fitted = fit_weights(labels)
    food = fitted.stats.get(("food-merchant", 8))
    assert food is not None
    assert food.learned_weight > 1.0


def test_rare_signals_are_not_trusted():
    fitted = fit_weights([_label("PURCHASE AUTHORIZED ON 09/16 MACDINTONS FL", 5)])
    bar = fitted.stats.get(("bar-merchant", 5))
    assert not bar.trustworthy
    assert fitted.weight_for("bar-merchant", 5, fallback=2.5) == 2.5


# --- Evaluation --------------------------------------------------------------

def test_evaluation_runs_the_whole_pipeline_not_just_scoring():
    """
    REGRESSION. The first version measured `score()` alone and reported that
    91% of the historical ledger produced "no signal" -- those rows are dues and
    transfers that the exact rules resolve before scoring is consulted.
    Measuring one tier of a four-tier pipeline says nothing about the pipeline.
    """
    labels = [
        {
            "details": "ZELLE FROM JANE DOE ON 07/08 REF # A1",
            "amount": 35.0,
            "transaction_date": "2025-09-18",
            "committee_id": 1,
            "source": "ledger",
        }
    ]
    (committee, confidence, source), = predict(labels)
    assert committee == 1, "an exact rule must be visible to the evaluator"
    assert source == "rule"


def test_taxonomy_drift_is_not_counted_as_error():
    """
    Formal income was booked to Membership before committee 18 existed. Counting
    that as a mistake would argue for breaking a rule that is right today.
    """
    assert (18, 5) in KNOWN_TAXONOMY_DRIFT
    labels = [
        {
            "details": "ZELLE FROM A B ON 02/08 REF # X1 AIS FORMAL 2023",
            "amount": 25.0,
            "transaction_date": "2023-02-08",
            "committee_id": 5,
            "source": "ledger",
        }
    ]
    result = evaluate(labels)
    assert result.drift == 1
    assert result.by_committee == {}


def test_evaluation_flags_a_label_set_drawn_only_from_flagged_rows():
    """Selection bias: labels only from hard cases misrepresent the easy majority."""
    labels = [_label("PURCHASE AUTHORIZED ON 09/16 PUBLIX FL", 8) for _ in range(3)]
    for label in labels:
        label["source"] = "review"
    warning = evaluate(labels).selection_bias_warning()
    assert "spot-check" in warning


def test_no_warning_once_spot_checks_exist():
    labels = [_label("PURCHASE AUTHORIZED ON 09/16 PUBLIX FL", 8) for _ in range(3)]
    labels[0]["source"] = "review"
    labels[1]["source"] = "spot-check"
    labels[2]["source"] = "ledger"
    assert evaluate(labels).selection_bias_warning() == ""


def test_threshold_recommendation_prefers_coverage_among_accurate_bars():
    labels = [
        _label(f"PURCHASE AUTHORIZED ON 09/16 PUBLIX #{i} GAINESVILLE FL", 8, date="2025-09-16")
        for i in range(20)
    ]
    result = evaluate(labels)
    recommended = result.recommend(0.9)
    if recommended is not None:
        accurate = [p for p in result.points if p.applied and p.precision >= 0.9]
        assert recommended.threshold == min(p.threshold for p in accurate)


# --- Generated context (M20) -------------------------------------------------

def test_settled_merchants_are_offered_to_the_model():
    labels = [_label(f"PURCHASE AUTHORIZED ON 09/16 WIX.COM #{i} CA", 15) for i in range(4)]
    decided = merchant_decisions(labels)
    assert any(committee_id == 15 for _key, committee_id, _n in decided)


def test_inconsistently_decided_merchants_are_not_presented_as_settled():
    """
    Telling the model "PUBLIX is Meeting Food" when humans split it 6/5 would
    teach a confidence nobody has. Those belong in the contested list.
    """
    labels = [_label("PURCHASE AUTHORIZED ON 09/16 PUBLIX #1 GAINESVILLE FL", 8) for _ in range(3)]
    labels += [_label("PURCHASE AUTHORIZED ON 09/16 PUBLIX #1 GAINESVILLE FL", 5) for _ in range(3)]

    settled_keys = {key for key, _c, _n in merchant_decisions(labels)}
    contested_keys = {key for key, _counts in contested_merchants(labels)}
    assert not settled_keys & contested_keys
    assert contested_keys


def test_precedent_retrieval_prefers_the_same_merchant():
    labels = [
        _label("PURCHASE AUTHORIZED ON 01/23 CHIPOTLE ONLINE CA CARD 0319", 7),
        _label("PURCHASE AUTHORIZED ON 09/11 PUBLIX #1560 GAINESVILLE FL", 8),
    ]
    precedents = find_precedents(
        "PURCHASE AUTHORIZED ON 04/02 CHIPOTLE ONLINE CA CARD 8313", labels
    )
    assert precedents
    assert precedents[0].committee_id == 7, "the matching merchant must rank first"


def test_context_names_the_current_card_roster():
    context = build_context([])
    assert "8313" in context and "5718" in context
    assert "Annalee" in context and "Grant" in context


def test_context_warns_that_cards_are_not_certain():
    """The prompt must carry the caveat treasury gave, or the model over-trusts it."""
    context = build_context([]).lower()
    assert "not a certainty" in context or "wrong card" in context


def test_prompt_includes_precedents_for_the_row_being_judged():
    labels = [_label("PURCHASE AUTHORIZED ON 01/23 CHIPOTLE ONLINE CA", 7)]
    prompt = build_prompt_for("PURCHASE AUTHORIZED ON 04/02 CHIPOTLE ONLINE CA", labels)
    assert "PAST DECISIONS" in prompt
    assert "Consulting" in prompt


def test_prompt_stays_bounded_as_labels_accumulate():
    """Retrieval is capped so a large label set cannot inflate cost without bound."""
    many = [
        _label(f"PURCHASE AUTHORIZED ON 09/16 MERCHANT{i} GAINESVILLE FL", 8)
        for i in range(500)
    ]
    prompt = build_prompt_for("PURCHASE AUTHORIZED ON 09/16 MERCHANT7 GAINESVILLE FL", many)
    assert len(prompt) < 12000, f"prompt grew to {len(prompt)} chars"
