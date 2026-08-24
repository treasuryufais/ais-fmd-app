"""
Hybrid auto-categorization for treasury uploads (Checking + Venmo).

Sends transactions to GPT-4.1 for fuzzy matching (primarily Meeting Food),
then applies deterministic Python overrides for all high-confidence rules.
Python overrides always win over LLM output.

Both passes are given event-calendar context (treasury_event_calendar.py): what
the org had scheduled on the purchase date tells you what a Publix run was for
in a way the merchant name never can. Anything still uncategorized after both
passes falls back to a strong same-day event.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

import pandas as pd
import streamlit as st
from openai import OpenAI

from .treasury_event_calendar import event_context, strong_hint

# --- Keyword lists ---
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

# Merchants a Meeting Food run plausibly comes from. Used to keep the event
# calendar from tagging, say, a hardware store as Meeting Food just because it
# was bought the day before a GBM.
GROCERY_KEYWORDS = (
    "publix",
    "walmart",
    "wm supercenter",
    "target",
    "costco",
    "sam's club",
    "sams club",
    "winn dixie",
    "trader joe",
    "aldi",
    "whole foods",
    "fresh market",
    "sprouts",
    "dollar general",
    "dollar tree",
    "bagel",
    "donut",
    "dunkin",
    "starbucks",
    "catering",
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
)

DUES_AMOUNTS = frozenset({35.0, 35, 52.5, 52.50})

# Labels must match the Committee ID dropdown in Treasury_Management.py exactly —
# the upload flow parses the leading integer back out of this string.
COMMITTEE_LABEL = {
    1: "1 - Dues",
    5: "5 - Membership",
    7: "7 - Consulting",
    8: "8 - Meeting Food",
    9: "9 - Marketing",
    10: "10 - Professional Development",
    16: "16 - Passport",
    17: "17 - Refunded",
    18: "18 - Formal",
}

COMMITTEE_PURPOSE = {
    1: "Dues",
    5: "Food & Drink",
    7: "Professional Development",
    8: "Meeting Food",
    9: "Marketing",
    10: "Professional Development",
    16: "ISOM Passport",
    17: "Refunded",
    18: "Formal",
}


# --- Helpers ---

_PURCHASE_DATE_RE = re.compile(r"purchase\s+authorized\s+on\s+(\d{2}/\d{2})", re.IGNORECASE)


def extract_purchase_date(details: str) -> str:
    """Pull the MM/DD out of "PURCHASE AUTHORIZED ON MM/DD ...".

    Wells Fargo pads this line to fixed width with runs of spaces between
    every word ("PURCHASE                    AUTHORIZED ON   01/14"), so this
    matches on \\s+ rather than a literal single-spaced substring.
    """
    if not isinstance(details, str):
        return ""
    match = _PURCHASE_DATE_RE.search(details)
    return match.group(1) if match else ""


def purchase_date_from_row(details: str, row_date) -> date | None:
    """When the money was actually spent.

    Prefers the "purchase authorized on MM/DD" date the bank prints in the
    details over the posting date, since a card swipe can post days later.
    The bank omits the year, so it comes from the posting date — with a
    rollback for purchases that post across New Year's (12/31 posting 01/02
    would otherwise read as a date eleven months in the future).
    """
    posted_ts = pd.to_datetime(row_date, errors="coerce")
    posted = None if pd.isna(posted_ts) else posted_ts.date()

    date_str = extract_purchase_date(details)
    if not date_str or len(date_str) < 5 or posted is None:
        return posted

    try:
        purchased = datetime.strptime(f"{date_str}/{posted.year}", "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return posted

    if (purchased - posted).days > 30:
        try:
            purchased = datetime.strptime(f"{date_str}/{posted.year - 1}", "%m/%d/%Y").date()
        except (ValueError, TypeError):
            return posted
    return purchased


def weekday_from_purchase_in_details(details: str, row_date) -> str:
    if not extract_purchase_date(details):
        return "Unknown"
    purchased = purchase_date_from_row(details, row_date)
    return purchased.strftime("%A") if purchased else "Unknown"


def is_venmo_or_zelle_channel(details: str, account: str) -> bool:
    d = (details or "").lower()
    acc = (account or "").lower()
    return "venmo" in d or "zelle" in d or acc == "venmo"


def _has_any(haystack: str, needles: tuple) -> bool:
    h = haystack.lower()
    return any(n in h for n in needles)


def _bar_or_pub_word(details_lower: str) -> bool:
    if re.search(r"\bbar\b", details_lower):
        return True
    if re.search(r"\bpub\b", details_lower):
        return True
    return False


def looks_like_meeting_food_merchant(details: str) -> bool:
    """Grocery/restaurant merchant, and not a bar or liquor store."""
    d = (details or "").lower()
    if _has_any(d, BAR_LIQUOR_KEYWORDS) or _bar_or_pub_word(d):
        return False
    return _has_any(d, FOOD_MERCHANT_KEYWORDS) or _has_any(d, GROCERY_KEYWORDS)


# --- Python override predicates ---

def is_refund_reimbursement_row(amount, details: str, account: str) -> bool:
    if pd.isna(amount):
        return False
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    return amt < 0 and is_venmo_or_zelle_channel(details, account)


def is_consulting_card_row(details: str) -> bool:
    return "card 8408" in (details or "").lower()


def is_formal_row(amount, details: str, account: str) -> bool:
    if pd.isna(amount):
        return False
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    if amt <= 0:
        return False
    d = (details or "").lower()
    return "formal" in d and is_venmo_or_zelle_channel(details, account)


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


def is_membership_bar_row(details: str, spend_date=None) -> bool:
    d = (details or "").lower()
    if _has_any(d, ("publix", "piesanos", "chipotle", "panda express", "walmart", "wm supercenter")):
        return False
    if not (_has_any(d, MEMBERSHIP_BAR_KEYWORDS) or _bar_or_pub_word(d)):
        return False
    # A bar-named venue isn't always a Membership social — e.g. the calendar's
    # own "Consulting Meeting + Rowdy Karaoke" at Rowdy's. If the calendar shows
    # a same-day strong event for a different committee, defer to it (via the
    # Step 4 fallback) instead of assuming Membership from the merchant name alone.
    if spend_date is not None:
        committee, _ = strong_hint(spend_date, window_days=0)
        if committee is not None and committee != 5:
            return False
    return True


# --- LLM call ---

def _call_llm(transaction_list: str, api_key: str | None = None) -> list[dict]:
    """Call GPT-4.1 with the transaction list; return parsed list of {index, category, matched_rule}."""
    if api_key is None:
        try:
            api_key = st.secrets["openai"]["api_key"]
        except Exception:
            return []

    prompt = f"""
