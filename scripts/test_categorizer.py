"""
Standalone test runner for the auto-categorization logic.
Runs without Streamlit — reads OPENAI_API_KEY from environment or .env file.

Usage:
    python test_categorizer.py <input_file>          # CSV or Excel
    python test_categorizer.py <input_file> --out <output.csv>
    python test_categorizer.py <input_file> --no-llm # Python rules + calendar only

Input columns expected (case-insensitive):
    Date / transaction_date / transactiondate
    Amount
    Details
    Account  (optional — "Venmo" or "Wells Fargo")

Output: CSV with added columns: purpose, budget, matched_rule, nearby_events
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# The event calendar is shared with the app rather than duplicated here — it is
# plain stdlib, so importing it costs nothing and keeps the dates in one place.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from views.treasury_event_calendar import (  # noqa: E402
    coverage_summary,
    event_context,
    strong_hint,
)

# Load .env if present (no dependency on python-dotenv — plain parse)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


# ── Keyword lists (kept in sync with treasury_auto_categorize.py) ──────────

FOOD_MERCHANT_KEYWORDS = (
    "publix", "piesanos", "chipotle", "panda express", "chick-fil-a",
    "pizza", "grill", "kitchen", "deli", "cafe", "restaurant", "food",
    "sushi", "asian", "mexic", "menchies", "mr and mrs crab",
    "hana sushi", "las carretas", "escapology",
)

GROCERY_KEYWORDS = (
    "publix", "walmart", "wm supercenter", "target", "costco", "sam's club",
    "sams club", "winn dixie", "trader joe", "aldi", "whole foods",
    "fresh market", "sprouts", "dollar general", "dollar tree", "bagel",
    "donut", "dunkin", "starbucks", "catering",
)

BAR_LIQUOR_KEYWORDS = (
    "macdintons", "salty dog", "saloon", "arcade bar", "the grove",
    "grove - ga", "gator beverage", "abc fine wine", "total wine",
    "liquor", "spirits", "bottle shop", "tavern", "bar ", " bar",
    "lounge", "first magnitud",
)

MEMBERSHIP_BAR_KEYWORDS = (
    "macdintons", "arcade bar", "first magnitud", "the grove", "grove - ga",
    "salty dog", "gator beverage", "abc fine wine", "total wine",
    "liquor", "tavern", "saloon", "lil rudy",
)

DUES_AMOUNTS = frozenset({35.0, 35, 52.5, 52.50})

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


# ── Helpers ─────────────────────────────────────────────────────────────────

_PURCHASE_DATE_RE = re.compile(r"purchase\s+authorized\s+on\s+(\d{2}/\d{2})", re.IGNORECASE)


def extract_purchase_date(details: str) -> str:
    """Pull the MM/DD out of "PURCHASE AUTHORIZED ON MM/DD ...".

    Wells Fargo pads this line to fixed width with runs of spaces between
    every word, so this matches on \\s+ rather than a literal substring.
    """
    if not isinstance(details, str):
        return ""
    match = _PURCHASE_DATE_RE.search(details)
    return match.group(1) if match else ""


def purchase_date_from_row(details: str, row_date) -> date | None:
    """Actual spend date: the bank's "purchase authorized on MM/DD" when present,
    else the posting date. Year comes from the posting date, rolled back for
    purchases that post across New Year's."""
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
    return bool(re.search(r"\bbar\b", details_lower) or re.search(r"\bpub\b", details_lower))


# ── Python override predicates ───────────────────────────────────────────────

def is_refund(amount, details: str, account: str) -> bool:
    try:
        return float(amount) < 0 and is_venmo_or_zelle_channel(details, account)
    except (TypeError, ValueError):
        return False


def is_consulting(details: str) -> bool:
    return "card 8408" in (details or "").lower()


def is_formal(amount, details: str, account: str) -> bool:
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    d = (details or "").lower()
    return amt > 0 and "formal" in d and is_venmo_or_zelle_channel(details, account)


def is_dues(amount, details: str, account: str) -> bool:
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    return amt in DUES_AMOUNTS and amt > 0 and is_venmo_or_zelle_channel(details, account)


