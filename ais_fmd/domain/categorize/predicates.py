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
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from typing import Callable

from ...config.categories import (
    ACCOUNT_VENMO,
    committee_label,
    normalize_account,
)
from ..money import equals_any, parse_amount

# --- Reference data ----------------------------------------------------------

# The rates in force when this categorizer was written (Fall 2024). These remain
# the fallback for any caller that supplies no `DuesSchedule`, so behaviour is
# bit-for-bit unchanged unless per-term rates are actually provided.
DUES_AMOUNTS: tuple[Decimal, ...] = (Decimal("35.00"), Decimal("52.50"))

# Venmo takes a cut, so dues paid over Venmo arrive NET of fees. The three
# historical amounts on record are 24.43, 29.34 and 39.14 against gross rates of
# $25, $30 and $40 -- shortfalls of 2.28%, 2.20% and 2.15%.
#
# Those three points do NOT fit one rate-plus-fixed-fee formula to the cent
# (solving from the $25 and $40 pairs predicts 0.67 for $30 where the observed
# fee is 0.66), so the exact schedule Venmo applied cannot be recovered from the
# data available. A bounded window below the gross rate is therefore the honest
# model: a fee can only ever reduce the amount received, and 3% clears all three
# observations with margin while staying far below the gap to the next rate.
VENMO_FEE_TOLERANCE: Decimal = Decimal("0.03")


@dataclass(frozen=True)
class DuesWindow:
    """The dues rates in force for one term."""

    term_id: str
    semester: str
    start: date
    end: date
    rates: tuple[Decimal, ...]
    # False means "nobody has confirmed these are the rates that term actually
    # charged" -- they were seeded from the old module constant. Surfaced by
    # `quality.check_unverified_dues_rates` rather than assumed correct.
    verified: bool = False


class DuesSchedule:
    """
    Which dues amounts are valid, and when.

    `DUES_AMOUNTS` was a module constant pinned to Fall 2024. The VP Treasury
    Handbook shows the rate changing nearly every term ($20/$40 -> $25/$40 ->
    $30/$50 -> $35/$52.50), and because `rule_dues` tests *exact* equality, the
    first term after a rate change silently stops categorizing dues altogether:
    no error is raised, the rows simply fall through to the review queue and
    dues income appears to collapse. Rates belong beside the term, as data.

    An empty schedule behaves exactly like the old constant, which is what every
    caller that passes nothing continues to get.
    """

    def __init__(
        self,
        windows: "tuple[DuesWindow, ...] | list[DuesWindow]" = (),
        *,
        default_rates: tuple[Decimal, ...] = DUES_AMOUNTS,
        accept_venmo_net: bool = False,
        venmo_fee_tolerance: Decimal = VENMO_FEE_TOLERANCE,
    ) -> None:
        self._windows = tuple(sorted(windows, key=lambda w: w.start))
        self._default_rates = tuple(default_rates)
        # OFF by default: accepting net-of-fee amounts books income that exact
        # matching would leave for a human, and that is a treasurer's call.
        # See HANDOFF section 4.1 / docs/treasury-questions.md.
        self._accept_venmo_net = accept_venmo_net
        self._venmo_fee_tolerance = venmo_fee_tolerance

    def __bool__(self) -> bool:
        return bool(self._windows)

    # Read-only throughout: `dues.schedule_from_terms` memoizes and hands the
    # same instance to every caller, so a settable flag here would let one page
    # silently change how another page books income.
    @property
    def windows(self) -> tuple[DuesWindow, ...]:
        return self._windows

    @property
    def accept_venmo_net(self) -> bool:
        return self._accept_venmo_net

    @property
    def venmo_fee_tolerance(self) -> Decimal:
        return self._venmo_fee_tolerance

    def window_for(self, when: date | None) -> DuesWindow | None:
        if when is None:
            return None
        for window in self._windows:
            if window.start <= when <= window.end:
                return window
        return None

    def rates_for(self, when: date | None) -> tuple[Decimal, ...]:
        """
        Rates in force on `when`, falling back to the default.

        Falling back rather than returning nothing is deliberate: a transaction
        dated outside every configured term must not silently stop being dues.
        """
        window = self.window_for(when)
        if window is None or not window.rates:
            return self._default_rates
        return window.rates

    def matches(self, amount: Decimal, when: date | None, *, is_venmo: bool = False) -> bool:
        """Does `amount` look like a dues payment made on `when`?"""
        rates = self.rates_for(when)
        if equals_any(amount, rates):
            return True
        if not (self.accept_venmo_net and is_venmo):
            return False
        # A fee only ever reduces what arrives, so the window is one-sided.
        return any(
            gross * (Decimal(1) - self.venmo_fee_tolerance) <= amount < gross
            for gross in rates
        )


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


