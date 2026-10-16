"""
Tests for sponsorship goal tracking (M20).

Fixtures intentionally mirror the real seed data's quirk: sponsorship
transactions carry `purpose == "Sponsorship / Donation"` but are booked
against committee 6 (Corporate Relations), not a dedicated sponsorship
committee. Matching happens on `purpose` for exactly that reason -- see the
module docstring in `ais_fmd/domain/sponsorship.py`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain import sponsorship as sponsorship_domain

SPONSORSHIP_PURPOSE = sponsorship_domain.SPONSORSHIP_PURPOSE
CORPORATE_RELATIONS_COMMITTEE_ID = 6


@pytest.fixture
def terms() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "TermID": ["FA24", "FA25"],
            "Semester": ["Fall 2024", "Fall 2025"],
            "start_date": ["2024-08-21", "2025-08-20"],
            "end_date": ["2024-12-13", "2025-12-12"],
            "sponsorship_goal": [None, 5000.0],
        }
    )


@pytest.fixture
def transactions() -> pd.DataFrame:
    rows = [
        # id, date, amount, details, committee, purpose, sponsor_name
        (1, "2025-09-01", 2000.00, "ACME CORP SPONSORSHIP", CORPORATE_RELATIONS_COMMITTEE_ID, SPONSORSHIP_PURPOSE, "Acme Corp"),
        # Sponsor not yet typed in -- the common case for a freshly-imported
        # payment, must surface as "" not None/NaN.
        (2, "2025-09-15", 1500.00, "WIDGETCO DONATION", CORPORATE_RELATIONS_COMMITTEE_ID, SPONSORSHIP_PURPOSE, None),
        # A correction/refund with the same purpose -- must not count as
        # money raised.
        (3, "2025-09-20", -200.00, "ACME CORP REFUND", CORPORATE_RELATIONS_COMMITTEE_ID, SPONSORSHIP_PURPOSE, None),
        # Not sponsorship -- ordinary committee spending, same committee ID
        # as the real sponsorship rows on purpose, to prove matching is on
        # `purpose` and not `budget_category`.
        (4, "2025-09-10", -420.00, "PURCHASE AUTHORIZED ON 09/10 PUBLIX FL", CORPORATE_RELATIONS_COMMITTEE_ID, "Meeting Food", None),
        # Uncategorized (no purpose yet) -- not counted, per the documented
        # limitation; would need the review queue worked first.
        (5, "2025-09-12", 900.00, "UNKNOWN DEPOSIT", None, None, None),
        # Prior term -- must not leak into Fall 2025's total.
        (6, "2024-09-01", 1000.00, "LAST YEAR'S SPONSOR", CORPORATE_RELATIONS_COMMITTEE_ID, SPONSORSHIP_PURPOSE, "Last Year Inc"),
    ]
    return pd.DataFrame(
        {
            "transactionid": [r[0] for r in rows],
            "transaction_date": pd.to_datetime([r[1] for r in rows]),
            "amount": [r[2] for r in rows],
            "details": [r[3] for r in rows],
            "budget_category": pd.array([r[4] for r in rows], dtype="Int64"),
            "purpose": [r[5] for r in rows],
            "sponsor_name": [r[6] for r in rows],
        }
    )


def test_sponsorship_transactions_matches_on_purpose_not_committee(transactions):
    """
    The Meeting Food row shares committee 6 with the real sponsorship rows and
    must still be excluded -- proves matching is on `purpose`, not
    `budget_category`. The uncategorized deposit (no purpose set) and the
    refund must also be excluded.
    """
    scoped = sponsorship_domain.sponsorship_transactions(transactions)
    assert set(scoped["details"]) == {
        "ACME CORP SPONSORSHIP",
        "WIDGETCO DONATION",
        "LAST YEAR'S SPONSOR",
    }


def test_goal_for_term_reads_stored_value(terms):
    assert sponsorship_domain.goal_for_term(terms, "FA25") == 5000.0


def test_goal_for_term_is_none_when_unset(terms):
    assert sponsorship_domain.goal_for_term(terms, "FA24") is None


def test_goal_for_term_is_none_for_unknown_term(terms):
    assert sponsorship_domain.goal_for_term(terms, "NOPE") is None


def test_summarize_totals_only_the_selected_semester(transactions, terms):
    summary = sponsorship_domain.summarize(transactions, terms, "Fall 2025")
    assert summary.payment_count == 2
    assert summary.raised == pytest.approx(3500.00)
    assert summary.goal == 5000.0


def test_summarize_percent_and_remaining(transactions, terms):
    summary = sponsorship_domain.summarize(transactions, terms, "Fall 2025")
    assert summary.percent_of_goal == pytest.approx(70.0)
    assert summary.remaining == pytest.approx(1500.00)


def test_summarize_with_no_goal_set(transactions, terms):
    summary = sponsorship_domain.summarize(transactions, terms, "Fall 2024")
    assert summary.raised == pytest.approx(1000.00)
    assert summary.goal is None
    assert summary.percent_of_goal is None
    assert summary.remaining is None


def test_summarize_on_empty_transactions(terms):
    empty = pd.DataFrame(
        columns=["transaction_date", "amount", "details", "budget_category", "purpose"]
    )
    summary = sponsorship_domain.summarize(empty, terms, "Fall 2025")
    assert summary.raised == 0.0
    assert summary.payment_count == 0
    assert summary.goal == 5000.0


# --- History view (compare_semesters) -----------------------------------------

def test_compare_semesters_one_row_per_semester(transactions, terms):
    history = sponsorship_domain.compare_semesters(
        transactions, terms, ["Fall 2024", "Fall 2025"]
    )
    assert list(history["Semester"]) == ["Fall 2024", "Fall 2025"]


def test_compare_semesters_shows_goal_and_raised_per_row(transactions, terms):
    history = sponsorship_domain.compare_semesters(
        transactions, terms, ["Fall 2024", "Fall 2025"]
    ).set_index("Semester")

    # Fall 2024: no goal set, $1,000 raised. Pandas stores the missing goal as
    # NaN in this float column -- that is the correct on-disk representation
    # of "no goal", not a bug; `pd.isna` is the right way to check it.
    assert pd.isna(history.loc["Fall 2024", "Goal"])
    assert history.loc["Fall 2024", "Raised"] == pytest.approx(1000.00)
    assert pd.isna(history.loc["Fall 2024", "% of Goal"])

    # Fall 2025: $5,000 goal, $3,500 raised -> 70%.
    assert history.loc["Fall 2025", "Goal"] == 5000.0
    assert history.loc["Fall 2025", "Raised"] == pytest.approx(3500.00)
    assert history.loc["Fall 2025", "% of Goal"] == pytest.approx(70.0)
    assert history.loc["Fall 2025", "Payments"] == 2


# --- Drill-down (transactions_for_semester) -----------------------------------

def test_transactions_for_semester_lists_individual_payments(transactions, terms):
    detail = sponsorship_domain.transactions_for_semester(transactions, terms, "Fall 2025")
    assert list(detail.columns) == ["transactionid", "Date", "Amount", "Details", "Sponsor"]
    assert set(detail["Details"]) == {"ACME CORP SPONSORSHIP", "WIDGETCO DONATION"}
    assert set(detail["Amount"]) == {2000.00, 1500.00}


def test_transactions_for_semester_sorted_by_date(transactions, terms):
    detail = sponsorship_domain.transactions_for_semester(transactions, terms, "Fall 2025")
    assert list(detail["Date"]) == sorted(detail["Date"])


def test_transactions_for_semester_excludes_refund_and_other_purpose(transactions, terms):
    """Same exclusions as the aggregate total -- the drill-down must not show
    rows that never counted toward Raised in the first place."""
    detail = sponsorship_domain.transactions_for_semester(transactions, terms, "Fall 2025")
    assert "ACME CORP REFUND" not in set(detail["Details"])
    assert "PURCHASE AUTHORIZED ON 09/10 PUBLIX FL" not in set(detail["Details"])


def test_transactions_for_semester_empty_when_no_payments(transactions, terms):
    detail = sponsorship_domain.transactions_for_semester(transactions, terms, "Spring 2025")
    assert detail.empty
    assert list(detail.columns) == ["transactionid", "Date", "Amount", "Details", "Sponsor"]


def test_transactions_for_semester_includes_sponsor_name(transactions, terms):
    detail = sponsorship_domain.transactions_for_semester(
        transactions, terms, "Fall 2025"
    ).set_index("Details")
    assert detail.loc["ACME CORP SPONSORSHIP", "Sponsor"] == "Acme Corp"


def test_transactions_for_semester_blank_not_null_when_sponsor_unset(transactions, terms):
    """Unset must render as "" for a text-column editor, not None/NaN."""
    detail = sponsorship_domain.transactions_for_semester(
        transactions, terms, "Fall 2025"
    ).set_index("Details")
    assert detail.loc["WIDGETCO DONATION", "Sponsor"] == ""


def test_transactions_for_semester_carries_transaction_id(transactions, terms):
    """The id is what a caller writes `set_transaction_sponsor_name` back with."""
    detail = sponsorship_domain.transactions_for_semester(
        transactions, terms, "Fall 2025"
    ).set_index("Details")
    assert detail.loc["ACME CORP SPONSORSHIP", "transactionid"] == 1
    assert detail.loc["WIDGETCO DONATION", "transactionid"] == 2
