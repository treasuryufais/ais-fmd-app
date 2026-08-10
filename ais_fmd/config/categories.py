"""
The single source of truth for committees, purposes, and accounts.

Before this module existed the committee ID list was written down in five
places: a markdown reference table, two inline dropdown lists, the
categorizer's COMMITTEE_LABEL dict, and a copy in scripts/test_categorizer.py.
They had already begun to disagree.

Everything that needs to know a committee ID, a purpose name, or an account
label now reads it from here. The reference table shown to treasurers is
*generated* from these structures rather than typed by hand, so it cannot
drift from the mapping the code actually applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# --- Committees --------------------------------------------------------------

@dataclass(frozen=True)
class Committee:
    id: int
    name: str
    # "committee" participates in budget-vs-actual; "ledger" is a bookkeeping
    # bucket (transfers, refunds) that should not appear in budget charts.
    kind: str = "committee"


COMMITTEES: tuple[Committee, ...] = (
    Committee(1, "Dues", kind="ledger"),
    Committee(2, "Treasury"),
    Committee(3, "Transfers", kind="ledger"),
    Committee(4, "President"),
    Committee(5, "Membership"),
    Committee(6, "Corporate Relations"),
    Committee(7, "Consulting"),
    Committee(8, "Meeting Food"),
    Committee(9, "Marketing"),
    Committee(10, "Professional Development"),
    Committee(11, "Sponsorship / Donation", kind="ledger"),
    Committee(12, "Overhead"),
    Committee(13, "Merch"),
    Committee(14, "Road Trip"),
    Committee(15, "Technology"),
    Committee(16, "Passport"),
    Committee(17, "Refunded", kind="ledger"),
    Committee(18, "Formal"),
)

COMMITTEE_BY_ID: dict[int, Committee] = {c.id: c for c in COMMITTEES}
COMMITTEE_BY_NAME: dict[str, Committee] = {c.name.lower(): c for c in COMMITTEES}

# Committees that receive a budget allocation each term. Derived, not retyped.
BUDGETED_COMMITTEE_IDS: tuple[int, ...] = tuple(
    c.id for c in COMMITTEES if c.kind == "committee"
)


def committee_name(committee_id: int | float | None) -> str:
    """Human-readable committee name, or empty string when unset/unknown."""
    if committee_id is None:
        return ""
    try:
        cid = int(committee_id)
    except (TypeError, ValueError):
        return ""
    committee = COMMITTEE_BY_ID.get(cid)
    return committee.name if committee else ""


def committee_label(committee_id: int | float | None) -> str:
    """The "8 - Meeting Food" display form used in dropdowns."""
    if committee_id is None:
        return ""
    try:
        cid = int(committee_id)
    except (TypeError, ValueError):
        return ""
    committee = COMMITTEE_BY_ID.get(cid)
    return f"{cid} - {committee.name}" if committee else ""


def parse_committee_label(label: str | int | float | None) -> int | None:
    """
    Inverse of `committee_label`. Accepts "8 - Meeting Food", "8", or 8.

    Returns None for blanks and anything unrecognised, so a malformed value
    clears the assignment rather than silently landing on the wrong committee.
    """
    if label is None:
        return None
    if isinstance(label, (int, float)):
        try:
            cid = int(label)
        except (TypeError, ValueError):
            return None
        return cid if cid in COMMITTEE_BY_ID else None

    text = str(label).strip()
    if not text:
        return None
    head = text.split("-", 1)[0].strip()
    try:
        cid = int(head)
    except ValueError:
        # Fall back to matching on name, so "Meeting Food" also works.
        committee = COMMITTEE_BY_NAME.get(text.lower())
        return committee.id if committee else None
    return cid if cid in COMMITTEE_BY_ID else None


def committee_dropdown_options() -> list[str]:
    """Blank-first option list for st.column_config.SelectboxColumn."""
    return [""] + [committee_label(c.id) for c in COMMITTEES]


def committee_reference_rows() -> list[tuple[int, str]]:
    """Rows for the on-screen ID reference table. Generated, never typed."""
    return [(c.id, c.name) for c in COMMITTEES]


# --- Purposes ----------------------------------------------------------------

PURPOSES: tuple[str, ...] = (
    "Dues",
    "Food & Drink",
    "Formal",
    "GBM Catering",
    "ISOM Passport",
    "Marketing",
    "Meeting Food",
    "Merch",
    "Misc.",
    "Professional Development",
    "Professional Events",
    "Refunded",
    "Road Trip",
    "Social Events",
    "Sponsorship / Donation",
    "Tax",
    "Technology",
    "Transfers",
    "Travel Reimbursement",
)


def purpose_dropdown_options() -> list[str]:
    return [""] + list(PURPOSES)


# --- Purpose -> committee ----------------------------------------------------
#
# FINDING F7. The original `map_purpose_to_budget_id` sent "Professional
# Development" to committee 7 (Consulting) and "Food & Drink" to committee 5
# (Membership), while the reference table rendered directly above the upload
# grid told the treasurer that 7 is Consulting, 10 is Professional Development
# and 5 is Membership.
#
# Changing this silently would rewrite which committee historical spending is
# charged against, so the original behaviour is preserved verbatim and the two
# contested entries are marked. The Data Quality page surfaces them, and
# `domain.quality.disputed_mapping_report()` counts how many real transactions
# each one affects, so the decision can be made with the numbers in hand.

PURPOSE_TO_COMMITTEE: dict[str, int] = {
    "Dues": 1,
    "Refunded": 17,
    "Formal": 18,
    "Meeting Food": 8,
    "Food & Drink": 5,
    "Professional Development": 7,
}

DISPUTED_PURPOSE_MAPPINGS: dict[str, str] = {
    "Professional Development": (
        "Mapped to committee 7 (Consulting), but the reference table lists "
        "committee 10 as Professional Development."
    ),
    "Food & Drink": (
        "Mapped to committee 5 (Membership). Defensible if social spending is "
        "owned by Membership, but it is not documented anywhere."
    ),
}


def purpose_to_committee(purpose: str | None) -> int | None:
    if not purpose:
        return None
    return PURPOSE_TO_COMMITTEE.get(str(purpose).strip())


# --- Accounts ----------------------------------------------------------------
#
# FINDING F13. Checking uploads wrote the literal 'Wells', while AGENT.md and
# the AI Assistant's prompt both claimed the value was 'Wells Fargo' -- so the
# model was briefed on a value that did not exist in the data. One canonical
# spelling, plus a normaliser that folds the legacy variants.

ACCOUNT_VENMO = "Venmo"
ACCOUNT_WELLS_FARGO = "Wells Fargo"

ACCOUNTS: tuple[str, ...] = (ACCOUNT_VENMO, ACCOUNT_WELLS_FARGO)

_ACCOUNT_ALIASES: dict[str, str] = {
    "venmo": ACCOUNT_VENMO,
    "wells": ACCOUNT_WELLS_FARGO,
    "wells fargo": ACCOUNT_WELLS_FARGO,
    "wellsfargo": ACCOUNT_WELLS_FARGO,
    "checking": ACCOUNT_WELLS_FARGO,
}


def normalize_account(value: str | None) -> str | None:
    """Fold legacy spellings onto the canonical label."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    return _ACCOUNT_ALIASES.get(key, str(value).strip())


def legacy_account_values(values: Iterable[str | None]) -> set[str]:
    """Values that are recognised but not canonical -- i.e. need backfilling."""
    stale: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in ACCOUNTS:
            continue
        if normalize_account(text) in ACCOUNTS:
            stale.add(text)
    return stale
