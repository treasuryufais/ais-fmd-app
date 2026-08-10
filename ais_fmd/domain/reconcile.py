"""
Module M1 -- reconciliation and balance integrity.

Nothing in the original tied the app's figures to reality. Transactions were
uploaded, categorized and charted, but no step ever asked whether the resulting
ledger agreed with what the bank said the balance was.

This module closes that loop: given a statement's opening and closing balance,
assert that opening + the period's transactions == closing. When it does not
balance, report the gap in dollars rather than just failing.

This is the difference between a dashboard and books that can be defended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from ..config.categories import normalize_account
from .money import format_currency, parse_amount

# Balances within this tolerance are treated as agreeing (rounding noise only).
TOLERANCE = Decimal("0.01")


@dataclass
class ReconciliationResult:
    account: str
    period_start: pd.Timestamp
    period_end: pd.Timestamp
    opening_balance: Decimal
    closing_balance: Decimal
    expected_closing: Decimal
    transaction_total: Decimal
    transaction_count: int
    discrepancy: Decimal
    date_gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)

    @property
    def balanced(self) -> bool:
        return abs(self.discrepancy) <= TOLERANCE

    @property
    def status(self) -> str:
        if self.balanced:
            return "balanced"
        return "short" if self.discrepancy < 0 else "over"

    def explain(self) -> str:
        if self.balanced:
            return (
                f"{self.account} reconciles for "
                f"{self.period_start:%b %d} – {self.period_end:%b %d, %Y}: "
                f"{self.transaction_count} transactions totalling "
                f"{format_currency(self.transaction_total, signed=True)} take "
                f"{format_currency(self.opening_balance)} to "
                f"{format_currency(self.closing_balance)}."
            )
        direction = "less" if self.discrepancy < 0 else "more"
        return (
            f"{self.account} does not reconcile. The ledger accounts for "
            f"{format_currency(abs(self.discrepancy))} {direction} than the bank "
            f"reports. Expected closing {format_currency(self.expected_closing)}, "
            f"statement says {format_currency(self.closing_balance)}."
        )


def reconcile_period(
    df_transactions: pd.DataFrame,
    *,
    account: str,
    period_start: object,
    period_end: object,
    opening_balance: object,
    closing_balance: object,
) -> ReconciliationResult:
    """Reconcile one account over one statement period."""
    start = pd.to_datetime(period_start)
    end = pd.to_datetime(period_end)
    canonical = normalize_account(account) or str(account)

    opening = parse_amount(opening_balance) or Decimal("0.00")
    closing = parse_amount(closing_balance) or Decimal("0.00")

    scoped = _scope(df_transactions, canonical, start, end)

    total = Decimal("0.00")
    for value in scoped["amount"]:
        parsed = parse_amount(value)
        if parsed is not None:
            total += parsed

    expected = opening + total
    return ReconciliationResult(
        account=canonical,
        period_start=start,
        period_end=end,
        opening_balance=opening,
        closing_balance=closing,
        expected_closing=expected,
        transaction_total=total,
        transaction_count=int(len(scoped)),
        discrepancy=expected - closing,
        date_gaps=find_date_gaps(scoped, start, end),
    )


def _scope(
    df_transactions: pd.DataFrame,
    account: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    if df_transactions.empty:
        return df_transactions
    scoped = df_transactions.copy()
    scoped["transaction_date"] = pd.to_datetime(scoped["transaction_date"], errors="coerce")
    scoped["_account"] = scoped["account"].map(normalize_account)
    mask = (
        (scoped["_account"] == account)
        & (scoped["transaction_date"] >= start)
        & (scoped["transaction_date"] <= end)
    )
    return scoped[mask].drop(columns=["_account"])


def find_date_gaps(
    scoped: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    min_gap_days: int = 14,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """
    Stretches with no activity at all.

    A two-week silence in an active account usually means a statement was never
    uploaded, not that nobody spent anything.
    """
    if scoped.empty:
        return [(start, end)]

    dates = pd.to_datetime(scoped["transaction_date"], errors="coerce").dropna().sort_values()
    if dates.empty:
        return [(start, end)]

    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    boundaries = [start] + dates.tolist() + [end]
    for earlier, later in zip(boundaries, boundaries[1:]):
        if (later - earlier).days >= min_gap_days:
            gaps.append((earlier, later))
    return gaps


def reconcile_all(
    df_transactions: pd.DataFrame,
    df_balances: pd.DataFrame,
) -> list[ReconciliationResult]:
    """Reconcile every recorded statement period."""
    if df_balances.empty:
        return []
    results = []
    for _, row in df_balances.iterrows():
        results.append(
            reconcile_period(
                df_transactions,
                account=row["account"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                opening_balance=row["opening_balance"],
                closing_balance=row["closing_balance"],
            )
        )
    return sorted(results, key=lambda r: (r.account, r.period_start))


def to_frame(results: list[ReconciliationResult]) -> pd.DataFrame:
    """Tabular view for display."""
    columns = [
        "Account", "Period", "Opening", "Transactions", "Expected Closing",
        "Statement Closing", "Discrepancy", "Status",
    ]
    if not results:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "Account": r.account,
                "Period": f"{r.period_start:%Y-%m-%d} → {r.period_end:%Y-%m-%d}",
                "Opening": float(r.opening_balance),
                "Transactions": r.transaction_count,
                "Expected Closing": float(r.expected_closing),
                "Statement Closing": float(r.closing_balance),
                "Discrepancy": float(r.discrepancy),
                "Status": r.status,
            }
            for r in results
        ],
        columns=columns,
    )
