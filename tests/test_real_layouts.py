"""
Regression tests built from a REAL Wells Fargo export.

These exist because the first version of the parser passed a full test suite and
then failed on the first real file it saw -- silently, reporting success while
producing $4.4 trillion in credits and a description column reading "Posted" on
every row. The fixtures had been written to match the same assumptions as the
code, so they confirmed the assumption rather than testing it.

Everything here mirrors the shape of an actual statement:
  * a labelled header row
  * DESCRIPTION at index 1 and AMOUNT at index 2 -- the opposite of the
    headerless layout the original code assumed
  * a low-cardinality STATUS column that must never be mistaken for descriptions
  * descriptions padded with runs of spaces

No real names or account details are reproduced.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain.categorize.merchants import merchant_key
from ais_fmd.domain.categorize.predicates import (
    classify_deterministic,
    extract_purchase_date,
    weekday_from_details,
)
from ais_fmd.domain.money import is_amount_like, parse_amount
from ais_fmd.domain.parsers import wells_fargo

# Real statements pad with runs of spaces. This spacing is load-bearing.
PURCHASE = (
    "PURCHASE                                AUTHORIZED ON   {date} "
    "{merchant} GAINESVILLE FL S30613800522 CARD 3466"
)
ZELLE = "ZELLE FROM SAMPLE PERSON ON {date} REF # JFR0TQ6QPUIM {note}"


def labelled_frame(rows: list[list[str]]) -> pd.DataFrame:
    """Shape A, as read with header=None (the header lands in row 0)."""
    header = ["DATE", "DESCRIPTION", "AMOUNT", "CHECK #", "STATUS"]
    return pd.DataFrame([header] + rows)


def sample_rows(count: int = 12) -> list[list[str]]:
    rows = []
    for index in range(count):
        day = (index % 27) + 1
        rows.append(
            [
                f"09/{day:02d}/2025",
                PURCHASE.format(date=f"09/{day:02d}", merchant=f"MERCHANT {index}"),
                f"-{10 + index}.50",
                "",
                "Posted",
            ]
        )
    return rows


# --- Layout ------------------------------------------------------------------

def test_labelled_export_reads_amount_and_description_from_the_right_columns():
    """
    The regression that matters most. DESCRIPTION is column 1 and AMOUNT is
    column 2 here; the original code read column 1 as the amount, which turned
    every transaction into $0.00.
    """
    frame = labelled_frame(sample_rows())
    result = wells_fargo.parse(frame, "Checking.csv")

    assert result.ok
    assert result.row_count == 12
    assert result.rejected_rows == 0
    # Amounts are real, negative, and not zero.
    amounts = pd.to_numeric(result.rows["amount"])
    assert (amounts < 0).all()
    assert not (amounts == 0).any()
    # Details are descriptions, not the status flag.
    assert "MERCHANT" in result.rows.iloc[0]["details"]
    assert "Posted" not in result.rows.iloc[0]["details"]


def test_status_column_can_never_win_the_description_slot():
    """
    "Posted" repeated on every row previously scored as a perfect description
    column, because scoring only asked "does it contain letters".
    """
    frame = labelled_frame(sample_rows(40))
    result = wells_fargo.parse(frame, "Checking.csv")
    assert result.ok
    assert result.rows["details"].nunique() > 1


def test_headerless_layout_still_works():
    """Shape B must keep working -- amount at index 1, description at index 4."""
    rows = [
        ["09/16/2025", "-142.30", "*", "", PURCHASE.format(date="09/16", merchant="PUBLIX")],
        ["09/17/2025", "-58.10", "*", "", PURCHASE.format(date="09/17", merchant="CHIPOTLE")],
        ["09/18/2025", "-12.00", "*", "", PURCHASE.format(date="09/18", merchant="PANERA")],
    ]
    result = wells_fargo.parse(pd.DataFrame(rows), "checking.csv")
    assert result.ok
    assert result.row_count == 3
    assert float(result.rows.iloc[0]["amount"]) == -142.30


def test_implausible_result_is_rejected_rather_than_imported():
    """
    The $4.4 trillion case. Even if column detection is confidently wrong, an
    absurd result must not be imported.
    """
    rows = [
        [f"09/{(i % 27) + 1:02d}/2025", "9" * 13, "", "", "Posted"] for i in range(20)
    ]
    frame = pd.DataFrame([["DATE", "AMOUNT", "CHECK #", "MEMO", "STATUS"]] + rows)
    result = wells_fargo.parse(frame, "weird.csv")
    assert not result.ok
    assert result.rows.empty


def test_amount_detection_is_strict_about_whole_cells():
    """
    The root cause of the silent failure: `parse_amount` finds a number anywhere
    in a string, so a description containing "ON 07/08" scored as an amount.
    """
    description = ZELLE.format(date="07/08", note="DUES")
    assert parse_amount(description) is not None  # permissive, by design
    assert not is_amount_like(description)        # strict, used for detection
    assert is_amount_like("-61.25")
    assert is_amount_like("35.00")
    assert not is_amount_like("09/16/2025")


# --- Description parsing -----------------------------------------------------

def test_purchase_date_survives_multi_space_padding():
    """
    The literal marker "purchase authorized on " never matched a real statement,
    so the weekday was always Unknown and Meeting Food could never fire.
    """
    assert extract_purchase_date(PURCHASE.format(date="05/17", merchant="PUBLIX")) == "05/17"


def test_recurring_payment_descriptions_also_yield_a_date():
    text = (
        "RECURRING PAYMENT                       AUTHORIZED ON   07/08 "
        "ATLASSIAN HTTPSWWW.ATLA CA S30419113 CARD 7757"
    )
    assert extract_purchase_date(text) == "07/08"


def test_weekday_rolls_back_the_year_across_a_december_boundary():
    """A 12/30 purchase can post on 01/02, which must not be read as December of the new year."""
    text = PURCHASE.format(date="12/30", merchant="PUBLIX")
    assert weekday_from_details(text, "2026-01-02") == "Tuesday"  # 2025-12-30


def test_meeting_food_fires_on_a_real_format_description():
    """End-to-end: the rule that produced zero matches on the real file."""
    # 2025-09-16 was a Tuesday.
    result = classify_deterministic(
        {
            "amount": -142.30,
            "details": PURCHASE.format(date="09/16", merchant="PUBLIX SUPER MAR"),
            "transaction_date": "2025-09-18",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id == 8


# --- Merchant keys -----------------------------------------------------------

def test_store_numbers_collapse_to_one_key():
    a = merchant_key(PURCHASE.format(date="09/16", merchant="CHIPOTLE 1462"))
    b = merchant_key(PURCHASE.format(date="11/02", merchant="CHIPOTLE 1893"))
    assert a == b != ""


def test_merchant_names_beginning_with_a_number_are_preserved():
    key = merchant_key(PURCHASE.format(date="09/16", merchant="7 ELEVEN"))
    assert key.startswith("7")


@pytest.mark.parametrize("generic", ["TST", "THE", "RETURN", "ON"])
def test_no_key_collapses_to_a_single_generic_word(generic):
    """
    A one-token key like "tst" would match every Toast-POS restaurant and
    mis-categorise all of them under one rule.
    """
    key = merchant_key(PURCHASE.format(date="09/16", merchant=generic))
    assert key == "" or len(key.split()) > 1 or len(key) >= 4


def test_pos_prefixes_are_stripped():
    assert not merchant_key(
        PURCHASE.format(date="09/16", merchant="TST* MI APA")
    ).startswith("tst")


def test_zelle_transfers_produce_no_merchant_key():
    """
    Person-to-person transfers have no merchant. Learning them would create one
    rule per member, keyed on a person's name.
    """
    assert merchant_key(ZELLE.format(date="07/08", note="DUES")) == ""
    assert merchant_key(ZELLE.format(date="07/08", note="VENMO REIMBURSEMENT")) == ""


def test_keys_are_stable_across_dates_and_reference_numbers():
    keys = {
        merchant_key(
            "PURCHASE                    AUTHORIZED ON   "
            f"{month:02d}/14 PUBLIX SUPER MAR GAINESVILLE FL S3061380{month:04d} CARD 3466"
        )
        for month in range(1, 13)
    }
    assert len(keys) == 1, f"key drifted across dates: {keys}"
