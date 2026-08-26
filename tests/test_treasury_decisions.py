"""
The treasury rulings, asserted one by one.

Every test here exists because a human answered a question in
`docs/treasury-questions.md`, and each one names the answer it encodes. That
matters more than usual: these are not properties derived from the data, they
are *decisions*, and a decision can be reversed. When one is, the test that
fails should be the one that tells you which ruling you just contradicted --
not some incidental assertion three modules away.

Answered 2026-08-24. Still open, and deliberately untested here: the disputed
purpose mappings (5a), and whether a "headshot" memo means Professional
Development or Membership.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.domain import dues as dues_module
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.predicates import (
    DuesSchedule,
    DuesWindow,
    classify_deterministic,
    classify_exact,
    committee_from_memo,
)


def venmo(note: str, sender: str = "Ava Mitchell") -> str:
    return f"3421987654321098 | {note} | {sender} | UF AIS"


def zelle_from(name: str = "Jane Doe") -> str:
    return f"ZELLE FROM {name.upper()} ON 07/08 REF # A1"


def zelle_to(name: str = "John Smith") -> str:
    return f"ZELLE TO {name.upper()} ON 07/09 REF # A2"


def row(details: str, amount: float, when: str = "2026-09-10") -> dict:
    return {
        "amount": amount,
        "details": details,
        "account": "Venmo",
        "transaction_date": when,
    }


FALL_2026 = DuesWindow(
    term_id="FA26",
    semester="Fall 2026",
    start=date(2026, 8, 15),
    end=date(2026, 12, 18),
    rates=(Decimal("50.00"), Decimal("65.00")),
    verified=True,
)


# --- Q1: per-term dues rates -------------------------------------------------
#
# "Last term was 35 early and 52.5 late. This term it's 50 and 65."

def test_fall_2026_rates_are_recorded_and_confirmed():
    assert dues_module.CONFIRMED_DUES_RATES["Fall 2026"] == (
        Decimal("50.00"),
        Decimal("65.00"),
    )


def test_the_new_rates_are_recognised_as_dues():
    """
    The failure this fixes was silent.

    Dues match on an exact amount. Fall 2026 began on 2026-08-15 charging $50
    and $65 while the app still assumed $35/$52.50, so every dues payment of the
    new term fell through to the review queue and dues income read as collapsed.
    """
    schedule = DuesSchedule([FALL_2026])
    for amount in (50.00, 65.00):
        result = classify_exact(row(zelle_from(), amount), dues=schedule)
        assert result.committee_id == 1, f"${amount} should be dues in Fall 2026"


def test_the_old_rates_stop_being_dues_once_the_term_changed():
    """A rate that is no longer charged must not keep matching."""
    schedule = DuesSchedule([FALL_2026])
    assert classify_exact(row(zelle_from(), 35.00), dues=schedule).committee_id != 1


def test_earlier_terms_are_left_unconfirmed_on_purpose():
    """
    Treasury answered for two terms, not for nine.

    Spring 2025 carried both rate pairs in the real data and nobody has said
    which counted. Recording a guess here would move about $1,870 of income, so
    the absence is the point -- `quality.check_unverified_dues_rates` keeps
    reporting the rest until someone confirms them.
    """
    assert set(dues_module.CONFIRMED_DUES_RATES) == {"Spring 2026", "Fall 2026"}
    assert "Spring 2025" not in dues_module.CONFIRMED_DUES_RATES


# --- Q1 fallout: the amount collision treasury warned about ------------------
#
# "There are also going to be some formal dues that might be around those
# amounts but the actual dues are a very specific amount."

def test_a_formal_memo_beats_a_matching_dues_amount():
    """$50 is both the Fall 2026 dues rate and a plausible formal ticket."""
    contested = row(venmo("spring formal ticket"), 50.00)
    assert classify_exact(contested, dues=DuesSchedule([FALL_2026])).committee_id == 18


def test_the_same_amount_without_a_memo_is_still_dues():
    """The memo breaks the tie; it does not disable the amount rule."""
    plain = row(zelle_from(), 50.00)
    assert classify_exact(plain, dues=DuesSchedule([FALL_2026])).committee_id == 1


def test_a_dues_memo_books_an_off_schedule_amount():
    """
    Treasury asked for a dues memo keyword "in addition" to the amount rule.

    $36 is not a Fall 2026 rate, so the schedule rejects it. The payer calling
    it dues is enough.
    """
    odd = row(zelle_from() + " fall dues", 36.00)
    result = classify_exact(odd, dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1
    assert "36.00" in result.rule, "the odd amount belongs in the reason"


def test_the_schedule_still_gets_first_refusal():
    """
    "In addition" means after, not instead.

    A row the schedule recognises must keep the schedule's reasoning, naming the
    term it matched. If the memo rule ran first it would shadow that on every
    dues row that happens to carry the word.
    """
    on_rate = row(zelle_from() + " dues", 50.00)
    result = classify_exact(on_rate, dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1
    assert "Fall 2026" in result.rule, "the schedule should have claimed this row"


def test_a_dues_memo_does_not_outrank_a_formal_memo():
    """The collision treasury warned about survives the addition."""
    both = row(venmo("formal ticket - dues"), 50.00)
    assert classify_exact(both, dues=DuesSchedule([FALL_2026])).committee_id == 18


def test_an_outgoing_dues_memo_is_not_income():
    """A negative transfer memoed "dues" is a refund, which is a human's call."""
    refund = row(zelle_to() + " dues refund", -50.00)
    assert classify_exact(refund, dues=DuesSchedule([FALL_2026])).committee_id != 1


