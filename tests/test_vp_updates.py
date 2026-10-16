"""
Updates on My Committee: what a VP is told about charges they flagged, and about
charges the treasurer moved INTO their committee.

The point of the second half is that a moved charge changes a VP's totals, and
without a message it just appears. The property that matters most is the privacy
one: the committee a charge arrives in learns about the charge, and nothing about
who flagged it, why, or which committee it came from.

All data is the sandbox seed; all emails are invented `@sandbox.local` addresses.
"""

from __future__ import annotations

import pandas as pd

from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import flags as flags_domain

# Fixtures and helpers shared with the other VP tests.
from tests.test_flags import NOTE, OFFICER, VP, _flagged, charge_in  # noqa: F401
from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import run_as  # noqa: F401

TREASURER_EMAIL = "treasurer@sandbox.local"
TODAY = pd.Timestamp("2026-10-10")
CONSULTING, MARKETING, MEMBERSHIP = 7, 9, 5


def move(backend: SqliteBackend, flag_id: int, to: int) -> None:
    result = backend.move_flagged_charge(flag_id, to, TREASURER_EMAIL, "Checked the receipt.")
    assert result.ok, result.error


def page_text(app) -> str:
    """Every visible piece of text a page rendered, in one string."""
    parts: list[str] = []
    for group in (app.success, app.info, app.warning, app.error, app.markdown, app.caption, app.subheader):
        parts.extend(str(element.value) for element in group)
    return "\n".join(parts)


# --- The rules (pure) ----------------------------------------------------------

CHARGES = pd.DataFrame(
    {
        "transactionid": [1, 2, 3],
        "transaction_date": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]),
        "amount": [-50.0, -60.0, -70.0],
        "details": ["PUBLIX", "CANVA", "STAPLES"],
        "budget_category": [CONSULTING, MARKETING, CONSULTING],
        "purpose": ["Food", "Marketing", "Misc."],
    }
)


def flag(**kw) -> dict:
    base = {
        "flag_id": 1, "transaction_id": 1, "booked_to_committee_id": CONSULTING,
        "flagged_by": VP, "note": NOTE, "status": "open",
        "flagged_at": "2026-10-08 10:00:00", "resolved_at": None, "resolved_by": None,
        "resolution_note": None,
    }
    base.update(kw)
    return base


def notes(own=(), arrivals=None, lines=(CONSULTING,), **kw):
    own_frame = pd.DataFrame(list(own)) if own else pd.DataFrame()
    return flags_domain.notifications(own_frame, arrivals, CHARGES, lines, today=TODAY, **kw)


def test_a_waiting_flag_says_it_is_waiting_and_shows_the_charge():
    result = notes([flag()])
    assert list(result["kind"]) == [flags_domain.PENDING]
    assert result.iloc[0]["details"] == "PUBLIX"
    assert result.iloc[0]["amount"] == -50.0


def test_a_flag_whose_charge_left_is_reported_as_moved_out():
    # Charge 2 is booked to Marketing now, so for Consulting it has left.
    result = notes([flag(transaction_id=2, status="resolved", resolved_at="2026-10-09 09:00:00",
                         resolution_note="Moved from Consulting to Marketing.")])
    assert list(result["kind"]) == [flags_domain.MOVED_OUT]
    assert result.iloc[0]["reply"] == "Moved from Consulting to Marketing."


def test_a_resolved_flag_whose_charge_is_still_theirs_is_not_called_moved():
    result = notes([flag(transaction_id=1, status="resolved", resolved_at="2026-10-09 09:00:00")])
    assert list(result["kind"]) == [flags_domain.CLOSED]


def test_a_dismissed_flag_says_the_treasurer_kept_it():
    result = notes([flag(status="dismissed", resolved_at="2026-10-09 09:00:00",
                         resolution_note="It is Consulting's.")])
    assert list(result["kind"]) == [flags_domain.KEPT]


def test_answered_flags_expire_but_waiting_ones_do_not():
    old = "2026-08-01 09:00:00"
    answered = flag(flag_id=1, status="dismissed", flagged_at=old, resolved_at=old)
    waiting = flag(flag_id=2, transaction_id=3, flagged_at=old)
    assert list(notes([answered, waiting])["kind"]) == [flags_domain.PENDING]


def test_an_arrival_shows_the_charge_and_no_reply():
    arrivals = pd.DataFrame(
        {
            "flag_id": [9], "transaction_id": [2], "resolved_at": ["2026-10-09 09:00:00"],
            "transaction_date": pd.to_datetime(["2026-09-02"]), "amount": [-60.0],
            "details": ["CANVA"], "purpose": ["Marketing"],
        }
    )
    result = notes(arrivals=arrivals, lines=(MARKETING,))
    assert list(result["kind"]) == [flags_domain.ARRIVED]
    assert result.iloc[0]["details"] == "CANVA"
    assert result.iloc[0]["reply"] == ""