def is_membership(details: str, spend_date=None) -> bool:
    d = (details or "").lower()
    if _has_any(d, ("publix", "piesanos", "chipotle", "panda express", "walmart", "wm supercenter")):
        return False
    if not (_has_any(d, MEMBERSHIP_BAR_KEYWORDS) or _bar_or_pub_word(d)):
        return False
    # A bar-named venue isn't always a Membership social (e.g. a Consulting
    # karaoke night at a bar) — defer to the calendar if it says otherwise.
    if spend_date is not None:
        committee, _ = strong_hint(spend_date, window_days=0)
        if committee is not None and committee != 5:
            return False
    return True


def looks_like_meeting_food_merchant(details: str) -> bool:
    d = (details or "").lower()
    if _has_any(d, BAR_LIQUOR_KEYWORDS) or _bar_or_pub_word(d):
        return False
    return _has_any(d, FOOD_MERCHANT_KEYWORDS) or _has_any(d, GROCERY_KEYWORDS)


# ── LLM call ────────────────────────────────────────────────────────────────

def call_llm(transaction_list: str, api_key: str) -> list[dict]:
    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed — pip install openai", file=sys.stderr)
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

7) Calendar fallback
If no rule above applies, Amount is negative (money out), and Nearby_Events shows
exactly ONE same-day event whose committee has no "?", categorize as that committee.

8) Uncategorized
If no rule applies, return Uncategorized. Prefer Uncategorized over a guess —
a blank row gets reviewed, a wrong committee does not.

For each transaction return:
- index (1-based)
- category
- matched_rule

Return ONLY valid JSON:

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
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw).get("transactions", [])
    except Exception as e:
        print(f"LLM error: {e}", file=sys.stderr)
        return []


def parse_llm_category(category_str: str) -> int | None:
    """Pull the committee number out rather than substring-matching a label
    table, so "Committee ID 10" cannot be read as "Committee ID 1"."""
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


# ── Input loading ────────────────────────────────────────────────────────────

COLUMN_ALIASES = {
    "date": "transactiondate",
    "transaction_date": "transactiondate",
    "transactiondate": "transactiondate",
    "amount": "amount",
    "details": "details",
    "description": "details",
    "account": "account",
}


