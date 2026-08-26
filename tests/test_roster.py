"""
Module M21 -- roster parsing and dues reconciliation.

Every fixture in here is a real pattern from the Fall 2026 statement, with the
names changed. That matters more than usual: the whole module exists because
naive `payer == member` matching fails on real bank data in three specific ways,
and a test suite built from tidy invented names would pass while the feature
stayed useless.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain.roster import (
    Member,
    extract_memo,
    match_key,
    member_from_name,
    normalize_name,
    parse_roster,
    reconcile,
    suggested_alias,
    unpaid_frame,
)


def zelle(payer: str, memo: str = "", ref: str = "BACX1234") -> str:
    tail = f" {memo}" if memo else ""
    return f"ZELLE FROM {payer.upper()} ON 08/19 REF # {ref}{tail}"


def dues_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"details": d, "amount": a, "transaction_date": "2026-08-19"}
            for d, a in rows
        ]
    )


def roster(*names: str) -> list[Member]:
    return [m for m in (member_from_name(n) for n in names) if m is not None]


# --- Name normalization ------------------------------------------------------

@pytest.mark.parametrize(
    "a, b",
    [
        ("SCHUCK JOHN", "John Schuck"),          # Wells Fargo emits both orders
        ("ALZAMORA ALEJANDRA", "Alejandra Alzamora"),
        ("Doe, Jane", "Jane Doe"),               # sheet exports often use "Last, First"
        ("  Maria   Ilieva ", "maria ilieva"),
        ("José Peña", "Jose Pena"),              # accents differ between systems
        ("Robert Hosay Jr", "Robert Hosay"),     # suffix on one side only
        ("Beth A McNamara", "Beth McNamara"),    # middle initial on one side only
    ],
)
def test_names_that_must_compare_equal(a, b):
    assert normalize_name(a) == normalize_name(b)


def test_different_people_do_not_collide():
    assert normalize_name("Nicolas Sanders") != normalize_name("Nicolas Sandoval")
    assert normalize_name("Kate McNamara") != normalize_name("Beth McNamara")


def test_noise_words_are_stripped():
    """"AIS DUES FOR ALEJANDRA ALZAMORA" has to reduce to the name."""
    assert normalize_name("AIS DUES FOR ALEJANDRA ALZAMORA") == normalize_name(
        "Alejandra Alzamora"
    )
    assert normalize_name("SIA RAJPUT MS ISOM DATA SCIENCE FALL 2026") == normalize_name(
        "Sia Rajput"
    )


def test_a_name_that_is_all_noise_normalises_to_nothing():
    assert normalize_name("AIS DUES FALL 2026") == ""
    assert member_from_name("AIS DUES") is None


def test_match_key_and_normalize_agree():
    """The stored key and the matching rule must not drift apart."""
    assert match_key("Schuck John") == normalize_name("John Schuck")


# --- Memo extraction ---------------------------------------------------------

def test_memo_is_sliced_after_the_reference():
    assert extract_memo(zelle("Beth McNamara", "KATE MCNAMARA DUES")) == "KATE MCNAMARA DUES"


def test_no_memo_returns_empty_not_the_payer():
    """
    The distinction the whole matcher rests on.

    If a missing memo returned the row text, every payment would appear to name
    its own sender and "on behalf of" could never be seen.
    """
    assert extract_memo(zelle("Cameryn Weitz")) == ""


def test_venmo_note_is_read_from_the_pipe_format():
    assert extract_memo("3421987654321098 | hoodie money | Ava Mitchell | UF AIS") == (
        "hoodie money"
    )


# --- Matching ----------------------------------------------------------------

def test_payer_is_credited_when_the_memo_names_nobody():
    members = roster("Cameryn Weitz")
    result = reconcile(dues_frame([(zelle("Cameryn Weitz", "AIS DUES"), 50.0)]), members)
    assert result.summary()["paid"] == 1
    assert not result.matches[0].on_behalf_of


def test_reversed_name_order_still_matches():
    """Wells Fargo wrote "SCHUCK JOHN"; the roster says "John Schuck"."""
    members = roster("John Schuck")
    result = reconcile(dues_frame([(zelle("Schuck John", "AIS DUES"), 50.0)]), members)
    assert result.summary()["paid"] == 1


def test_a_memo_naming_another_member_credits_that_member():
    members = roster("Nicolas Sanders", "Charlie Andrews")
    result = reconcile(
        dues_frame([(zelle("Nicolas Sanders", "CHARLIE ANDREWS DUES"), 50.0)]), members
    )
    credited = result.matches[0]
    assert credited.member.full_name == "Charlie Andrews"
    assert credited.on_behalf_of
    assert [m.full_name for m in result.unpaid] == ["Nicolas Sanders"]


def test_a_shared_surname_does_not_break_the_beneficiary():
    """
    The commonest on-behalf case: a parent paying for a student.

    An earlier implementation subtracted the payer's tokens from the row before
    searching it, which deleted the shared surname and left Kate unmatched.
    """
    members = roster("Kate McNamara")
    result = reconcile(
        dues_frame([(zelle("Beth McNamara", "KATE MCNAMARA DUES"), 50.0)]), members
    )
    assert result.matches[0].member.full_name == "Kate McNamara"
    assert not result.unpaid


def test_one_payer_can_cover_several_members():
    members = roster("Nicolas Sanders", "Charlie Andrews", "Jayme Rudds")
    result = reconcile(
        dues_frame(
            [
                (zelle("Nicolas Sanders", "NICOLAS SANDERS DUES", "R1"), 50.0),
                (zelle("Nicolas Sanders", "CHARLIE ANDREWS DUES", "R2"), 50.0),
                (zelle("Nicolas Sanders", "JAYME RUDDS DUES", "R3"), 35.0),
            ]
        ),
        members,
    )
    assert result.summary()["paid"] == 3
    assert not result.unpaid
    assert result.summary()["collected"] == 135.0


def test_paying_for_yourself_is_not_on_behalf_of():
    members = roster("Nicolas Sanders")
    result = reconcile(
        dues_frame([(zelle("Nicolas Sanders", "NICOLAS SANDERS DUES"), 50.0)]), members
    )
    assert not result.matches[0].on_behalf_of


def test_a_payment_naming_nobody_on_the_roster_is_unmatched():
    """
    Unmatched is a result, not a failure.

    No fuzzy matching: crediting the wrong person's dues is worse than asking a
    human, because the error is invisible and the member gets chased anyway.
    """
    members = roster("Cameryn Weitz")
    result = reconcile(dues_frame([(zelle("Somebody Else", "AIS DUES"), 50.0)]), members)
    assert result.summary()["unmatched_payments"] == 1
    assert result.summary()["unmatched_amount"] == 50.0
    assert result.summary()["paid"] == 0


def test_a_single_shared_token_is_not_a_match():
    """With a roster of hundreds, one common surname would claim other people's money."""
    members = roster("Kate McNamara", "Beth McNamara")
    result = reconcile(dues_frame([(zelle("Somebody", "MCNAMARA"), 50.0)]), members)
    assert result.summary()["unmatched_payments"] == 1


