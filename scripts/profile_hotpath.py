"""Time the operations a page performs on every rerun, against real data."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import alerts, budgets, dues, quality, reconcile
from ais_fmd.domain.categorize.merchants import MerchantMemory
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.terms import attach_semester, default_semester, ordered_semesters


def timed(label, fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = (time.perf_counter() - start) * 1000
    flag = "  <-- SLOW" if elapsed > 250 else ""
    print(f"  {elapsed:8.1f} ms  {label}{flag}")
    return result


def main() -> int:
    backend = SqliteBackend()
    print("loading...")
    tx = timed("fetch_transactions", backend.fetch_transactions)
    terms = timed("fetch_terms", backend.fetch_terms)
    budgets_df = timed("fetch_budgets", backend.fetch_budgets)
    balances = timed("fetch_statement_balances", backend.fetch_statement_balances)
    merchants = MerchantMemory.from_records(backend.fetch_merchants().to_dict("records"))

    print(f"\nrows: {len(tx):,} transactions, {len(terms)} terms\n")
    semester = default_semester(tx, terms)

    print("per-rerun work:")
    timed("attach_semester (whole table)", attach_semester, tx, terms)
    timed("budget_vs_actual", budgets.budget_vs_actual, tx, budgets_df, terms, semester)
    timed("semester_totals", budgets.semester_totals, tx, terms, semester)
    timed(
        "historical_budget_vs_actual (all terms)",
        budgets.historical_budget_vs_actual,
        tx, budgets_df, terms, None,
    )
    timed("burn_rate_projection", budgets.burn_rate_projection, tx, budgets_df, terms, semester)
    timed("dues.summarize", dues.summarize, tx, terms, semester)
    timed("dues.compare_semesters (all)", dues.compare_semesters, tx, terms, ordered_semesters(terms))
    timed("quality.run_all_checks", quality.run_all_checks, tx, budgets_df, terms)
    timed("reconcile.reconcile_all", reconcile.reconcile_all, tx, balances)
    timed("alerts.evaluate", alerts.evaluate, tx, budgets_df, terms, balances, semester=semester)

    pending = tx[tx["budget_category"].isna() | tx["purpose"].isna()]
    timed(
        f"categorize_records (review queue, {len(pending)} rows)",
        categorize_records,
        pending.to_dict("records"),
        merchants,
    )

    order = ordered_semesters(terms)

    print("\nDashboard sparklines:")
    timed(f"batched semester_totals_by_semester ({len(order)} terms)",
          budgets.semester_totals_by_semester, tx, terms, order)

    start = time.perf_counter()
    for s in order:
        budgets.semester_totals(tx, terms, s)
        budgets.semester_totals(tx, terms, s)
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  {elapsed:8.1f} ms  [superseded] one call per term x 2 series")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