FA26_TERMS = pd.DataFrame(
    [
        {
            "TermID": "FA26",
            "Semester": "Fall 2026",
            "start_date": "2026-08-01",
            "end_date": "2026-12-18",
            "dues_rates": "50.00,65.00",
            "dues_rates_verified": 1,
        }
    ]
)


def _off_rate_rows(memo: str, amount: float = 45.00, n: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "TransactionID": i,
                "transaction_date": "2026-09-12",
                "details": f"ZELLE FROM MEMBER{i} ON 09/12 REF # A{i} {memo}".strip(),
                "amount": amount,
                "account": "Wells Fargo",
            }
            for i in range(n)
        ]
    )


def test_off_rate_dues_are_still_reported_but_not_alarming():
    """
    Booking these must not silence the check, and must not shout either.

    Treasury: "Execs pay the discounted rate, some people pay less for other
    reasons." Off-rate dues are routine, so a HIGH alarm on every discount would
    train a treasurer to ignore this check and take the real signal down with
    it. They are already counted as income, so the finding is informational and
    points at the fix: add the rate to the term.
    """
    from ais_fmd.domain.quality import check_possible_dues_rate_change

    issue = check_possible_dues_rate_change(
        _off_rate_rows("fall dues"), pd.DataFrame(), FA26_TERMS
    )
    assert issue is not None
    assert issue.severity == "low"
    assert "already counted as dues income" in issue.detail


def test_an_unexplained_cluster_is_still_high():
    """
    The signal the check exists for, preserved.

    Several people sending one unrecognised amount with no memo saying dues is
    what an unrecorded rate change looks like from inside the data.
    """
    from ais_fmd.domain.quality import check_possible_dues_rate_change

    issue = check_possible_dues_rate_change(
        _off_rate_rows(""), pd.DataFrame(), FA26_TERMS
    )
    assert issue is not None
    assert issue.severity == "high"


def test_one_unexplained_row_keeps_the_cluster_high():
    """A mixed cluster is not explained; the quiet reading must not win."""
    from ais_fmd.domain.quality import check_possible_dues_rate_change

    rows = pd.concat(
        [_off_rate_rows("fall dues", n=3), _off_rate_rows("", n=1)], ignore_index=True
    )
    rows["TransactionID"] = range(len(rows))
    issue = check_possible_dues_rate_change(rows, pd.DataFrame(), FA26_TERMS)
    assert issue is not None
    assert issue.severity == "high"


# --- Q3: reimbursements belong to the committee they repaid ------------------
#
# "Reimbursements should go to the committee they represent. If someone spends
# money on a personal card and gets reimbursed then it was a committee
# expenditure."

def test_an_outgoing_transfer_is_no_longer_booked_to_refunded():
    result = classify_deterministic(row(zelle_to(), -45.00))
    assert result.committee_id is None, "17/Refunded is no longer the default"


def test_an_unresolved_reimbursement_explains_itself_to_the_queue():
    """
    A row with no answer still has to carry a question.

    The review queue renders `match_rule`. Left as UNMATCHED these rows would
    show an empty cell, which is indistinguishable from a parse failure -- and
    working this queue is the single highest-value thing anyone can do with the
    app right now.
    """
    run = categorize_records([row(zelle_to(), -45.00)])
    rule = run.classifications[0].rule
    assert rule, "an unresolved reimbursement must not be a blank cell"
    assert "committee" in rule.lower()


@pytest.mark.parametrize(
    "memo, committee",
    [
        ("reimbursing the hoodie order", 13),
        ("road trip gas", 14),
        ("formal deposit", 18),
    ],
)
def test_a_reimbursement_takes_the_committee_its_memo_names(memo, committee):
    assert classify_deterministic(row(venmo(memo), -120.00)).committee_id == committee


