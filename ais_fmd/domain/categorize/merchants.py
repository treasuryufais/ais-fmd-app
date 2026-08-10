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
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"purchase authorized on \d{1,2}/\d{1,2}", re.I),
    re.compile(r"\bcard\s+\d{3,5}\b", re.I),
    re.compile(r"\bxxxx\d+\b", re.I),
    re.compile(r"\b[a-z]\d{3,}\b", re.I),          # reference codes: S3045, P12345678
    re.compile(r"\b\d{10,}\b"),                     # long numeric ids
    re.compile(r"#\s*\d+"),                         # store numbers
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # dates
    re.compile(r"\brecurring payment\b", re.I),
    re.compile(r"\bpurchase\b", re.I),
    re.compile(r"\bauthorized\b", re.I),
)

# Trailing "GAINESVILLE FL" / "FL" style location suffixes.
_TRAILING_LOCATION = re.compile(
    r"\s+[a-z .'-]+\s+(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|"
    r"ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
    r"wa|wv|wi|wy)\s*$",
    re.I,
)

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")
# Trailing store/location numbers: "chipotle 1462" and "chipotle 1463" are the
# same merchant, so the number is dropped to let one rule cover both.
_TRAILING_DIGITS = re.compile(r"(?:\s+\d+)+$")

MIN_KEY_LENGTH = 3

# Venmo details are pipe-joined as "txn id | note | from | to". Every field is
# person- or transfer-specific, so there is no merchant to remember -- learning
# from them would create one useless rule per member. Venmo rows are handled by
# the deterministic dues/refund/formal rules instead.
_VENMO_SHAPED = re.compile(r"^\s*\d{10,}\s*\|")


def merchant_key(details: object) -> str:
    """
    Reduce a bank description to a stable merchant key.

    Returns "" when nothing usable survives, which callers treat as
    "not eligible for merchant memory".
    """
    if details is None:
        return ""
    text = str(details)

    if _VENMO_SHAPED.match(text):
        return ""
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if not part.strip().isdigit()]
        text = max(parts, key=len) if parts else ""
        if not text:
            return ""

    text = text.lower()
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _TRAILING_LOCATION.sub("", text).strip()
    text = _TRAILING_DIGITS.sub("", text).strip()
    text = _WHITESPACE.sub(" ", text).strip()

    if len(text) < MIN_KEY_LENGTH:
        return ""
    # Cap length so an unusually chatty description still keys stably.
    return " ".join(text.split()[:6])


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