You are categorizing financial transactions for a student organization accounting system.

IMPORTANT:
- Ignore the Date column.
- Use the provided Amount, Purchase_Date_From_Details, and Weekday_From_Details fields.
- Do not recalculate weekdays yourself if Weekday_From_Details is provided.
- Nothing is case-sensitive.
- Follow rule priority exactly. First match wins.

CALENDAR CONTEXT:
Each transaction carries a Nearby_Events field: what the organization had on its
official calendar on the purchase date and the day either side. The committee an
event's spending belongs to is shown in [brackets].
- "same day" / "next day" / "previous day" are relative to the purchase. "next
  day: GBM 7" means the purchase happened the day BEFORE that GBM — a supply run.
- A committee with a trailing "?" (e.g. [Membership?]) is a loose association:
  the event happened, but what was spent on it is a guess. Use it only to break
  a tie, never as the sole reason to assign a category.
- "outside calendar" means that semester was never entered — judge on merchant
  and weekday alone. "none" means nothing was scheduled.
Nearby_Events is what separates a Publix run for a GBM from a Publix run for a
social: same merchant, different committee.

Return ONE category from:
- Category 17 (Refunded/Reimbursement)
- Committee ID 18 (Formal)
- Committee ID 7 (Consulting)
- Committee ID 1 (Dues)
- Committee ID 8 (GBM Meeting Food)
- Committee ID 5 (Membership)
- Committee ID 10 (Professional Development — workshops)
- Committee ID 16 (Passport)
- Committee ID 9 (Marketing)
- Uncategorized

