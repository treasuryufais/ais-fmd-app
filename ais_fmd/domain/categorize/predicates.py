"""
Deterministic categorization rules.

Two changes of substance from the original:

1. **The keyword lists are actually used.** `FOOD_MERCHANT_KEYWORDS` and
   `BAR_LIQUOR_KEYWORDS` were defined in the original module and referenced
   nowhere -- Meeting Food was delegated to the language model even though the
   data needed to decide it locally was sitting right there. Implementing that
   rule in Python is what lets the pipeline stop sending most rows to a model.

2. **Rule order is explicit and matches the documented priority.** The original
   Python override chain ran refund -> consulting -> formal -> dues ->
   membership, while the prompt next to it documented refund -> formal ->
   consulting -> dues -> meeting food -> membership. The documented order is
   used here, and the ordering lives in one list rather than an if/elif chain.

Every function in this module is pure: same input, same output, no I/O. That is
what makes them the highest-value tests in the repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from ...config.categories import (
    ACCOUNT_VENMO,
    committee_label,
    normalize_account,
)
from ..money import equals_any, parse_amount

# --- Reference data ----------------------------------------------------------

DUES_AMOUNTS: tuple[Decimal, ...] = (Decimal("35.00"), Decimal("52.50"))

FOOD_MERCHANT_KEYWORDS: tuple[str, ...] = (
    "publix", "piesanos", "chipotle", "panda express", "chick-fil-a", "chick fil a",
    "pizza", "grill", "kitchen", "deli", "cafe", "restaurant", "food", "sushi",
    "asian", "mexic", "menchies", "mr and mrs crab", "hana sushi", "las carretas",
    "escapology", "bagel", "bakery", "taco", "wings", "subway", "jimmy john",
    "panera", "sam's club", "walmart", "wm supercenter", "costco", "target",
)

BAR_LIQUOR_KEYWORDS: tuple[str, ...] = (
    "macdintons", "salty dog", "saloon", "arcade bar", "the grove", "grove - ga",
    "gator beverage", "abc fine wine", "total wine", "liquor", "spirits",
    "bottle shop", "tavern", "lounge", "first magnitud", "lil rudy", "brewery",
    "brewing", "cantina", "pub house",
)

# Grocery/big-box merchants that must never be read as a bar even when a
# generic bar keyword appears somewhere in the description.
NEVER_BAR_KEYWORDS: tuple[str, ...] = (
    "publix", "piesanos", "chipotle", "panda express", "walmart",
    "wm supercenter", "target", "costco", "sam's club",
)

CONSULTING_CARD_MARKER = "card 8408"

# Debit cards assigned to specific officers, confirmed against treasury's
# "Categorization Architecture" doc (the source spec this categorizer was
# originally built from): Annalee (8313) and Grant (5718), both Membership.
#
# A transaction on one of these cards is that officer's committee spend BY
# CARDHOLDER DEFAULT -- but only a default, not a certainty like the card
# check in `rule_consulting`. Card issuance has been messy enough in practice
# that meeting food has repeatedly been bought on the wrong person's card, so
# this must never outrank `rule_meeting_food`: see the tier split below.
# Treasury's own account of this ("we need to build in the logic that
# sometimes people's cards were used to buy things for other committees")
# means even this ordering is a best-effort default, not a closed case --
# the Review Queue remains where a treasurer corrects the individual
# exceptions this cannot see, and a recurring exception can be captured
# permanently as a merchant-memory rule (which already outranks this).
MEMBERSHIP_CARD_MARKERS: tuple[str, ...] = ("card 8313", "card 5718")

MEETING_WEEKDAYS = frozenset({"Tuesday", "Wednesday"})

COMMITTEE_PURPOSE: dict[int, str] = {
    1: "Dues",
    5: "Food & Drink",
    7: "Professional Development",
    8: "Meeting Food",
    17: "Refunded",
    18: "Formal",
}

# Real Wells Fargo descriptions pad with runs of spaces:
#   "PURCHASE                    AUTHORIZED ON   05/17 MERCHANT ..."
# The original literal "purchase authorized on " never matched a real statement,
# so the embedded purchase date was never found, the weekday was always
# "Unknown", and the Meeting Food rule could not fire at all. Whitespace is
# collapsed before matching, and "RECURRING PAYMENT" is accepted alongside
# "PURCHASE".
_PURCHASE_RE = re.compile(
    r"(?:purchase|recurring\s+payment)\s+authorized\s+on\s+(\d{1,2}/\d{1,2})",
    re.I,
)
_WHITESPACE_RE = re.compile(r"\s+")
_BAR_WORD_RE = re.compile(r"\b(bar|pub)\b")


# --- Result type -------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    """The outcome of categorizing one transaction."""

    committee_id: int | None
    purpose: str | None
    rule: str
    confidence: float
    source: str  # "rule" | "merchant" | "llm" | "none"

    @property
    def budget_label(self) -> str:
        return committee_label(self.committee_id) if self.committee_id else ""

    @property
    def is_assigned(self) -> bool:
        return self.committee_id is not None


UNMATCHED = Classification(
    committee_id=None, purpose=None, rule="", confidence=0.0, source="none"
)


def _assign(committee_id: int, rule: str, *, confidence: float, source: str = "rule") -> Classification:
    return Classification(
        committee_id=committee_id,
        purpose=COMMITTEE_PURPOSE[committee_id],
        rule=rule,
        confidence=confidence,
        source=source,
    )


# --- Field helpers -----------------------------------------------------------

def extract_purchase_date(details: object) -> str:
    """
    The MM/DD that Wells Fargo embeds in 'PURCHASE AUTHORIZED ON 09/17 ...'.

    Returns "" when there is no embedded date.
    """
    if not isinstance(details, str):
        return ""
    match = _PURCHASE_RE.search(_WHITESPACE_RE.sub(" ", details))
    return match.group(1) if match else ""


def weekday_from_details(details: object, row_date: object) -> str:
    """
    Weekday of the embedded purchase date.

    The posting date on the statement row is often a day or two after the
    purchase, which is why the original went looking inside the description --
    a Tuesday GBM grocery run frequently posts on Thursday.
    """
    date_text = extract_purchase_date(details)
    if not date_text:
        return "Unknown"
    try:
        import pandas as pd

        anchor = pd.to_datetime(row_date, errors="coerce")
        if pd.isna(anchor):
            return "Unknown"
        month, day = (int(part) for part in date_text.split("/"))
        year = int(anchor.year)
        # A purchase on 12/30 can post on 01/02 of the next year, so roll the
        # year back when the embedded month is far ahead of the posting month.
        if month - anchor.month > 6:
            year -= 1
        parsed = datetime(year, month, day)
        return parsed.strftime("%A")
    except (ValueError, TypeError):
        return "Unknown"


def _text(value: object) -> str:
    return "" if value is None else str(value).lower()


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def is_venmo_or_zelle(details: object, account: object) -> bool:
    text = _text(details)
    canonical = normalize_account(account if account is None else str(account))
    return "venmo" in text or "zelle" in text or canonical == ACCOUNT_VENMO


def looks_like_bar(details: object) -> bool:
    text = _text(details)
    if _has_any(text, NEVER_BAR_KEYWORDS):
        return False
    return _has_any(text, BAR_LIQUOR_KEYWORDS) or bool(_BAR_WORD_RE.search(text))


def looks_like_food_merchant(details: object) -> bool:
    return _has_any(_text(details), FOOD_MERCHANT_KEYWORDS)


# --- Individual rules --------------------------------------------------------
#
# Each takes the record dict and returns a Classification or None.

def rule_refund(record: dict) -> Classification | None:
    amount = parse_amount(record.get("amount"))
    if amount is None or amount >= 0:
        return None
    if not is_venmo_or_zelle(record.get("details"), record.get("account")):
        return None
    return _assign(17, "Refund / reimbursement (negative Venmo or Zelle)", confidence=1.0)


def rule_formal(record: dict) -> Classification | None:
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return None
    if "formal" not in _text(record.get("details")):
        return None
    if not is_venmo_or_zelle(record.get("details"), record.get("account")):
        return None
    return _assign(18, "Formal (positive Venmo or Zelle mentioning 'formal')", confidence=1.0)


def rule_consulting(record: dict) -> Classification | None:
    if CONSULTING_CARD_MARKER not in _text(record.get("details")):
        return None
    return _assign(7, f"Consulting ('{CONSULTING_CARD_MARKER}')", confidence=1.0)


def rule_membership_card(record: dict) -> Classification | None:
    """
    Officer-assigned card (8313 or 5718). See MEMBERSHIP_CARD_MARKERS.

    Confidence is 0.9, not 1.0: unlike the card check in `rule_consulting`,
    this is a default inferred from cardholder assignment, not a certainty --
    and it is deliberately positioned to lose to `rule_meeting_food` (see the
    tier split at the bottom of this module).
    """
    text = _text(record.get("details"))
    if not any(marker in text for marker in MEMBERSHIP_CARD_MARKERS):
        return None
    return _assign(5, "Membership (officer card 8313/5718)", confidence=0.9)


def rule_dues(record: dict) -> Classification | None:
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return None
    if not equals_any(amount, DUES_AMOUNTS):
        return None
    if not is_venmo_or_zelle(record.get("details"), record.get("account")):
        return None
    return _assign(1, "Dues (exact dues amount via Venmo or Zelle)", confidence=1.0)


def rule_meeting_food(record: dict) -> Classification | None:
    """
    GBM meeting food: a food merchant, on a meeting weekday, that is not a bar.

    This is the rule the original delegated to the model. Everything it needs is
    local, so it runs here and the model never sees these rows.
    """
    details = record.get("details")
    if looks_like_bar(details) or not looks_like_food_merchant(details):
        return None
    weekday = weekday_from_details(details, record.get("transaction_date"))
    if weekday not in MEETING_WEEKDAYS:
        return None
    return _assign(
        8, f"Meeting food ({weekday} food merchant, not a bar)", confidence=0.9
    )


def rule_membership_bar(record: dict) -> Classification | None:
    if not looks_like_bar(record.get("details")):
        return None
    return _assign(5, "Membership (bar or liquor merchant)", confidence=0.85)


# Rules keyed on an unambiguous marker: a card number, an exact dues amount, the
# sign of a transfer. These are certainties, not guesses -- which is why they
# outrank merchant memory (see `pipeline.categorize_records`).
#
# The spec is explicit about the strongest of them: "If Details contains
# 'card 8408', categorize as Committee ID 7 immediately. Ignore all remaining
# rules." A learned merchant mapping must not be able to override that.
#
# `rule_membership_card` is deliberately NOT here despite also being a card
# check. Card 8408 (Consulting) is certain by design -- one person, one
# purpose, per treasury's own account. Cards 8313/5718 (Membership) are not:
# treasury confirmed meeting food has been bought on the wrong person's card
# because of past card-issuance problems, so it belongs in the heuristic tier
# below `rule_meeting_food`, not up here where nothing can override it.
EXACT_RULES: tuple[Callable[[dict], Classification | None], ...] = (
    rule_refund,
    rule_formal,
    rule_consulting,
    rule_dues,
)

# Keyword, weekday, and card-default heuristics. Confident, but each beatable
# by something more specific: a merchant mapping a human has confirmed
# (runs ahead of this whole tier -- see `pipeline.categorize_records`), or,
# within the tier, by a stronger heuristic listed first. Order matters here:
# `rule_meeting_food` must run before `rule_membership_card` so that meeting
# food bought on an officer's Membership card is still meeting food, not
# reassigned to Membership by cardholder default.
HEURISTIC_RULES: tuple[Callable[[dict], Classification | None], ...] = (
    rule_meeting_food,
    rule_membership_card,
    rule_membership_bar,
)

# Documented priority order. First match wins.
RULES: tuple[Callable[[dict], Classification | None], ...] = EXACT_RULES + HEURISTIC_RULES


def _first_match(
    record: dict, rules: tuple[Callable[[dict], Classification | None], ...]
) -> Classification:
    for rule in rules:
        result = rule(record)
        if result is not None:
            return result
    return UNMATCHED


def classify_exact(record: dict) -> Classification:
    """Only the unambiguous rules. Returns UNMATCHED if none fire."""
    return _first_match(record, EXACT_RULES)


def classify_heuristic(record: dict) -> Classification:
    """Only the keyword/weekday rules. Returns UNMATCHED if none fire."""
    return _first_match(record, HEURISTIC_RULES)


def classify_deterministic(record: dict) -> Classification:
    """Apply every rule in priority order. Returns UNMATCHED if none fire."""
    return _first_match(record, RULES)
