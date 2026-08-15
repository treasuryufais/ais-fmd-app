"""
Guards against the per-rerun recompute regressions.

These do not assert wall-clock times -- those are noisy and machine-dependent.
They assert the *shape* of the work: that the loop-invariant O(rows) passes
stay hoisted out of the per-semester loops.

This is the failure mode that already happened once. `attach_semester` was
made vectorised and fast, and then callers reintroduced the cost by invoking
it once per semester inside a loop. The inner function was fixed; the outer
loop put the cost back. A timing test would not have caught that -- the
per-call time was fine, the call count was not.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain import budgets, dues, terms as terms_module
from ais_fmd.domain.terms import attach_semester, ordered_semesters


@pytest.fixture
def sample_terms() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"TermID": "t1", "Semester": "Fall 2024", "start_date": "2024-08-15", "end_date": "2024-12-15"},
            {"TermID": "t2", "Semester": "Spring 2025", "start_date": "2025-01-05", "end_date": "2025-05-05"},
            {"TermID": "t3", "Semester": "Fall 2025", "start_date": "2025-08-15", "end_date": "2025-12-15"},
            {"TermID": "t4", "Semester": "Spring 2026", "start_date": "2026-01-05", "end_date": "2026-05-05"},
        ]
    )


@pytest.fixture
def sample_transactions() -> pd.DataFrame:
    rows = []
    for index, date in enumerate(
        ["2024-09-01", "2024-10-02", "2025-02-03", "2025-03-04",
         "2025-09-05", "2025-10-06", "2026-02-07", "2026-03-08"]
    ):
        rows.append(
            {
                "transaction_id": index,
                "transaction_date": date,
                "amount": -50.0 if index % 2 else 35.0,
                "details": f"ROW {index}",
                "budget_category": 5 if index % 2 else 1,
                "purpose": "Food & Drink",
                "account": "Wells Fargo",
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def sample_budgets(sample_terms) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"termid": term_id, "committeeid": committee, "budget_amount": 500.0}
            for term_id in sample_terms["TermID"]
            for committee in (1, 5)
        ]
    )


@pytest.fixture
def count_tagging(monkeypatch):
    """Count how many times the whole-table tagging pass runs."""
    calls: list[int] = []
    original = terms_module.attach_semester

    def counting(df_transactions, df_terms, **kwargs):
        calls.append(len(df_transactions))
        return original(df_transactions, df_terms, **kwargs)

    monkeypatch.setattr(terms_module, "attach_semester", counting)
    monkeypatch.setattr(budgets, "attach_semester", counting)
    monkeypatch.setattr(dues, "attach_semester", counting)
    return calls


def test_historical_budget_tags_once_not_once_per_semester(
    sample_transactions, sample_budgets, sample_terms, count_tagging
):
    budgets.historical_budget_vs_actual(
        sample_transactions, sample_budgets, sample_terms, None
    )
    assert len(count_tagging) == 1, (
        f"tagged the table {len(count_tagging)} times for "
        f"{len(sample_terms)} semesters -- the O(rows) pass is back inside the loop"
    )


def test_semester_totals_batch_tags_once(sample_transactions, sample_terms, count_tagging):
    order = ordered_semesters(sample_terms)
    budgets.semester_totals_by_semester(sample_transactions, sample_terms, order)
    assert len(count_tagging) == 1


def test_dues_compare_semesters_tags_once(sample_transactions, sample_terms, count_tagging):
    dues.compare_semesters(sample_transactions, sample_terms, ordered_semesters(sample_terms))
    assert len(count_tagging) == 1


def test_dues_compare_semesters_derives_dues_subset_once(
    sample_transactions, sample_terms, monkeypatch
):
    """`dues_transactions` is a parse_amount per row -- also loop-invariant."""
    calls: list[int] = []
    original = dues.dues_transactions

    def counting(df, schedule=None):
        calls.append(len(df))
        return original(df, schedule)

    monkeypatch.setattr(dues, "dues_transactions", counting)
    dues.compare_semesters(sample_transactions, sample_terms, ordered_semesters(sample_terms))
    assert len(calls) == 1


def test_semester_index_is_memoized_on_values(sample_terms, monkeypatch):
    """Repeated lookups reuse the built IntervalIndex rather than rebuilding it."""
    terms_module._INDEX_CACHE.clear()
    builds: list[int] = []
    original = terms_module.prepare_terms

    def counting(df):
        builds.append(1)
        return original(df)

    monkeypatch.setattr(terms_module, "prepare_terms", counting)
    for _ in range(5):
        terms_module.semester_index(sample_terms)
    assert len(builds) == 1


def test_semester_index_cache_keys_on_content_not_identity(sample_terms):
    """A different terms table must not hit another table's cached index."""
    terms_module._INDEX_CACHE.clear()
    first = terms_module.semester_index(sample_terms)

    edited = sample_terms.copy()
    edited.loc[edited["Semester"] == "Fall 2024", "Semester"] = "Autumn 2024"
    second = terms_module.semester_index(edited)

    assert "Fall 2024" in set(first.to_numpy())
    assert "Autumn 2024" in set(second.to_numpy())
    assert "Fall 2024" not in set(second.to_numpy())


def test_semester_index_cache_notices_date_edits(sample_terms):
    """Shifting a term's dates must rebuild the index, not serve the stale one."""
    terms_module._INDEX_CACHE.clear()
    tx = pd.DataFrame(
        [{"transaction_date": "2025-06-20", "amount": -10.0}]
    )
    assert attach_semester(tx, sample_terms)["Semester"].iloc[0] is None

    widened = sample_terms.copy()
    widened.loc[widened["Semester"] == "Spring 2025", "end_date"] = "2025-07-31"
    assert attach_semester(tx, widened)["Semester"].iloc[0] == "Spring 2025"


def test_cache_stays_bounded(sample_terms):
    """A long-lived server must not accumulate an entry per edit forever."""
    terms_module._INDEX_CACHE.clear()
    for year in range(2000, 2000 + terms_module._INDEX_CACHE_LIMIT + 10):
        variant = sample_terms.copy()
        variant.loc[:, "Semester"] = f"Fall {year}"
        terms_module.semester_index(variant)
    assert len(terms_module._INDEX_CACHE) <= terms_module._INDEX_CACHE_LIMIT