def test_a_memo_naming_two_members_is_refused_rather_than_guessed():
    """
    One payment, two members named, equally good fits -- a human decides.

    This is a real shape: somebody sends a single transfer covering a friend as
    well as themselves. Splitting it is a judgement about how much belongs to
    whom, so the matcher declines instead of crediting whichever sorted first.
    """
    members = roster("Alex Kim", "Sam Lee")
    result = reconcile(
        dues_frame([(zelle("Alex Kim", "DUES FOR ALEX KIM AND SAM LEE"), 100.0)]),
        members,
    )
    assert result.summary()["unmatched_payments"] == 1
    assert {m.full_name for m in result.unpaid} == {"Alex Kim", "Sam Lee"}


def test_the_longer_name_wins_over_a_prefix():
    members = roster("Kate", "Kate McNamara")
    result = reconcile(dues_frame([(zelle("Payer", "KATE MCNAMARA DUES"), 50.0)]), members)
    assert result.matches[0].member.full_name == "Kate McNamara"


# --- Reporting ---------------------------------------------------------------

# --- Preferred names ---------------------------------------------------------
#
# 34 of the 152 people on the Fall 2026 membership form go by a name other than
# their legal first name, and the bank data uses both forms freely.

def test_a_preferred_name_in_the_memo_finds_the_member():
    """The roster says "Katherine McNamara"; her payment memo says "KATE"."""
    df = pd.DataFrame(
        {"First Name": ["Katherine"], "Last Name": ["McNamara"], "Preferred Name": ["Kate"]}
    )
    members = parse_roster(df).members
    result = reconcile(
        dues_frame([(zelle("Beth McNamara", "KATE MCNAMARA DUES"), 50.0)]), members
    )
    assert result.matches[0].member.full_name == "Katherine McNamara"
    assert not result.unpaid


def test_a_preferred_name_that_is_already_a_full_name_still_works():
    """Some people type their whole name into the preferred-name box."""
    df = pd.DataFrame(
        {"First Name": ["Tyler"], "Last Name": ["Barnett"], "Preferred Name": ["Tyler Barnett"]}
    )
    members = parse_roster(df).members
    result = reconcile(dues_frame([(zelle("Tyler Barnett", "DUES"), 50.0)]), members)
    assert not result.unpaid


