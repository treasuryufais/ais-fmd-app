"""
The freshness line, pace section and downloads on the VP pages.

The logic behind them is tested in test_vp_extras.py; these render the real pages.
"""

from __future__ import annotations

from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import OFFICER, run_as  # noqa: F401


def test_my_committee_shows_how_current_the_figures_are_and_the_pace_section(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, 7)
    assert_clean(app, "My Committee as the Consulting VP")
    assert any("Includes charges through" in caption.value for caption in app.caption)
    assert "Spending pace" in [subheader.value for subheader in app.subheader]


def test_both_pages_offer_a_download(seeded_db, use_db):
    use_db(seeded_db)
    committee = run_as(VIEWS / "Officer.py", OFFICER, 7)
    transactions = run_as(VIEWS / "MyTransactions.py", OFFICER, 7, mytxn_semester="All")
    assert_clean(committee, "My Committee")
    assert_clean(transactions, "My Transactions")
    assert len(committee.get("download_button")) == 1
    assert len(transactions.get("download_button")) == 1