The last three come only from rule 7 below — there is no merchant pattern for them,
only the calendar.

RULE PRIORITY (first match wins):

1) Category 17 (Refunded/Reimbursement)
If Details contains "venmo" or "zelle" AND Amount is negative, categorize as Category 17.

2) Committee ID 18 (Formal)
If Details contains "venmo" or "zelle" AND Amount is positive AND Details contains "formal",
categorize as Committee ID 18.

3) Committee ID 7 (Consulting)
If Details contains "card 8408", categorize as Committee ID 7 immediately.

4) Committee ID 1 (Dues)
If Details contains "venmo" or "zelle" AND Amount is exactly 35, 35.0, 35.00, 52.5, or 52.50 AND Amount is positive, categorize as Committee ID 1.

5) Committee ID 8 (GBM Meeting Food)
Categorize as Committee ID 8 if BOTH are true:
- Merchant is a grocery store or restaurant/food place, and NOT clearly a bar or liquor store
- AND either:
  (a) Nearby_Events shows a GBM [Meeting Food] on the same day or the next day, OR
  (b) Nearby_Events is "outside calendar" or "none", AND Weekday_From_Details is Tuesday or Wednesday

Do NOT use (b) when the calendar is available and shows no GBM — if the calendar
covers that date and the only nearby event is a social, it is Committee ID 5.

Food/restaurant examples: publix, piesanos, chipotle, panda express, chick-fil-a, pizza, grill, kitchen, deli, cafe, restaurant, food, sushi, asian

Bar/liquor examples (exclude these): macdintons, salty dog, saloon, bar, pub, tavern, lounge, liquor, spirits, wine, beer, bottle shop, abc fine wine, total wine, gator beverage, arcade bar, the grove

6) Committee ID 5 (Membership)
Categorize as Committee ID 5 if EITHER:
- The merchant appears to be a bar or liquor store AND Nearby_Events does NOT show
  a same-day non-Membership committee with no "?" (e.g. a Consulting meeting or
  GBM that happens to be held at a bar belongs to that committee, not Membership), OR
- Amount is negative (money out) AND Nearby_Events shows a same-day [Membership]
  social with no "?" — e.g. a tab or party at a venue that matches the merchant.
A bar-named venue is not automatically Membership — check what the calendar says
actually happened there that day.
Bar/liquor examples: macdintons, salty dog, saloon, bar, pub, tavern, lounge, liquor, spirits, wine, beer, bottle shop, abc fine wine, total wine, gator beverage, arcade bar, the grove

7) Calendar fallback
If no rule above applies, Amount is negative (money out), and Nearby_Events shows
exactly ONE same-day event whose committee has no "?", categorize as that committee.

8) Uncategorized
If no rule applies, return Uncategorized. Prefer Uncategorized over a guess —
a blank row gets reviewed, a wrong committee does not.

For each transaction return:
- index
- category
- matched_rule

Return ONLY valid JSON in this format:

{{
  "transactions": [
    {{
      "index": 1,
      "category": "Committee ID 8 (GBM Meeting Food)",
      "matched_rule": "Committee ID 8"
    }}
  ]
}}