def test_reimbursed_spend_now_lands_in_a_budgeted_committee():
    """
    The accounting consequence, stated as a test.

    17 (Refunded) is `kind="ledger"` and sits outside budget-vs-actual, so under
    the old rule every reimbursed dollar was invisible to the budget of the
    committee that spent it. Committees 13/14/18 are all `kind="committee"`.
    """
    from ais_fmd.config.categories import COMMITTEE_BY_ID

    result = classify_deterministic(row(venmo("crewneck order"), -300.00))
    assert COMMITTEE_BY_ID[result.committee_id].kind == "committee"


def test_a_merchant_return_is_still_a_refund():
    """
    Money coming back is not a reimbursement.

    17 keeps its meaning for the incoming direction, which is why the rule was
    renamed rather than deleted.
    """
    from ais_fmd.domain.categorize import bulk

    rows = [
        {
            "details": "PURCHASE RETURN AUTHORIZED ON 04/12 AMAZON.COM",
            "amount": 37.39,
            "transaction_date": "2026-04-12",
            "account": "Wells Fargo",
        }
    ]
    assert bulk.propose(bulk.group_residual(rows)[0]).committee_id == 17


# --- Q4: Venmo, deprecated going forward, counted net historically -----------
#
# "Venmo will now be depricated and no dues or money will come from it. For
# venmo it should still be counted historically taking into account those fees
# (if accurate)."

def test_historical_venmo_dues_are_counted_net_of_fees():
    terms = pd.DataFrame(
        [
            {
                "TermID": "X",
                "Semester": "Test",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "dues_rates": "25.00,30.00,40.00",
                "dues_rates_verified": 1,
            }
        ]
    )
    schedule = dues_module.schedule_from_terms(terms)
    assert schedule.accept_venmo_net, "treasury enabled this"
    for net in ("24.43", "29.34", "39.14"):
        assert schedule.matches(Decimal(net), date(2026, 3, 1), is_venmo=True)


def test_zelle_is_untouched_by_the_venmo_window():
    """Only Venmo took a cut, and Zelle is what remains in use."""
    terms = pd.DataFrame(
        [
            {
                "TermID": "X",
                "Semester": "Test",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "dues_rates": "25.00",
                "dues_rates_verified": 1,
            }
        ]
    )
    schedule = dues_module.schedule_from_terms(terms)
    assert not schedule.matches(Decimal("24.43"), date(2026, 3, 1), is_venmo=False)


def test_the_fee_window_still_refuses_amounts_it_cannot_justify():
    """
    "If accurate" is honoured, not assumed away.

    The observed net amounts do not fit one fee formula to the cent, so this is
    a bounded window below gross rather than a reconstructed schedule. It must
    not swallow an unrelated amount that merely happens to sit nearby.
    """
    terms = pd.DataFrame(
        [
            {
                "TermID": "X",
                "Semester": "Test",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "dues_rates": "25.00,40.00",
                "dues_rates_verified": 1,
            }
        ]
    )
    schedule = dues_module.schedule_from_terms(terms)
    when = date(2026, 3, 1)
    assert not schedule.matches(Decimal("20.00"), when, is_venmo=True)
    assert not schedule.matches(Decimal("33.00"), when, is_venmo=True)
    # A fee only ever reduces what arrives.
    assert not schedule.matches(Decimal("41.00"), when, is_venmo=True)


# --- Q5b: memos are enough to book a transfer --------------------------------
#
# "Absolutely look at the memos they will clarify it well."

@pytest.mark.parametrize(
    "memo, committee",
    [
        ("crewneck", 13),
        ("hoodie", 13),
        ("t-shirt order", 13),
        ("merch", 13),
        ("road trip", 14),
        ("roadtrip st augustine", 14),
        ("st. augustine", 14),
        ("semi formal", 18),
        ("formal", 18),
    ],
)
def test_incoming_payments_are_booked_from_their_memo(memo, committee):
    assert classify_deterministic(row(venmo(memo), 45.00)).committee_id == committee


def test_a_payer_name_still_teaches_nothing():
    """
    The distinction that makes memo rules safe.

    A rule keyed on a member's name would mis-categorise everything that member
    ever pays. A memo travels with one payment, so it carries no such risk --
    which is why memos are used and names still are not.
    """
    from ais_fmd.domain.categorize.merchants import merchant_key

    assert not merchant_key(zelle_from("Jane Doe"))


