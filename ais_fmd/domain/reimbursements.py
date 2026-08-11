"""
Module M8 -- reimbursement requests.

A member submits what they spent and attaches a receipt; the treasurer approves;
the approved request then *matches itself* against the bank feed when the next
statement is uploaded.

That last part is the point. The categorizer currently reverse-engineers intent
from Venmo notes and Zelle memos -- archaeology on a string someone typed on
their phone. A reimbursement request records the committee and purpose *before*
the money moves, so when the matching payment appears it arrives already
categorized, with a receipt attached and a named requester.

Matching is intentionally conservative: exact amount, within a date window, and
an outgoing transfer. Anything ambiguous is offered as a candidate for a human
to confirm rather than linked automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from ..config.categories import committee_name
from .money import parse_amount

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
PAID = "paid"

STATUSES = (PENDING, APPROVED, REJECTED, PAID)

# A reimbursement usually clears within a couple of weeks of approval.
MATCH_WINDOW_DAYS = 45
AMOUNT_TOLERANCE = Decimal("0.01")


@dataclass
class MatchCandidate:
    request_id: int
    transaction_id: int
    transaction_date: pd.Timestamp
    amount: float
    details: str
    confidence: float
    reason: str


def summarize(df_requests: pd.DataFrame) -> dict:
    """Counts and outstanding value, for the header of the page."""
    empty = {status: 0 for status in STATUSES}
    if df_requests is None or df_requests.empty:
        return {**empty, "outstanding_amount": 0.0, "total": 0}

    counts = df_requests["status"].value_counts().to_dict()
    owed = df_requests[df_requests["status"].isin([PENDING, APPROVED])]
    return {
        **empty,
        **{str(k): int(v) for k, v in counts.items()},
        "outstanding_amount": float(owed["amount"].sum()) if not owed.empty else 0.0,
        "total": int(len(df_requests)),
    }


def display_frame(df_requests: pd.DataFrame) -> pd.DataFrame:
    """Human-readable view for a table."""
    columns = [
        "Request", "Requester", "Committee", "Amount", "Incurred",
        "Status", "Submitted", "Note",
    ]
    if df_requests is None or df_requests.empty:
        return pd.DataFrame(columns=columns)

    frame = df_requests.copy()
    return pd.DataFrame(
        {
            "Request": frame["request_id"],
            "Requester": frame["requester"],
            "Committee": frame["committee_id"].map(committee_name),
            "Amount": frame["amount"],
            "Incurred": frame["incurred_on"],
            "Status": frame["status"],
            "Submitted": pd.to_datetime(frame["submitted_at"], errors="coerce"),
            "Note": frame["decision_note"].fillna(""),
        },
        columns=columns,
    )


def find_matches(
    df_requests: pd.DataFrame,
    df_transactions: pd.DataFrame,
) -> list[MatchCandidate]:
    """
    Candidate bank transactions for each approved-but-unpaid request.

    Only outgoing transactions are considered: a reimbursement is money leaving
    the account, so an incoming payment of the same value is never the match.
    """
    if df_requests is None or df_requests.empty:
        return []
    if df_transactions is None or df_transactions.empty:
        return []

    open_requests = df_requests[df_requests["status"] == APPROVED]
    open_requests = open_requests[open_requests["matched_transaction_id"].isna()]
    if open_requests.empty:
        return []

    payments = df_transactions[df_transactions["amount"] < 0].copy()
    if payments.empty:
        return []
    payments["transaction_date"] = pd.to_datetime(payments["transaction_date"], errors="coerce")

    # Transactions already claimed by another request are not offered again.
    claimed = {
        int(value)
        for value in df_requests["matched_transaction_id"].dropna().tolist()
    }

    candidates: list[MatchCandidate] = []
    for _, request in open_requests.iterrows():
        target = parse_amount(request["amount"])
        if target is None:
            continue

        submitted = pd.to_datetime(request.get("submitted_at"), errors="coerce")
        incurred = pd.to_datetime(request.get("incurred_on"), errors="coerce")
        anchor = incurred if pd.notna(incurred) else submitted
        if pd.isna(anchor):
            continue

        for _, payment in payments.iterrows():
            transaction_id = int(payment["transactionid"])
            if transaction_id in claimed:
                continue

            paid = parse_amount(abs(payment["amount"]))
            if paid is None or abs(paid - target) > AMOUNT_TOLERANCE:
                continue

            when = payment["transaction_date"]
            if pd.isna(when):
                continue
            gap = (when - anchor).days
            if gap < -3 or gap > MATCH_WINDOW_DAYS:
                continue

            # Closer in time is a stronger match; a named requester in the
            # description is stronger still.
            confidence = max(0.55, 1.0 - (abs(gap) / (MATCH_WINDOW_DAYS * 2)))
            reason = f"Exact amount, {gap} day(s) after the expense"
            requester = str(request.get("requester") or "").split("@")[0]
            surname = requester.split(".")[-1].lower() if requester else ""
            if surname and len(surname) > 2 and surname in str(payment["details"]).lower():
                confidence = min(1.0, confidence + 0.2)
                reason += "; requester named in the description"

            candidates.append(
                MatchCandidate(
                    request_id=int(request["request_id"]),
                    transaction_id=transaction_id,
                    transaction_date=when,
                    amount=float(payment["amount"]),
                    details=str(payment["details"]),
                    confidence=round(confidence, 2),
                    reason=reason,
                )
            )

    candidates.sort(key=lambda c: -c.confidence)
    return candidates


def candidates_frame(candidates: list[MatchCandidate]) -> pd.DataFrame:
    columns = ["Request", "Transaction", "Date", "Amount", "Confidence", "Why", "Details"]
    if not candidates:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "Request": c.request_id,
                "Transaction": c.transaction_id,
                "Date": c.transaction_date,
                "Amount": c.amount,
                "Confidence": c.confidence,
                "Why": c.reason,
                "Details": c.details[:80],
            }
            for c in candidates
        ],
        columns=columns,
    )


def validate_request(amount: object, description: str, committee_id: int | None) -> str | None:
    """Return an error message, or None when the request is submittable."""
    parsed = parse_amount(amount)
    if parsed is None or parsed <= 0:
        return "Enter an amount greater than zero."
    if parsed > Decimal("10000"):
        return "Amounts over $10,000 need to go through the treasurer directly."
    if not (description or "").strip():
        return "Describe what the expense was for."
    if committee_id is None:
        return "Choose the committee this belongs to."
    return None