def load_input(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(p)
    else:
        df = pd.read_csv(p)

    df.columns = [COLUMN_ALIASES.get(c.lower().strip().replace(" ", "_"), c.lower().strip()) for c in df.columns]

    if "transactiondate" not in df.columns:
        # If the file has no header (like raw Wells Fargo CSV), assume positional
        df = pd.read_csv(p, header=None)
        df.columns = ["transactiondate", "amount", "_drop1", "_drop2", "details"][: len(df.columns)]
        df = df[[c for c in df.columns if not c.startswith("_")]]

    if "account" not in df.columns:
        df["account"] = ""

    df["details"] = df["details"].astype(str).str.strip()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["transactiondate"] = df["transactiondate"].astype(str).str.strip()
    return df.dropna(subset=["details"]).reset_index(drop=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def categorize(df: pd.DataFrame, api_key: str | None) -> pd.DataFrame:
    n = len(df)
    purpose_out: list[str] = [""] * n
    budget_out: list[str] = [""] * n
    matched_rule_out: list[str] = [""] * n

    # Enrich for LLM
    purchase_dates = [extract_purchase_date(str(df.iloc[i].get("details", ""))) for i in range(n)]
    weekdays = [
        weekday_from_purchase_in_details(str(df.iloc[i].get("details", "")), df.iloc[i].get("transactiondate"))
        for i in range(n)
    ]
    spent_on = [
        purchase_date_from_row(str(df.iloc[i].get("details", "")), df.iloc[i].get("transactiondate"))
        for i in range(n)
    ]
    nearby_events = [event_context(d) for d in spent_on]

    # LLM pass
    if api_key:
        transaction_list = "\n".join(
            f'{i + 1}. Details: {df.iloc[i].get("details", "")} | '
            f'Amount: {df.iloc[i].get("amount")} | '
            f'Account: {df.iloc[i].get("account", "")} | '
            f'Purchase_Date_From_Details: {purchase_dates[i]} | '
            f'Weekday_From_Details: {weekdays[i]} | '
            f'Nearby_Events: {nearby_events[i]}'
            for i in range(n)
        )
        print(f"Sending {n} transactions to GPT-4.1...")
        llm_results = call_llm(transaction_list, api_key)
        print(f"LLM returned {len(llm_results)} results.")
        for item in llm_results:
            idx = item.get("index", 0) - 1
            if 0 <= idx < n:
                cid = parse_llm_category(item.get("category", ""))
                if cid is not None:
                    purpose_out[idx] = COMMITTEE_PURPOSE[cid]
                    budget_out[idx] = COMMITTEE_LABEL[cid]
                    matched_rule_out[idx] = f"LLM: {item.get('matched_rule', '')}"
    else:
        print("No OPENAI_API_KEY — skipping LLM pass, running Python rules only.")

    # Python overrides
    overridden = 0
    for i in range(n):
        row = df.iloc[i]
        amt = row.get("amount")
        details = str(row.get("details", "") or "")
        account = str(row.get("account", "") or "")

        prev = budget_out[i]

        if is_refund(amt, details, account):
            purpose_out[i], budget_out[i], matched_rule_out[i] = COMMITTEE_PURPOSE[17], COMMITTEE_LABEL[17], "Python: Refund"
        elif is_consulting(details):
            purpose_out[i], budget_out[i], matched_rule_out[i] = COMMITTEE_PURPOSE[7], COMMITTEE_LABEL[7], "Python: Consulting"
        elif is_formal(amt, details, account):
            purpose_out[i], budget_out[i], matched_rule_out[i] = COMMITTEE_PURPOSE[18], COMMITTEE_LABEL[18], "Python: Formal"
        elif is_dues(amt, details, account):
            purpose_out[i], budget_out[i], matched_rule_out[i] = COMMITTEE_PURPOSE[1], COMMITTEE_LABEL[1], "Python: Dues"
        elif is_membership(details, spend_date=spent_on[i]):
            purpose_out[i], budget_out[i], matched_rule_out[i] = COMMITTEE_PURPOSE[5], COMMITTEE_LABEL[5], "Python: Membership"

        if budget_out[i] != prev and prev:
            overridden += 1

    if overridden:
        print(f"Python overrides changed {overridden} LLM assignments.")

    # Event calendar fallback — fills blanks only, expenses only.
    filled = 0
    for i in range(n):
        if budget_out[i]:
            continue
        try:
            amount = float(df.iloc[i].get("amount"))
        except (TypeError, ValueError):
            continue
        if amount >= 0:
            continue

        cid, reason = strong_hint(spent_on[i])
        if cid is None:
            continue
        if cid == 8 and not looks_like_meeting_food_merchant(str(df.iloc[i].get("details", ""))):
            continue

        purpose_out[i] = COMMITTEE_PURPOSE[cid]
        budget_out[i] = COMMITTEE_LABEL[cid]
        matched_rule_out[i] = f"Event: {reason}"
        filled += 1

    if filled:
        print(f"Event calendar filled {filled} rows the other passes left blank.")

    df = df.copy()
    df["purpose"] = purpose_out
    df["budget"] = budget_out
    df["matched_rule"] = matched_rule_out
    df["weekday_from_details"] = weekdays
    df["spend_date"] = [d.isoformat() if d else "" for d in spent_on]
    df["nearby_events"] = nearby_events
    return df


def main():
    parser = argparse.ArgumentParser(description="Test auto-categorizer without Streamlit")
    parser.add_argument("input", help="Input CSV or Excel file")
    parser.add_argument("--out", default=None, help="Output CSV path (default: <input>_categorized.csv)")
    parser.add_argument("--no-llm", action="store_true", help="Skip the LLM pass; Python rules + calendar only")
    args = parser.parse_args()

    api_key = None if args.no_llm else (os.environ.get("OPENAI_API_KEY", "").strip() or None)

    print(f"Event calendar: {coverage_summary()}")
    print(f"Loading: {args.input}")
    df = load_input(args.input)
    print(f"Loaded {len(df)} rows.")

    result = categorize(df, api_key)

    out_path = args.out or str(Path(args.input).with_suffix("")) + "_categorized.csv"
    result.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    # Summary
    counts = result["budget"].replace("", "Uncategorized").value_counts()
    print("\n── Category summary ──")
    for cat, count in counts.items():
        print(f"  {cat}: {count}")

    outside = int((result["nearby_events"] == "outside calendar").sum())
    if outside:
        print(
            f"\n  {outside} of {len(result)} rows fall outside the entered semesters — "
            "those were categorized on merchant and weekday alone."
        )


if __name__ == "__main__":
    main()