@pytest.mark.parametrize("memo", ["headshot", "headshots for linkedin"])
def test_headshots_are_left_for_a_human(memo):
    """
    Professional Development or Membership -- treasury did not say.

    Both are defensible and a keyword cannot choose. Six rows, $35 total; the
    cost of leaving them in the queue is far below the cost of guessing.
    """
    assert committee_from_memo(memo) is None


def test_bare_ticket_names_no_committee():
    """"Ticket" appears on formal, road trip and event payments alike."""
    assert committee_from_memo("ticket payment") is None


# --- Q2: the card roster turns over with the officers ------------------------
#
# "The cards might change around with new VPs, I will input their numbers later
# go with your best guess now based on everything I have sent historically."

def test_no_card_was_guessed_from_the_apps_own_output():
    """
    The best guess available is "no guess", and that is the honest answer.

    The only current-era evidence about the seven undocumented cards is this
    app's own `budget_category`, written by `categorize_frame` at import time.
    Deriving a roster from it would train the categorizer on its own output and
    harden its mistakes into evidence -- the one thing `scoring.py` says never
    to do. The 2022-23 human labels are a different cohort with different cards.
    """
    from ais_fmd.domain.categorize.scoring import INFERRED_CARDS

    assert INFERRED_CARDS == {}


def test_spend_after_the_cohort_ended_is_flagged():
    """
    A reissued card keeps voting, just for the wrong committee.

    Nothing in a bank statement distinguishes a card that changed hands, so the
    only way this surfaces is by noticing the roster is older than the spend.
    """
    from ais_fmd.domain.quality import check_card_roster_era

    transactions = pd.DataFrame(
        [
            {
                "TransactionID": 1,
                "transaction_date": "2026-09-20",  # Fall 2026: new cohort
                "details": "PURCHASE AUTHORIZED ON 09/19 PUBLIX GAINESVILLE FL CARD 8313",
                "amount": -84.10,
                "budget_category": 5,
            }
        ]
    )
    issue = check_card_roster_era(transactions, pd.DataFrame(), pd.DataFrame())
    assert issue is not None
    assert issue.count == 1
    assert "8408" in issue.detail, "the certainty rule is the one to warn about"


def test_spend_inside_the_cohort_is_not_flagged():
    from ais_fmd.domain.quality import check_card_roster_era

    transactions = pd.DataFrame(
        [
            {
                "TransactionID": 1,
                "transaction_date": "2026-03-04",  # still the 2024-2026 cohort
                "details": "PURCHASE AUTHORIZED ON 03/03 PUBLIX GAINESVILLE FL CARD 8313",
                "amount": -84.10,
                "budget_category": 5,
            }
        ]
    )
    assert check_card_roster_era(transactions, pd.DataFrame(), pd.DataFrame()) is None


def test_a_completed_handover_is_reported_differently():
    """
    What the real Fall 2026 statement actually looked like.

    Not one card in it appears on the roster — cards 3466 and 3526 against a
    roster of 3568/5718/8313/8408. Nothing is mis-booked, because an unknown
    card contributes no evidence; the card signal is simply inert. That is
    quieter than a wrong assignment and easier to miss, so it gets its own
    message rather than silence.
    """
    from ais_fmd.domain.quality import check_card_roster_era

    transactions = pd.DataFrame(
        [
            {
                "TransactionID": 1,
                "transaction_date": "2026-08-17",
                "details": "PURCHASE AUTHORIZED ON 08/15 TST*MACDINTONS GAI CARD 3526",
                "amount": -298.96,
                "budget_category": 5,
            },
            {
                "TransactionID": 2,
                "transaction_date": "2026-08-10",
                "details": "PURCHASE AUTHORIZED ON 08/09 US MOBILE CARD 3466",
                "amount": -96.00,
                "budget_category": None,
            },
        ]
    )
    issue = check_card_roster_era(transactions, pd.DataFrame(), pd.DataFrame())
    assert issue is not None
    assert issue.count == 2
    assert "3466" in issue.detail and "3526" in issue.detail
    assert "no card" in issue.title.lower()


def test_transfers_without_a_card_are_not_a_roster_problem():
    """Zelle rows carry no card, so they say nothing either way."""
    from ais_fmd.domain.quality import check_card_roster_era

    transactions = pd.DataFrame(
        [
            {
                "TransactionID": 1,
                "transaction_date": "2026-08-19",
                "details": "ZELLE FROM JANE DOE ON 08/19 REF # A1 AIS DUES",
                "amount": 50.00,
                "budget_category": 1,
            }
        ]
    )
    assert check_card_roster_era(transactions, pd.DataFrame(), pd.DataFrame()) is None
