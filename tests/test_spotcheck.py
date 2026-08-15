"""
Spot-checking auto-applied rows (M20).

The point of this module is measuring something nobody has measured: accuracy on
the rows the categorizer books *without asking*. Coverage has always been
reported; accuracy on the confident majority never has.

The tests that carry weight here are the sampling-bias ones. A sample that
quietly skips the rare committees, or that changes between runs, produces a
number that looks like a measurement and is not one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ais_fmd.domain.categorize import spotcheck
from ais_fmd.domain.categorize.predicates import Classification
from ais_fmd.domain.categorize.spotcheck import SPOT_CHECK_SOURCE, build_sample


def assigned(committee_id: int, source: str = "rule", confidence: float = 1.0) -> Classification:
    return Classification(
        committee_id=committee_id,
        purpose=None,
        rule=f"test rule {committee_id}",
        confidence=confidence,
        source=source,
    )


UNASSIGNED = Classification(
    committee_id=None, purpose=None, rule="", confidence=0.0, source="none"
)


def record(index: int, amount: float = -10.0) -> dict:
    return {
        "transactionid": index,
        "transaction_date": "2025-09-18",
        "amount": amount,
        "details": f"PURCHASE {index}",
        "account": "Wells Fargo",
    }


def population(counts: dict[tuple[str, int], int]) -> tuple[list[dict], list]:
    """Build records/classifications with a given (tier, committee) shape."""
    records, classifications, index = [], [], 0
    for (source, committee_id), how_many in counts.items():
        for _ in range(how_many):
            records.append(record(index))
            classifications.append(assigned(committee_id, source))
            index += 1
    return records, classifications


# --- What gets sampled at all ------------------------------------------------

def test_unassigned_rows_are_never_sampled():
    records = [record(0), record(1)]
    sample = build_sample(records, [assigned(5), UNASSIGNED], size=10)
    assert sample.population == 1
    assert [r.index for r in sample.rows] == [0]


def test_merchant_rows_are_not_sampled():
    """A merchant mapping is already a human's decision; re-asking is noise."""
    records = [record(0), record(1)]
    sample = build_sample(records, [assigned(5, "rule"), assigned(5, "merchant")], size=10)
    assert sample.population == 1
    assert sample.rows[0].source == "rule"


def test_empty_population():
    sample = build_sample([], [], size=10)
    assert sample.size == 0
    assert "No auto-applied rows" in sample.summary_line()


# --- Stratification ----------------------------------------------------------

def test_every_stratum_gets_at_least_one_row():
    """
    The floor is the point. A rare committee is exactly where a systematic
    misbooking survives longest, because nobody ever looks at it.
    """
    records, classifications = population({
        ("rule", 1): 245,
        ("rule", 17): 116,
        ("scored", 5): 98,
        ("rule", 7): 3,       # rare
        ("scored", 4): 2,     # rarer
    })
    sample = build_sample(records, classifications, size=40)
    sampled = {row.stratum for row in sample.rows}
    assert ("rule", 7) in sampled
    assert ("scored", 4) in sampled
    assert len(sampled) == 5


def test_large_strata_get_proportionally_more():
    records, classifications = population({("rule", 1): 200, ("scored", 5): 20})
    sample = build_sample(records, classifications, size=22)
    counts: dict = {}
    for row in sample.rows:
        counts[row.stratum] = counts.get(row.stratum, 0) + 1
    assert counts[("rule", 1)] > counts[("scored", 5)]


def test_sample_never_exceeds_population():
    records, classifications = population({("rule", 1): 3})
    sample = build_sample(records, classifications, size=50)
    assert sample.size == 3


def test_size_smaller_than_strata_count_still_returns_that_many():
    records, classifications = population({
        ("rule", 1): 5, ("rule", 5): 5, ("rule", 7): 5, ("rule", 8): 5,
    })
    sample = build_sample(records, classifications, size=2)
    assert sample.size == 2


def test_no_duplicate_rows_in_a_sample():
    records, classifications = population({("rule", 1): 60, ("scored", 5): 40})
    sample = build_sample(records, classifications, size=50)
    indexes = [row.index for row in sample.rows]
    assert len(indexes) == len(set(indexes))


# --- Determinism -------------------------------------------------------------

def test_same_seed_gives_the_same_sample():
    records, classifications = population({("rule", 1): 100, ("scored", 5): 50})
    first = build_sample(records, classifications, size=30, seed=4)
    second = build_sample(records, classifications, size=30, seed=4)
    assert [r.index for r in first.rows] == [r.index for r in second.rows]


def test_different_seed_gives_a_different_sample():
    records, classifications = population({("rule", 1): 100, ("scored", 5): 50})
    first = build_sample(records, classifications, size=30, seed=1)
    second = build_sample(records, classifications, size=30, seed=99)
    assert [r.index for r in first.rows] != [r.index for r in second.rows]


def test_a_rows_rank_survives_new_rows_being_added():
    """
    Ranking on the row's own identity, not its position, is what lets a review
    be paused and resumed after more statements land.
    """
    records, classifications = population({("rule", 1): 40})
    first = build_sample(records, classifications, size=10, seed=2)

    extra_records = records + [record(500 + i) for i in range(20)]
    extra_classifications = classifications + [assigned(1) for _ in range(20)]
    second = build_sample(extra_records, extra_classifications, size=30, seed=2)

    # The originally-chosen rows are still chosen in the larger sample.
    assert {r.index for r in first.rows} & {r.index for r in second.rows}


