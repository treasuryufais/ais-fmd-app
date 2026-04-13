"""
Enhanced auto-categorization for treasury uploads (Checking + Venmo).

Derived from treasury project rules: refunds/reimbursements, consulting card,
formal (Venmo/Zelle + amount), dues amounts, membership (bar + specific cards),
GBM meeting food (Tue/Wed + food merchant, not bar).
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

# --- Keyword lists (from treasury project spec) ---
FOOD_MERCHANT_KEYWORDS = (
    "publix",
    "piesanos",
    "chipotle",
    "panda express",
    "chick-fil-a",
    "pizza",
    "grill",
    "kitchen",
    "deli",
    "cafe",
    "restaurant",
    "food",
    "sushi",
    "asian",
    "mexic",
    "menchies",
    "mr and mrs crab",
    "hana sushi",
    "las carretas",
    "escapology",
)

BAR_LIQUOR_KEYWORDS = (
    "macdintons",
    "salty dog",
    "saloon",
    "arcade bar",
    "the grove",
    "grove - ga",
    "gator beverage",
    "abc fine wine",
    "total wine",
    "liquor",
    "spirits",
    "bottle shop",
    "tavern",
    "bar ",
    " bar",
    "lounge",
    "first magnitud",
)

MEMBERSHIP_BAR_KEYWORDS = (
    "macdintons",
    "arcade bar",
    "first magnitud",
    "the grove",
    "grove - ga",
    "salty dog",
    "gator beverage",
    "abc fine wine",
    "total wine",
    "liquor",
    "tavern",
    "saloon",
    "lil rudy",
    # Avoid bare "pub"/"bar" — they match inside "PUBLIX" / unrelated merchants
)

DUES_AMOUNTS = frozenset({35.0, 35, 52.5, 52.50})


def extract_purchase_date(details: str) -> str:
    """Return 'MM/DD' from 'PURCHASE AUTHORIZED ON MM/DD ...' if present."""
    if not isinstance(details, str):
        return ""
    details_lower = details.lower()
    marker = "purchase authorized on "
    if marker not in details_lower:
        return ""
    start = details_lower.find(marker) + len(marker)
    return details[start : start + 5].strip()


def weekday_from_purchase_in_details(details: str, row_date) -> str:
    """Weekday name from embedded purchase date, else 'Unknown'."""
    date_str = extract_purchase_date(details)
    if not date_str or len(date_str) < 5:
        return "Unknown"
    try:
        year = pd.to_datetime(row_date, errors="coerce").year
        if pd.isna(year):
            return "Unknown"
        dt = datetime.strptime(f"{date_str}/{int(year)}", "%m/%d/%Y")
        return dt.strftime("%A")
    except (ValueError, TypeError):
        return "Unknown"


def is_venmo_or_zelle_channel(details: str, account: str) -> bool:
    """True if transaction is clearly Venmo/Zelle (details text or Venmo account)."""
    d = (details or "").lower()
    acc = (account or "").lower()
    return "venmo" in d or "zelle" in d or acc == "venmo"


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    h = haystack.lower()
    return any(n in h for n in needles)


def is_refund_reimbursement_row(amount, details: str, account: str) -> bool:
    if pd.isna(amount):
        return False
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    if amt >= 0:
        return False
    return is_venmo_or_zelle_channel(details, account)


def is_consulting_card_row(details: str) -> bool:
    return "card 8408" in (details or "").lower()


def is_formal_row(amount, details: str, account: str) -> bool:
    if pd.isna(amount) or float(amount) <= 0:
        return False
    d = (details or "").lower()
    if "formal" not in d:
        return False
    return is_venmo_or_zelle_channel(details, account)


def is_dues_row(amount, details: str, account: str) -> bool:
    if pd.isna(amount):
        return False
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    if amt not in DUES_AMOUNTS or amt <= 0:
        return False
    return is_venmo_or_zelle_channel(details, account)


def is_membership_bar_row(details: str) -> bool:
    """Bar/liquor / social venue spend → Membership committee."""
    d = (details or "").lower()
    if _has_any(d, ("publix", "piesanos", "chipotle", "panda express", "walmart", "wm supercenter")):
        return False
    return _has_any(d, MEMBERSHIP_BAR_KEYWORDS) or _bar_or_pub_word(d)


def _bar_or_pub_word(details_lower: str) -> bool:
    """True if 'bar' or 'pub' appears as a word (not inside 'publix')."""
    if re.search(r"\bbar\b", details_lower):
        return True
    if re.search(r"\bpub\b", details_lower):
        return True
    return False


def is_gbm_meeting_food_row(amount, details: str, row_date, account: str) -> bool:
    """Tue/Wed food purchase at grocery/restaurant, not a bar line."""
    if pd.isna(amount) or float(amount) >= 0:
        return False
    d = (details or "").lower()
    if "purchase authorized on" not in d:
        return False
    wd = weekday_from_purchase_in_details(details, row_date)
    if wd not in ("Tuesday", "Wednesday"):
        return False
    if not _has_any(d, FOOD_MERCHANT_KEYWORDS):
        return False
    if _has_any(d, BAR_LIQUOR_KEYWORDS):
        return False
    return True


def _budget_label(committee_id: int) -> str:
    labels = {
        1: "1 - Dues",
        5: "5 - Membership",
        7: "7 - Consulting",
        8: "8 - Meeting Food",
        17: "17 - Refunded",
        18: "18 - Formal",
    }
    return labels.get(committee_id, "")


def _purpose_for_committee(cid: int) -> str:
    return {
        1: "Dues",
        5: "Food & Drink",
        7: "Professional Development",
        8: "Meeting Food",
        17: "Refunded",
        18: "Formal",
    }.get(cid, "")


def apply_enhanced_auto_categorization(df_proc: pd.DataFrame) -> pd.DataFrame:
    """
    Set purpose + budget columns on a processed upload frame.
    Expects: transactiondate, amount, details, account; mutates copies of purpose/budget.
    """
    df = df_proc.copy()
    if "purpose" not in df.columns:
        df["purpose"] = None
    if "budget" not in df.columns:
        df["budget"] = ""

    n = len(df)
    purpose_out: list[str | None] = [None] * n
    budget_out: list[str] = [""] * n

    for i in range(n):
        row = df.iloc[i]
        amt = row.get("amount")
        details = str(row.get("details", "") or "")
        account = str(row.get("account", "") or "")
        row_date = row.get("transactiondate")

        if is_refund_reimbursement_row(amt, details, account):
            purpose_out[i] = _purpose_for_committee(17)
            budget_out[i] = _budget_label(17)
            continue
        if is_consulting_card_row(details):
            purpose_out[i] = _purpose_for_committee(7)
            budget_out[i] = _budget_label(7)
            continue
        if is_formal_row(amt, details, account):
            purpose_out[i] = _purpose_for_committee(18)
            budget_out[i] = _budget_label(18)
            continue
        if is_dues_row(amt, details, account):
            purpose_out[i] = _purpose_for_committee(1)
            budget_out[i] = _budget_label(1)
            continue
        # GBM food before membership so grocery (e.g. Publix) is not caught by bar/pub heuristics
        if is_gbm_meeting_food_row(amt, details, row_date, account):
            purpose_out[i] = _purpose_for_committee(8)
            budget_out[i] = _budget_label(8)
            continue
        if is_membership_bar_row(details):
            purpose_out[i] = _purpose_for_committee(5)
            budget_out[i] = _budget_label(5)
            continue

    df["purpose"] = purpose_out
    df["budget"] = budget_out
    return df