def test_the_legal_name_still_matches_when_a_preferred_name_exists():
    df = pd.DataFrame(
        {"First Name": ["Nicholas"], "Last Name": ["DeLise"], "Preferred Name": ["Nico"]}
    )
    members = parse_roster(df).members
    result = reconcile(dues_frame([(zelle("Nicholas L DeLise", "DUES"), 50.0)]), members)
    assert not result.unpaid


def test_preferred_name_is_never_used_as_the_identity():
    """
    The bug this guards was silent and severe.

    "Preferred Name" sat in the full-name column list and matched first. Only
    65 of 152 people filled it in, so the roster came out as 58 bare nicknames
    with no surnames, and two-thirds of the membership vanished -- while the
    import reported success.
    """
    df = pd.DataFrame(
        {
            "First Name": ["Abhiram", "Kenneth"],
            "Last Name": ["Moturi", "Ordonez"],
            "Preferred Name": ["Abhi", None],
        }
    )
    parsed = parse_roster(df)
    assert len(parsed.members) == 2
    assert {m.full_name for m in parsed.members} == {"Abhiram Moturi", "Kenneth Ordonez"}


# --- Self-certification ------------------------------------------------------

def test_members_who_say_they_paid_but_did_not_are_singled_out():
    df = pd.DataFrame(
        {
            "First Name": ["Says", "Quiet"],
            "Last Name": ["Paid", "Person"],
            "I certify that I have completed the dues transaction": [
                "Yes, I have already paid my dues via Zelle, or Cash transfer.",
                "",
            ],
        }
    )
    members = parse_roster(df).members
    result = reconcile(pd.DataFrame(), members)
    assert [m.full_name for m in result.disputed] == ["Says Paid"]
    assert result.summary()["unpaid"] == 2, "both are unpaid; only one is disputed"


# --- Near-miss suggestions ---------------------------------------------------

def test_a_shortened_first_name_is_suggested_not_credited():
    """"ZACKARY FLORENDO" against a roster reading "Zack Florendo"."""
    members = roster("Zack Florendo")
    result = reconcile(dues_frame([(zelle("Zackary Florendo"), 65.0)]), members)
    assert result.summary()["paid"] == 0, "must not auto-credit"
    suggestions = result.suggestions()
    assert len(suggestions) == 1
    payment, member = suggestions[0]
    assert member.full_name == "Zack Florendo"
    assert payment.amount == 65.0


def test_a_near_miss_in_a_memo_is_found_even_though_the_payer_was_credited():
    """
    The case that only a member-side search finds.

    "NICOLAS SANDERS ... JAYME RUDDS DUES" against a roster reading "Jayme
    Rudd": the plural stops the memo matching, so the payment falls back to
    crediting Nicolas and never appears in the unmatched list at all.
    """
    members = roster("Nicolas Sanders", "Jayme Rudd")
    result = reconcile(
        dues_frame(
            [
                (zelle("Nicolas Sanders", "NICOLAS SANDERS DUES", "R1"), 50.0),
                (zelle("Nicolas Sanders", "JAYME RUDDS DUES", "R2"), 35.0),
            ]
        ),
        members,
    )
    assert [m.full_name for m in result.unpaid] == ["Jayme Rudd"]
    suggestions = result.suggestions()
    assert len(suggestions) == 1
    assert suggestions[0][1].full_name == "Jayme Rudd"
    assert suggestions[0][0].amount == 35.0


# --- Confirming a suggestion --------------------------------------------------

def test_confirming_a_shortened_name_derives_the_payers_own_spelling():
    """The alias to record is what the payer actually typed, not the roster spelling."""
    members = roster("Zack Florendo")
    result = reconcile(dues_frame([(zelle("Zackary Florendo"), 65.0)]), members)
    payment, member = result.suggestions()[0]
    assert suggested_alias(payment, member) == "florendo zackary"


def test_confirming_a_memo_near_miss_ignores_the_noise_word():
    members = roster("Nicolas Sanders", "Jayme Rudd")
    result = reconcile(
        dues_frame(
            [
                (zelle("Nicolas Sanders", "NICOLAS SANDERS DUES", "R1"), 50.0),
                (zelle("Nicolas Sanders", "JAYME RUDDS DUES", "R2"), 35.0),
            ]
        ),
        members,
    )
    payment, member = result.suggestions()[0]
    alias = suggested_alias(payment, member)
    assert alias == "jayme rudds"
    assert "dues" not in alias, "the memo noise word must not end up in the alias"


