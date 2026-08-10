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

MEETING_WEEKDAYS = frozenset({"Tuesday", "Wednesday"})

COMMITTEE_PURPOSE: dict[int, str] = {
    1: "Dues",
    5: "Food & Drink",
    7: "Professional Development",
    8: "Meeting Food",
    17: "Refunded",
    18: "Formal",
}

_PURCHASE_MARKER = "purchase authorized on "
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
    """The MM/DD that Wells Fargo embeds in 'PURCHASE AUTHORIZED ON 09/17 ...'."""
    if not isinstance(details, str):
        return ""
    lowered = details.lower()
    if _PURCHASE_MARKER not in lowered:
        return ""
    start = lowered.find(_PURCHASE_MARKER) + len(_PURCHASE_MARKER)
    return details[start : start + 5].strip()


def weekday_from_details(details: object, row_date: object) -> str:
    """
    Weekday of the embedded purchase date.

    The posting date on the statement row is often a day or two after the
    purchase, which is why the original went looking inside the description --
    a Tuesday GBM grocery run frequently posts on Thursday.
    """
    date_text = extract_purchase_date(details)
    if len(date_text) < 5:
        return "Unknown"
    try:
        import pandas as pd

        year = pd.to_datetime(row_date, errors="coerce")
        if pd.isna(year):
            return "Unknown"
        parsed = datetime.strptime(f"{date_text}/{int(year.year)}", "%m/%d/%Y")
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


# Documented priority order. First match wins.
RULES: tuple[Callable[[dict], Classification | None], ...] = (
    rule_refund,
    rule_formal,
    rule_consulting,
    rule_dues,
    rule_meeting_food,
    rule_membership_bar,
)


def classify_deterministic(record: dict) -> Classification:
    """Apply every rule in priority order. Returns UNMATCHED if none fire."""
    for rule in RULES:
        result = rule(record)
        if result is not None:
            return result
    return UNMATCHED