def test_nothing_to_say_gives_an_empty_frame_with_the_right_columns():
    result = notes()
    assert result.empty
    assert list(result.columns) == flags_domain.NOTIFICATION_COLUMNS


def test_newest_first():
    first = flag(flag_id=1, transaction_id=2, status="resolved", resolved_at="2026-10-05 09:00:00")
    second = flag(flag_id=2, transaction_id=3, status="dismissed", resolved_at="2026-10-09 09:00:00")
    assert list(notes([first, second])["kind"]) == [flags_domain.KEPT, flags_domain.MOVED_OUT]


# --- The data layer ------------------------------------------------------------


def test_a_moved_charge_is_reported_to_the_committee_it_moved_into(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, CONSULTING)
    move(backend, flag_id, MARKETING)

    arrived = backend.fetch_flags_moved_into((MARKETING,))
    assert list(arrived["transaction_id"]) == [charge]


def test_the_arrival_carries_nothing_about_who_flagged_it_why_or_where_from(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    _, flag_id = _flagged(backend, CONSULTING)
    move(backend, flag_id, MARKETING)

    arrived = backend.fetch_flags_moved_into((MARKETING,))
    forbidden = {"flagged_by", "note", "resolution_note", "booked_to_committee_id", "resolved_by", "status"}
    assert forbidden.isdisjoint(arrived.columns)
    # And not smuggled into any cell either.
    flat = " ".join(arrived.astype(str).to_numpy().ravel())
    assert VP not in flat
    assert "Moved from" not in flat
    assert "Consulting" not in flat


def test_the_committee_it_left_and_uninvolved_ones_get_no_arrival(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    _, flag_id = _flagged(backend, CONSULTING)
    move(backend, flag_id, MARKETING)

    assert backend.fetch_flags_moved_into((CONSULTING,)).empty
    assert backend.fetch_flags_moved_into((MEMBERSHIP,)).empty
    assert backend.fetch_flags_moved_into(()).empty


def test_a_flag_that_is_open_or_dismissed_is_not_an_arrival(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    _, open_flag = _flagged(backend, CONSULTING)
    assert backend.fetch_flags_moved_into((MARKETING,)).empty  # still open

    assert backend.resolve_flag(open_flag, "dismissed", TREASURER_EMAIL, "Keep it.").ok
    assert backend.fetch_flags_moved_into((MARKETING,)).empty


def test_a_charge_moved_on_again_stops_being_an_arrival(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, CONSULTING)
    move(backend, flag_id, MARKETING)
    assert not backend.fetch_flags_moved_into((MARKETING,)).empty

    from ais_fmd.data.backend import TransactionChange

    backend.update_transactions(
        [TransactionChange(transaction_id=charge, purpose=None, budget_category=MEMBERSHIP)],
        TREASURER_EMAIL,
    )
    assert backend.fetch_flags_moved_into((MARKETING,)).empty


# --- The page ------------------------------------------------------------------


def test_both_committees_are_told_and_the_receiver_learns_nothing_extra(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    _, flag_id = _flagged(backend, CONSULTING)
    move(backend, flag_id, MARKETING)

    receiver = run_as(VIEWS / "Officer.py", OFFICER, MARKETING)
    assert_clean(receiver, "My Committee for the committee a charge moved into")
    text = page_text(receiver)
    assert "Moved into your committee" in text
    assert "Moved from" not in text          # the treasurer's reply names the source
    assert "Consulting" not in text          # neither does anything else on the page
    assert NOTE not in text                  # nor the other VP's reason

    sender = run_as(VIEWS / "Officer.py", OFFICER, CONSULTING)
    assert_clean(sender, "My Committee for the committee that flagged it")
    assert "Moved out of your committee" in page_text(sender)


def test_a_committee_with_nothing_to_report_sees_no_updates_box(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, MEMBERSHIP)
    assert_clean(app, "My Committee with no flag activity")
    assert "Updates" not in [str(header.value) for header in app.subheader]


def test_a_pending_flag_shows_as_waiting_on_my_committee(seeded_db, use_db):
    use_db(seeded_db)
    _flagged(SqliteBackend(), CONSULTING)
    app = run_as(VIEWS / "Officer.py", OFFICER, CONSULTING)
    assert_clean(app, "My Committee with a waiting flag")
    assert "Waiting on the treasurer" in page_text(app)


def test_recent_charges_is_gone_from_my_committee(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, CONSULTING)
    assert_clean(app, "My Committee")
    assert "Recent charges" not in [str(header.value) for header in app.subheader]