def test_a_confirmed_alias_matches_directly_next_time():
    """The point of confirming: the next statement needs no suggestion at all."""
    zack = member_from_name("Zack Florendo")
    zack = Member(**{**zack.__dict__, "alt_keys": ("florendo zackary",)})
    result = reconcile(dues_frame([(zelle("Zackary Florendo"), 65.0)]), [zack])
    assert result.summary()["paid"] == 1
    assert not result.unpaid
    assert result.suggestions() == []


def test_a_shared_surname_alone_is_not_suggested():
    """"Eugene Wang" against "Yung Cheng Wang" -- only the surname agrees."""
    members = roster("Eugene Wang")
    result = reconcile(dues_frame([(zelle("Yung Cheng Wang"), 50.0)]), members)
    assert result.suggestions() == []


def test_a_two_letter_token_is_not_treated_as_a_prefix():
    """"Li" must not claim Liang, Lillian and Liu alike."""
    members = roster("Li Chen")
    result = reconcile(dues_frame([(zelle("Liang Chen"), 50.0)]), members)
    assert result.suggestions() == [], "'li' is too short to be evidence"


def test_an_ambiguous_suggestion_is_withheld():
    members = roster("Sam Smith")
    result = reconcile(
        dues_frame(
            [
                (zelle("Samuel Smith", "", "R1"), 50.0),
                (zelle("Sammy Smith", "", "R2"), 50.0),
            ]
        ),
        members,
    )
    assert result.suggestions() == [], "two candidates means a human decides"


def test_unpaid_list_is_the_chase_list():
    members = roster("Paid Person", "Unpaid Person")
    result = reconcile(dues_frame([(zelle("Paid Person", "DUES"), 50.0)]), members)
    frame = unpaid_frame(result)
    assert list(frame["Member"]) == ["Unpaid Person"]


def test_a_member_credited_twice_is_flagged():
    members = roster("Double Payer")
    result = reconcile(
        dues_frame(
            [
                (zelle("Double Payer", "DUES", "R1"), 50.0),
                (zelle("Double Payer", "DUES", "R2"), 50.0),
            ]
        ),
        members,
    )
    repeats = result.duplicates()
    assert len(repeats) == 1
    member, count, total = repeats[0]
    assert count == 2 and total == 100.0
    # Counted once as a person, twice as money.
    assert result.summary()["paid"] == 1
    assert result.summary()["collected"] == 100.0


def test_empty_payments_leave_everyone_unpaid():
    members = roster("Nobody Paid")
    result = reconcile(pd.DataFrame(), members)
    assert result.summary() == {
        "expected": 1,
        "paid": 0,
        "unpaid": 1,
        "rate": 0.0,
        "collected": 0.0,
        "unmatched_payments": 0,
        "unmatched_amount": 0.0,
        "on_behalf": 0,
        "disputed": 0,
    }


# --- Roster parsing ----------------------------------------------------------

def test_single_name_column():
    df = pd.DataFrame({"Full Name": ["Jane Doe", "John Roe"], "Email": ["j@x.edu", ""]})
    parsed = parse_roster(df)
    assert parsed.ok
    assert [m.full_name for m in parsed.members] == ["Jane Doe", "John Roe"]
    assert parsed.members[0].email == "j@x.edu"


def test_separate_first_and_last_columns():
    df = pd.DataFrame({"First Name": ["Jane"], "Last Name": ["Doe"]})
    parsed = parse_roster(df)
    assert parsed.ok
    assert parsed.members[0].full_name == "Jane Doe"


def test_columns_are_matched_by_label_not_position():
    """The mistake that made the original statement parser import 892 rows as $0.00."""
    df = pd.DataFrame({"Email": ["j@x.edu"], "UFID": ["1234"], "Name": ["Jane Doe"]})
    parsed = parse_roster(df)
    assert parsed.ok
    assert parsed.members[0].full_name == "Jane Doe"
    assert parsed.members[0].ufid == "1234"


def test_a_file_with_no_name_column_is_refused_with_the_columns_listed():
    df = pd.DataFrame({"Amount": [1], "Date": ["2026-08-19"]})
    parsed = parse_roster(df)
    assert not parsed.ok
    assert "Amount" in parsed.errors[0], "say what was actually found"


def test_duplicate_and_blank_names_are_skipped_not_counted():
    df = pd.DataFrame({"Name": ["Jane Doe", "Jane Doe", "", None, "John Roe"]})
    parsed = parse_roster(df)
    assert len(parsed.members) == 2
    assert parsed.skipped_rows == 3
    assert parsed.warnings