Transactions:
{transaction_list}
"""

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4.1",
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.choices[0].message.content.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(result)
        return data.get("transactions", [])
    except Exception:
        return []


def _parse_llm_category(category_str: str) -> int | None:
    """Map an LLM category string to a committee ID, or None if unrecognized/uncategorized.

    Pulls the number out rather than substring-matching a label table, so
    "Committee ID 10" cannot be read as "Committee ID 1".
    """
    if not category_str:
        return None
    s = category_str.lower()
    if "uncategorized" in s:
        return None
    match = re.search(r"(?:committee id|category)\s*#?\s*(\d+)", s)
    if not match:
        return None
    cid = int(match.group(1))
    return cid if cid in COMMITTEE_LABEL else None


# --- Main entry point ---

def apply_enhanced_auto_categorization(df_proc: pd.DataFrame, api_key: str | None = None) -> pd.DataFrame:
    """
    Hybrid categorization: LLM pass for fuzzy matching, then deterministic Python overrides,
    then an event-calendar fallback for whatever is still blank.
    Expects columns: transactiondate, amount, details, account.
    Returns df with purpose, budget, and event_hint columns set.
    """
    df = df_proc.copy()
    if "purpose" not in df.columns:
        df["purpose"] = None
    if "budget" not in df.columns:
        df["budget"] = ""

    n = len(df)
    purpose_out: list[str | None] = [None] * n
    budget_out: list[str] = [""] * n
    event_out: list[str] = [""] * n

    # --- Step 1: build enriched transaction strings for LLM ---
    purchase_dates = []
    weekdays = []
    spent_on: list[date | None] = []
    nearby_events = []
    for i in range(n):
        row = df.iloc[i]
        details = str(row.get("details", "") or "")
        row_date = row.get("transactiondate")
        purchase_dates.append(extract_purchase_date(details))
        weekdays.append(weekday_from_purchase_in_details(details, row_date))
        spent = purchase_date_from_row(details, row_date)
        spent_on.append(spent)
        nearby_events.append(event_context(spent))

    transaction_list = "\n".join(
        f'{i + 1}. Details: {str(df.iloc[i].get("details", ""))} | '
        f'Amount: {df.iloc[i].get("amount")} | '
        f'Account: {str(df.iloc[i].get("account", ""))} | '
        f'Purchase_Date_From_Details: {purchase_dates[i]} | '
        f'Weekday_From_Details: {weekdays[i]} | '
        f'Nearby_Events: {nearby_events[i]}'
        for i in range(n)
    )

    # --- Step 2: LLM pass ---
    llm_results = _call_llm(transaction_list, api_key=api_key)
    for item in llm_results:
        idx = item.get("index", 0) - 1
        if 0 <= idx < n:
            cid = _parse_llm_category(item.get("category", ""))
            if cid is not None:
                purpose_out[idx] = COMMITTEE_PURPOSE[cid]
                budget_out[idx] = COMMITTEE_LABEL[cid]

    # --- Step 3: Python overrides (always win) ---
    for i in range(n):
        row = df.iloc[i]
        amt = row.get("amount")
        details = str(row.get("details", "") or "")
        account = str(row.get("account", "") or "")

        if is_refund_reimbursement_row(amt, details, account):
            purpose_out[i] = COMMITTEE_PURPOSE[17]
            budget_out[i] = COMMITTEE_LABEL[17]
        elif is_consulting_card_row(details):
            purpose_out[i] = COMMITTEE_PURPOSE[7]
            budget_out[i] = COMMITTEE_LABEL[7]
        elif is_formal_row(amt, details, account):
            purpose_out[i] = COMMITTEE_PURPOSE[18]
            budget_out[i] = COMMITTEE_LABEL[18]
        elif is_dues_row(amt, details, account):
            purpose_out[i] = COMMITTEE_PURPOSE[1]
            budget_out[i] = COMMITTEE_LABEL[1]
        elif is_membership_bar_row(details, spend_date=spent_on[i]):
            purpose_out[i] = COMMITTEE_PURPOSE[5]
            budget_out[i] = COMMITTEE_LABEL[5]
        # Meeting Food: trust LLM — no Python override

    # --- Step 4: event calendar fallback (fills blanks only, never overrides) ---
    for i in range(n):
        if budget_out[i]:
            continue

        amt = df.iloc[i].get("amount")
        try:
            amount = float(amt)
        except (TypeError, ValueError):
            continue
        # Expenses only. Money coming in is dues or formal payments, which the
        # deterministic rules above already own — a date match should not
        # reclassify an inflow as event spending.
        if amount >= 0:
            continue

        committee, reason = strong_hint(spent_on[i])
        if committee is None:
            continue
        details = str(df.iloc[i].get("details", "") or "")
        if committee == 8 and not looks_like_meeting_food_merchant(details):
            continue

        purpose_out[i] = COMMITTEE_PURPOSE[committee]
        budget_out[i] = COMMITTEE_LABEL[committee]
        event_out[i] = reason

    df["purpose"] = purpose_out
    df["budget"] = budget_out
    df["event_hint"] = event_out
    return df