def is_venmo(details: object, account: object) -> bool:
    """
    Venmo specifically, not Zelle.

    Only Venmo takes a cut, so only Venmo rows are eligible for net-of-fee dues
    matching. Zelle transfers arrive whole.
    """
    canonical = normalize_account(account if account is None else str(account))
    return "venmo" in _text(details) or canonical == ACCOUNT_VENMO


def record_date(record: dict) -> date | None:
    """
    The transaction's own date, for schedule lookups.

    Distinct from the purchase date `rule_meeting_food` digs out of the
    description: that one is needed for the weekday, this one for "which term
    was this in". Accepts the ISO strings SQLite stores, datetimes, and
    anything with a `.date()`.
    """
    value = record.get("transaction_date")
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_date = getattr(value, "date", None)  # pandas.Timestamp and friends
    if callable(to_date):
        try:
            return to_date()
        except (TypeError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


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


def rule_dues(record: dict, schedule: DuesSchedule | None = None) -> Classification | None:
    """
    An exact dues amount arriving by Venmo or Zelle.

    `schedule` supplies the rates in force for the transaction's term. Omitting
    it falls back to `DUES_AMOUNTS`, which is what the rule did before rates
    became per-term data.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return None
    details, account = record.get("details"), record.get("account")
    if not is_venmo_or_zelle(details, account):
        return None

    if schedule is None:
        if not equals_any(amount, DUES_AMOUNTS):
            return None
        return _assign(1, "Dues (exact dues amount via Venmo or Zelle)", confidence=1.0)

    when = record_date(record)
    if not schedule.matches(amount, when, is_venmo=is_venmo(details, account)):
        return None

    window = schedule.window_for(when)
    where = f" for {window.semester}" if window is not None else ""
    return _assign(1, f"Dues (dues rate{where} via Venmo or Zelle)", confidence=1.0)


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


def first_match(
    record: dict, rules: tuple[Callable[[dict], Classification | None], ...]
) -> Classification:
    for rule in rules:
        result = rule(record)
        if result is not None:
            return result
    return UNMATCHED


def exact_rules(
    dues: DuesSchedule | None = None,
) -> tuple[Callable[[dict], Classification | None], ...]:
    """
    The exact-rule chain, with `rule_dues` bound to a schedule if one is given.

    Binding here rather than giving every rule a context parameter keeps the
    other three rules plain single-argument callables, and keeps the no-schedule
    path returning the exact same tuple object it always did.
    """
    if dues is None:
        return EXACT_RULES
    return (rule_refund, rule_formal, rule_consulting, partial(rule_dues, schedule=dues))


def classify_exact(record: dict, *, dues: DuesSchedule | None = None) -> Classification:
    """Only the unambiguous rules. Returns UNMATCHED if none fire."""
    return first_match(record, exact_rules(dues))


def classify_heuristic(record: dict) -> Classification:
    """Only the keyword/weekday rules. Returns UNMATCHED if none fire."""
    return first_match(record, HEURISTIC_RULES)


def classify_deterministic(record: dict) -> Classification:
    """Apply every rule in priority order. Returns UNMATCHED if none fire."""
    return first_match(record, RULES)