# --- Persistence (SqliteBackend) ---------------------------------------------

def test_replace_members_round_trips_aliases_and_the_paid_claim():
    """
    A stored roster must not lose what made matching work at import time.

    Aliases and the self-certification flag live only in the roster module's
    `Member` objects unless the backend actually stores them -- an earlier
    version of `replace_members` dropped both silently, which worked in every
    test because none of them reloaded from storage and checked.
    """
    from ais_fmd.data.sqlite_backend import SqliteBackend

    backend = SqliteBackend()
    backend.insert_term("FA26", "Fall 2026", "2026-08-01", "2026-12-18", "test@sandbox.local")

    member = Member(
        full_name="Katherine McNamara",
        match_key=match_key("Katherine McNamara"),
        alt_keys=("kate mcnamara",),
        preferred_name="Kate",
        claims_paid=True,
        email="k@example.edu",
        ufid="12345",
    )
    result = backend.replace_members(
        "FA26",
        [
            {
                "full_name": member.full_name,
                "match_key": member.match_key,
                "alt_keys": member.alt_keys,
                "preferred_name": member.preferred_name,
                "claims_paid": member.claims_paid,
                "email": member.email,
                "ufid": member.ufid,
            }
        ],
        "roster.csv",
        "treasurer@sandbox.local",
    )
    assert result.ok

    stored = backend.fetch_members("FA26")
    assert len(stored) == 1
    row = stored.iloc[0]
    assert row["alt_keys"] == "kate mcnamara"
    assert row["preferred_name"] == "Kate"
    assert int(row["claims_paid"]) == 1


def test_replace_members_replaces_rather_than_merges():
    from ais_fmd.data.sqlite_backend import SqliteBackend

    backend = SqliteBackend()
    backend.insert_term("FA26", "Fall 2026", "2026-08-01", "2026-12-18", "test@sandbox.local")

    first = [{"full_name": n, "match_key": match_key(n)} for n in ("Alice", "Bob")]
    backend.replace_members("FA26", first, "v1.csv", "actor")
    assert len(backend.fetch_members("FA26")) == 2

    second = [{"full_name": "Alice", "match_key": match_key("Alice")}]
    backend.replace_members("FA26", second, "v2.csv", "actor")
    remaining = backend.fetch_members("FA26")
    assert len(remaining) == 1, "Bob left; a merge would have kept him"
    assert remaining.iloc[0]["full_name"] == "Alice"


def test_add_member_alias_persists_and_is_idempotent():
    from ais_fmd.data.sqlite_backend import SqliteBackend

    backend = SqliteBackend()
    backend.insert_term("FA26", "Fall 2026", "2026-08-01", "2026-12-18", "test@sandbox.local")
    key = match_key("Zack Florendo")
    backend.replace_members(
        "FA26", [{"full_name": "Zack Florendo", "match_key": key}], "roster.csv", "actor"
    )

    result = backend.add_member_alias("FA26", key, "florendo zackary", "treasurer@sandbox.local")
    assert result.ok and result.updated == 1
    stored = backend.fetch_members("FA26").iloc[0]
    assert stored["alt_keys"] == "florendo zackary"

    # Confirming the same alias twice must not error or duplicate it.
    again = backend.add_member_alias("FA26", key, "florendo zackary", "treasurer@sandbox.local")
    assert again.ok and again.unchanged == 1
    assert backend.fetch_members("FA26").iloc[0]["alt_keys"] == "florendo zackary"


def test_add_member_alias_on_a_second_member_does_not_touch_the_first():
    from ais_fmd.data.sqlite_backend import SqliteBackend

    backend = SqliteBackend()
    backend.insert_term("FA26", "Fall 2026", "2026-08-01", "2026-12-18", "test@sandbox.local")
    backend.replace_members(
        "FA26",
        [
            {"full_name": "Alice", "match_key": match_key("Alice"), "alt_keys": ("ally",)},
            {"full_name": "Bob", "match_key": match_key("Bob")},
        ],
        "roster.csv",
        "actor",
    )
    backend.add_member_alias("FA26", match_key("Bob"), "bobby", "actor")
    stored = backend.fetch_members("FA26").set_index("match_key")
    assert stored.loc[match_key("Alice"), "alt_keys"] == "ally"
    assert stored.loc[match_key("Bob"), "alt_keys"] == "bobby"


def test_an_empty_file_is_an_error_not_an_empty_roster():
    """
    An empty roster would report 0 expected, 0 unpaid, 100% collection.

    A silently perfect collection rate is the worst possible failure mode here.
    """
    parsed = parse_roster(pd.DataFrame())
    assert not parsed.ok
    assert parsed.errors
