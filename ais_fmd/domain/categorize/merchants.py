"""
Module M4 -- merchant memory.

The highest-leverage cost reduction available, because most transactions are
repeat merchants. Once "PUBLIX #1234 GAINESVILLE FL" has been resolved once, it
never needs a model again.

A merchant key is the bank description with all the per-transaction noise
stripped out: the purchase date, card number, transaction ID, store number,
trailing city/state, and reference numbers. What remains is the stable part --
the merchant name.

Corrections made in the review queue write back here, so the set of rows that
still need a model shrinks every time the treasurer works the queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .predicates import Classification

# Noise patterns, stripped in order.
#
# Every pattern uses \s+ rather than a literal space: real Wells Fargo
# descriptions pad with runs of spaces ("PURCHASE      AUTHORIZED ON   05/17"),
# and single-space patterns silently matched nothing on real statements.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:purchase|recurring\s+payment)\s+authorized\s+on\s+\d{1,2}/\d{1,2}", re.I),
    re.compile(r"\bcard\s+\d{3,5}\b", re.I),
    re.compile(r"\bxxxx\d+\b", re.I),
    re.compile(r"\bref\s*#?\s*\w+\b", re.I),        # "REF # JFR0TQ6QPUIM"
    re.compile(r"\b[a-z]\d{3,}\b", re.I),           # reference codes: S3045
    re.compile(r"\b\d{10,}\b"),                     # long numeric ids
    re.compile(r"\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b"),  # phone numbers
    re.compile(r"#\s*\d+"),                         # store numbers
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # dates
    re.compile(r"\brecurring\s+payment\b", re.I),
    re.compile(r"\bpurchase\b", re.I),
    re.compile(r"\bauthorized\b", re.I),
    re.compile(r"^\s*on\b", re.I),                  # leftover "ON" once the date goes
)

# Trailing two-letter state code only.
#
# This previously tried to strip the whole "CITY ST" suffix with
# `\s+[a-z .'-]+\s+(state)$`, but `[a-z .'-]+` is greedy and ate the merchant
# name as well: "WAL-MART SUPERCENTER GAINESVILLE FL" reduced to "wal", and
# "TST* HUEY MAGOOS ..." to "tst". Keys that short collide across unrelated
# merchants -- one "tst" rule would have mis-categorised every Toast-POS
# restaurant at once. Only the state code is stripped now; the token cap below
# does the rest of the work, and over-splitting is safe where collisions are not.
_TRAILING_STATE = re.compile(
    r"\s+(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|"
    r"ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
    r"wa|wv|wi|wy)\s*$",
    re.I,
)

# Point-of-sale processor prefixes sitting in front of the real merchant name:
# "TST* MI APA", "SQ *COFFEE", "PP*STORE". The asterisk is required -- without
# it the pattern would strip legitimate leading words like "sp" in "sports".
_POS_PREFIX = re.compile(r"^(?:tst|sq|sp|pp|py|paypal|ecs|dd|ab)\s*\*\s*", re.I)

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")
# Trailing store/location numbers: "chipotle 1462" and "chipotle 1463" are the
# same merchant, so the number is dropped to let one rule cover both.
_TRAILING_DIGITS = re.compile(r"(?:\s+\d+)+$")

MIN_KEY_LENGTH = 4
# Enough tokens to stay distinctive. Splitting one merchant across two keys just
# means learning two rules; merging two merchants into one key mis-categorises.
MAX_KEY_TOKENS = 3

# Words that are never a merchant on their own. A single-token key from this set
# is rejected rather than becoming a rule that matches half the statement.
_STOPWORD_KEYS = frozenset(
    {
        "the", "and", "for", "from", "with", "inc", "llc", "corp", "co",
        "on", "ref", "tst", "sq", "sp", "pos", "pay", "payment", "purchase",
        "return", "refund", "deposit", "withdrawal", "transfer", "check",
        "debit", "credit", "card", "online", "mobile", "recurring", "www",
    }
)

# Person-to-person transfers have no merchant to remember. Learning from them
# would create one useless rule per member -- and worse, a rule keyed on a
# member's name that then mis-categorises unrelated payments from that person.
# These rows are handled by the deterministic dues/refund/formal rules instead.
#
# Venmo details are pipe-joined as "txn id | note | from | to".
_VENMO_SHAPED = re.compile(r"^\s*\d{10,}\s*\|")
# Zelle rows read "ZELLE FROM JANE DOE ON 07/08 REF # ..." -- confirmed against
# a real statement, where these were producing merchant keys like
# "zelle from singh tanveer on ref".
_TRANSFER_SHAPED = re.compile(r"^\s*(?:zelle|venmo)\s+(?:from|to)\b", re.I)


def merchant_key(details: object) -> str:
    """
    Reduce a bank description to a stable merchant key.

    Returns "" when nothing usable survives, which callers treat as
    "not eligible for merchant memory".
    """
    if details is None:
        return ""
    # Collapse the runs of spaces real statements use, before anything else.
    text = _WHITESPACE.sub(" ", str(details)).strip()

    if _VENMO_SHAPED.match(text) or _TRANSFER_SHAPED.match(text):
        return ""
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if not part.strip().isdigit()]
        text = max(parts, key=len) if parts else ""
        if not text:
            return ""

    text = text.lower()
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _POS_PREFIX.sub("", text)
    text = _NON_ALNUM.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _TRAILING_STATE.sub("", text).strip()
    text = _TRAILING_DIGITS.sub("", text).strip()

    # Drop standalone numeric tokens -- store numbers like "CHIPOTLE 1462" vs
    # "CHIPOTLE 1893" are the same merchant. The first token is kept so names
    # that genuinely begin with a number ("7 ELEVEN", "5 GUYS") survive.
    tokens = [
        token
        for index, token in enumerate(text.split())
        if token and (index == 0 or not token.isdigit())
    ]
    if not tokens:
        return ""

    key = " ".join(tokens[:MAX_KEY_TOKENS])
    if len(key) < MIN_KEY_LENGTH:
        return ""
    # A single generic word would match far too much to be a safe rule.
    if len(tokens) == 1 and tokens[0] in _STOPWORD_KEYS:
        return ""
    return key


@dataclass(frozen=True)
class MerchantRule:
    key: str
    canonical_name: str
    committee_id: int
    purpose: str
    hit_count: int = 0
    source: str = "learned"


class MerchantMemory:
    """In-memory index over the merchants table."""

    def __init__(self, rules: list[MerchantRule] | None = None) -> None:
        self._rules: dict[str, MerchantRule] = {}
        for rule in rules or []:
            self._rules[rule.key] = rule

    @classmethod
    def from_records(cls, records: list[dict]) -> "MerchantMemory":
        rules = [
            MerchantRule(
                key=str(record["merchant_key"]),
                canonical_name=str(record.get("canonical_name") or record["merchant_key"]),
                committee_id=int(record["committee_id"]),
                purpose=str(record.get("purpose") or ""),
                hit_count=int(record.get("hit_count") or 0),
                source=str(record.get("source") or "learned"),
            )
            for record in records
            if record.get("merchant_key") and record.get("committee_id") is not None
        ]
        return cls(rules)

    def __len__(self) -> int:
        return len(self._rules)

    def lookup(self, details: object) -> Classification | None:
        key = merchant_key(details)
        if not key:
            return None
        rule = self._rules.get(key)
        if rule is None:
            return None
        return Classification(
            committee_id=rule.committee_id,
            purpose=rule.purpose or None,
            rule=f"Merchant memory: {rule.canonical_name}",
            confidence=0.95,
            source="merchant",
        )

    def remember(
        self,
        details: object,
        committee_id: int,
        purpose: str | None,
        *,
        canonical_name: str | None = None,
    ) -> MerchantRule | None:
        """Learn a mapping. Returns the rule, or None if the key is unusable."""
        key = merchant_key(details)
        if not key:
            return None
        existing = self._rules.get(key)
        rule = MerchantRule(
            key=key,
            canonical_name=canonical_name or (existing.canonical_name if existing else key),
            committee_id=committee_id,
            purpose=purpose or "",
            hit_count=(existing.hit_count if existing else 0) + 1,
        )
        self._rules[key] = rule
        return rule

    def as_records(self) -> list[dict]:
        return [
            {
                "merchant_key": rule.key,
                "canonical_name": rule.canonical_name,
                "committee_id": rule.committee_id,
                "purpose": rule.purpose,
                "hit_count": rule.hit_count,
                "source": rule.source,
            }
            for rule in sorted(self._rules.values(), key=lambda r: -r.hit_count)
        ]
