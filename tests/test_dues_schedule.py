"""
Per-term dues rates.

`rule_dues` matched an *exact* amount against a module constant pinned to Fall
2024. The VP Treasury Handbook shows the rate changing nearly every term, so the
first term after a change stopped categorizing dues entirely -- silently, with no
error, the payments just fell to the review queue.

The tests that matter here are the two ends of that:

  * `test_seeded_schedule_changes_nothing` -- moving rates into data must not
    move a single dollar. It is checked against the real 892-row extract in
    `scripts/verify_dues_schedule.py`; here it is checked in miniature.
  * `test_untold_rate_change_loses_every_dues_row` -- the failure being fixed,
    asserted so nobody reintroduces it by "simplifying" the schedule away.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.domain import dues as dues_module
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.predicates import (
    DUES_AMOUNTS,
    DuesSchedule,
    DuesWindow,
    record_date,
    rule_dues,
)
from ais_fmd.domain.quality import (
    check_possible_dues_rate_change,
    check_unverified_dues_rates,
)

FA26 = DuesWindow("FA26", "Fall 2026", date(2026, 8, 15), date(2026, 12, 20),
                  (Decimal("40.00"), Decimal("60.00")), verified=True)
SP26 = DuesWindow("SP26", "Spring 2026", date(2026, 1, 5), date(2026, 5, 15),
                  (Decimal("35.00"), Decimal("52.50")), verified=True)


def zelle(amount: float, when: str) -> dict:
    return {
        "details": "ZELLE FROM JANE DOE ON 07/08 REF # ABC123",
        "amount": amount,
        "transaction_date": when,
        "account": "Wells Fargo",
    }


def venmo(amount: float, when: str) -> dict:
    return {
        "details": "1234567890 | dues | Jane Doe | AIS",
        "amount": amount,
        "transaction_date": when,
        "account": "Venmo",
    }


# --- The fallback must be exactly the old behaviour --------------------------

def test_no_schedule_uses_the_original_constant():
    assert rule_dues(zelle(35.00, "2026-03-01")) is not None
    assert rule_dues(zelle(52.50, "2026-03-01")) is not None
    assert rule_dues(zelle(40.00, "2026-03-01")) is None


def test_empty_schedule_falls_back_to_the_constant():
    schedule = DuesSchedule()
    assert not schedule
    assert schedule.rates_for(date(2026, 3, 1)) == DUES_AMOUNTS


def test_date_outside_every_term_falls_back_rather_than_failing():
    """A row dated outside all terms must not silently stop being dues."""
    schedule = DuesSchedule([SP26])
    assert schedule.rates_for(date(2030, 1, 1)) == DUES_AMOUNTS
    assert rule_dues(zelle(35.00, "2030-01-01"), schedule) is not None


def test_seeded_schedule_changes_nothing():
    """Every term seeded with the old constant reproduces the old answers."""
    seeded = DuesSchedule([
        DuesWindow("SP26", "Spring 2026", date(2026, 1, 5), date(2026, 5, 15), DUES_AMOUNTS),
        DuesWindow("FA26", "Fall 2026", date(2026, 8, 15), date(2026, 12, 20), DUES_AMOUNTS),
    ])
    rows = [zelle(35.00, "2026-03-01"), zelle(52.50, "2026-09-20"),
            zelle(40.00, "2026-09-20"), zelle(-35.00, "2026-03-01")]
    before = categorize_records(rows)
    after = categorize_records(rows, dues=seeded)
    assert [c.committee_id for c in before.classifications] == \
           [c.committee_id for c in after.classifications]


# --- The failure this exists to prevent --------------------------------------

def test_untold_rate_change_loses_every_dues_row():
    """The time bomb: rates change, nobody updates the app, dues vanish."""
    payments = [zelle(40.00, "2026-09-20"), zelle(60.00, "2026-09-20")]
    stale = categorize_records(payments)
    assert all(c.committee_id is None for c in stale.classifications)


def test_updating_the_term_recovers_them():
    payments = [zelle(40.00, "2026-09-20"), zelle(60.00, "2026-09-20")]
    fixed = categorize_records(payments, dues=DuesSchedule([SP26, FA26]))
    assert all(c.committee_id == 1 for c in fixed.classifications)


def test_rates_are_scoped_to_their_own_term():
    schedule = DuesSchedule([SP26, FA26])
    # 40.00 is a Fall 2026 rate; in Spring 2026 it is not dues.
    assert rule_dues(zelle(40.00, "2026-09-20"), schedule) is not None
    assert rule_dues(zelle(40.00, "2026-03-01"), schedule) is None
    assert rule_dues(zelle(35.00, "2026-03-01"), schedule) is not None
    assert rule_dues(zelle(35.00, "2026-09-20"), schedule) is None


def test_the_reason_names_the_term():
    result = rule_dues(zelle(40.00, "2026-09-20"), DuesSchedule([SP26, FA26]))
    assert "Fall 2026" in result.rule


# --- Venmo net-of-fee, opt-in ------------------------------------------------

@pytest.mark.parametrize("net", ["24.43", "29.34", "39.14"])
def test_the_primitive_stays_strict_by_default(net):
    """
    `DuesSchedule` itself does not decide policy.

    Treasury enabled net-of-fee matching, but that decision is recorded once at
    `dues.schedule_from_terms` (see `ACCEPT_VENMO_NET_OF_FEES`), not baked into
    the matcher. Keeping the primitive strict is what lets a caller reconstruct
    the pre-decision behaviour -- which `verify_dues_schedule.py` needs in order
    to diff one against the other.
    """
    window = DuesWindow("X", "Test", date(2026, 1, 1), date(2026, 12, 31),
                        (Decimal("25.00"), Decimal("30.00"), Decimal("40.00")))
    strict = DuesSchedule([window])
    assert not strict.matches(Decimal(net), date(2026, 3, 1), is_venmo=True)


@pytest.mark.parametrize("net", ["24.43", "29.34", "39.14"])
def test_venmo_net_amounts_accepted_when_enabled(net):
    """The three amounts actually observed in the historical ledgers."""
    window = DuesWindow("X", "Test", date(2026, 1, 1), date(2026, 12, 31),
                        (Decimal("25.00"), Decimal("30.00"), Decimal("40.00")))
    lenient = DuesSchedule([window], accept_venmo_net=True)
    assert lenient.matches(Decimal(net), date(2026, 3, 1), is_venmo=True)


def test_venmo_window_does_not_swallow_unrelated_amounts():
    window = DuesWindow("X", "Test", date(2026, 1, 1), date(2026, 12, 31),
                        (Decimal("25.00"), Decimal("40.00")))
    lenient = DuesSchedule([window], accept_venmo_net=True)
    when = date(2026, 3, 1)
    assert not lenient.matches(Decimal("20.00"), when, is_venmo=True)
    assert not lenient.matches(Decimal("33.00"), when, is_venmo=True)
    # A fee only ever reduces the amount; above gross is never dues-by-fee.
    assert not lenient.matches(Decimal("41.00"), when, is_venmo=True)


def test_zelle_never_gets_the_fee_window():
    """Only Venmo takes a cut. Zelle arrives whole."""
    window = DuesWindow("X", "Test", date(2026, 1, 1), date(2026, 12, 31),
                        (Decimal("25.00"),))
    lenient = DuesSchedule([window], accept_venmo_net=True)
    assert not lenient.matches(Decimal("24.43"), date(2026, 3, 1), is_venmo=False)


def test_venmo_row_end_to_end_when_enabled():
    window = DuesWindow("X", "Test", date(2026, 1, 1), date(2026, 12, 31),
                        (Decimal("25.00"),))
    assert rule_dues(venmo(24.43, "2026-03-01"), DuesSchedule([window])) is None
    assert rule_dues(
        venmo(24.43, "2026-03-01"), DuesSchedule([window], accept_venmo_net=True)
    ) is not None


# --- Parsing the stored column -----------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("35.00,52.50", (Decimal("35.00"), Decimal("52.50"))),
        (" $35 ; 52.50 , ", (Decimal("35"), Decimal("52.50"))),
        ("", ()),
        (None, ()),
        ("not a number", ()),
        ("35.00,35.00", (Decimal("35.00"),)),
    ],
)
def test_parse_rates(raw, expected):
    assert dues_module.parse_rates(raw) == expected


def test_parse_rates_tolerates_nan():
    assert dues_module.parse_rates(float("nan")) == ()


def test_format_rates_round_trips():
    assert dues_module.format_rates(dues_module.parse_rates("52.5,35")) == "35.00,52.50"


def test_schedule_from_terms_skips_terms_without_rates():
    terms = pd.DataFrame([
        {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-05",
         "end_date": "2026-05-15", "dues_rates": "35.00,52.50", "dues_rates_verified": 1},
        {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15",
         "end_date": "2026-12-20", "dues_rates": None, "dues_rates_verified": 0},
    ])
    schedule = dues_module.schedule_from_terms(terms)
    assert [w.term_id for w in schedule.windows] == ["SP26"]
    assert schedule.rates_for(date(2026, 9, 20)) == DUES_AMOUNTS  # falls back


def test_schedule_from_terms_without_the_column():
    """A database created before the migration must not break the page."""
    terms = pd.DataFrame([
        {"TermID": "SP26", "Semester": "Spring 2026",
         "start_date": "2026-01-05", "end_date": "2026-05-15"},
    ])
    schedule = dues_module.schedule_from_terms(terms)
    assert not schedule
    assert schedule.rates_for(date(2026, 3, 1)) == DUES_AMOUNTS


# --- record_date -------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-03-01", date(2026, 3, 1)),
        ("2026-03-01 00:00:00", date(2026, 3, 1)),
        (date(2026, 3, 1), date(2026, 3, 1)),
        (pd.Timestamp("2026-03-01"), date(2026, 3, 1)),
        (None, None),
        ("", None),
        ("garbage", None),
    ],
)
def test_record_date(value, expected):
    assert record_date({"transaction_date": value}) == expected


# --- The quality checks ------------------------------------------------------

def test_unverified_rates_are_reported():
    terms = pd.DataFrame([
        {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-05",
         "end_date": "2026-05-15", "dues_rates": "35.00,52.50", "dues_rates_verified": 0},
    ])
    issue = check_unverified_dues_rates(pd.DataFrame(), pd.DataFrame(), terms)
    assert issue is not None and issue.count == 1
    assert "Spring 2026" in issue.detail


def test_verified_rates_are_silent():
    terms = pd.DataFrame([
        {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-05",
         "end_date": "2026-05-15", "dues_rates": "35.00,52.50", "dues_rates_verified": 1},
    ])
    assert check_unverified_dues_rates(pd.DataFrame(), pd.DataFrame(), terms) is None


def _terms_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15",
         "end_date": "2026-12-20", "dues_rates": "35.00,52.50", "dues_rates_verified": 1},
    ])


def test_rate_change_alarm_fires_on_a_cluster():
    """Several people paying the same unrecognised amount is what a rate looks like."""
    rows = pd.DataFrame([zelle(45.00, "2026-09-20") for _ in range(4)])
    issue = check_possible_dues_rate_change(rows, pd.DataFrame(), _terms_frame())
    assert issue is not None
    assert issue.count == 4
    assert "45.00" in issue.detail


def test_rate_change_alarm_ignores_a_one_off():
    rows = pd.DataFrame([zelle(45.00, "2026-09-20"), zelle(17.31, "2026-09-21")])
    assert check_possible_dues_rate_change(rows, pd.DataFrame(), _terms_frame()) is None


def test_rate_change_alarm_ignores_recognised_rates():
    rows = pd.DataFrame([zelle(35.00, "2026-09-20") for _ in range(6)])
    assert check_possible_dues_rate_change(rows, pd.DataFrame(), _terms_frame()) is None


def test_rate_change_alarm_ignores_card_purchases():
    """Only incoming transfers can be dues."""
    rows = pd.DataFrame([
        {"details": "PURCHASE AUTHORIZED ON 09/20 PUBLIX CARD 8313",
         "amount": 45.00, "transaction_date": "2026-09-20", "account": "Wells Fargo"}
        for _ in range(5)
    ])
    assert check_possible_dues_rate_change(rows, pd.DataFrame(), _terms_frame()) is None


def test_rate_change_alarm_ignores_outgoing():
    rows = pd.DataFrame([zelle(-45.00, "2026-09-20") for _ in range(5)])
    assert check_possible_dues_rate_change(rows, pd.DataFrame(), _terms_frame()) is None