# --- Largest-amount-first ----------------------------------------------------

def test_biggest_amount_in_a_stratum_is_always_included():
    """A misbooked $2,000 wire matters more than a misbooked $6 coffee."""
    records = [record(i, amount=-10.0) for i in range(30)]
    records[17]["amount"] = -2000.0
    classifications = [assigned(5, "scored") for _ in records]
    sample = build_sample(records, classifications, size=5, seed=3)
    assert 17 in {row.index for row in sample.rows}


def test_largest_first_can_be_switched_off():
    records = [record(i, amount=-10.0) for i in range(30)]
    records[17]["amount"] = -2000.0
    classifications = [assigned(5, "scored") for _ in records]
    sample = build_sample(records, classifications, size=3, seed=3, largest_first=0)
    assert 17 not in {row.index for row in sample.rows}


# --- Label construction ------------------------------------------------------

def make_row(index: int = 0, committee_id: int = 5) -> spotcheck.SpotCheckRow:
    return spotcheck.SpotCheckRow(
        index=index,
        record=record(index, amount=-42.5),
        committee_id=committee_id,
        committee="5 - Membership",
        rule="test",
        confidence=0.9,
        source="scored",
        amount=Decimal("-42.50"),
    )


def test_label_records_what_the_model_said_even_when_corrected():
    """Agreement rate has to come out of one query, not a kept-around sample."""
    label = spotcheck.to_label(make_row(), committee_id=8, actor="me", era="2024-2026")
    assert label["committee_id"] == 8
    assert label["model_committee"] == 5
    assert label["source"] == SPOT_CHECK_SOURCE


def test_label_is_keyed_so_reapplying_adds_nothing():
    """A NULL natural_key would let ON CONFLICT never fire — see the docstring."""
    first = spotcheck.to_label(make_row(), 5, "me", "2024-2026")
    second = spotcheck.to_label(make_row(), 8, "someone-else", "2024-2026")
    assert first["natural_key"]
    assert first["natural_key"] == second["natural_key"]


def test_different_transactions_get_different_keys():
    a = spotcheck.to_label(make_row(index=1), 5, "me", "2024-2026")
    b = spotcheck.to_label(make_row(index=2), 5, "me", "2024-2026")
    assert a["natural_key"] != b["natural_key"]


def test_label_fields_are_all_bindable():
    """
    Records arrive via DataFrame.to_dict, so dates are Timestamps and numbers
    are numpy scalars. Neither binds to SQLite, and the failure surfaces far
    from its cause.
    """
    pd = pytest.importorskip("pandas")
    row = make_row()
    row.record["transaction_date"] = pd.Timestamp("2025-09-18")
    row.record["transactionid"] = pd.array([7], dtype="int64")[0]
    label = spotcheck.to_label(row, 5, "me", "2024-2026")
    for key, value in label.items():
        assert value is None or isinstance(value, (str, int, float, bool)), (key, type(value))


# --- Agreement reporting -----------------------------------------------------

def test_agreement_counts_only_spot_checks():
    labels = [
        {"source": "ledger", "model_committee": 5, "committee_id": 8, "model_source": "rule"},
        {"source": SPOT_CHECK_SOURCE, "model_committee": 5, "committee_id": 5, "model_source": "rule"},
        {"source": SPOT_CHECK_SOURCE, "model_committee": 5, "committee_id": 8, "model_source": "scored"},
    ]
    stats = spotcheck.agreement(labels)
    assert stats["checked"] == 2
    assert stats["agreed"] == 1
    assert stats["rate"] == 0.5


def test_agreement_splits_by_tier():
    labels = [
        {"source": SPOT_CHECK_SOURCE, "model_committee": 1, "committee_id": 1, "model_source": "rule"},
        {"source": SPOT_CHECK_SOURCE, "model_committee": 1, "committee_id": 1, "model_source": "rule"},
        {"source": SPOT_CHECK_SOURCE, "model_committee": 5, "committee_id": 8, "model_source": "scored"},
    ]
    stats = spotcheck.agreement(labels)
    assert stats["by_source"]["rule"] == {"checked": 2, "agreed": 2}
    assert stats["by_source"]["scored"] == {"checked": 1, "agreed": 0}


def test_agreement_with_nothing_checked():
    assert spotcheck.agreement([])["checked"] == 0
    assert spotcheck.agreement([{"source": "ledger"}])["checked"] == 0


# --- The bias warning this exists to clear -----------------------------------

def test_spot_checks_clear_the_selection_bias_warning():
    from ais_fmd.domain.categorize.learning import Evaluation

    biased = Evaluation(source_mix={"review": 12})
    assert biased.selection_bias_warning()

    balanced = Evaluation(source_mix={"review": 12, "spot-check": 40})
    assert balanced.selection_bias_warning() == ""


# --- Honesty about what a sample can support ---------------------------------

def test_small_samples_say_they_are_not_a_measurement():
    records, classifications = population({("rule", 1): 100})
    assert "smoke test" in build_sample(records, classifications, size=10).coverage_note()


def test_larger_samples_report_a_margin():
    records, classifications = population({("rule", 1): 400})
    note = build_sample(records, classifications, size=100).coverage_note()
    assert "+/-" in note
