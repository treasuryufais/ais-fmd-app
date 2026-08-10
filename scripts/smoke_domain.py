"""Smoke check for the domain + data layers. Run with the sandbox venv."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.data.seed import seed
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import quality, reconcile
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.terms import ordered_semesters


def main() -> int:
    summary = seed()
    print("SEED:", summary)

    backend = SqliteBackend()
    tx = backend.fetch_transactions()
    terms = backend.fetch_terms()
    budgets = backend.fetch_budgets()

    print("transactions =", len(tx))
    print("columns =", list(tx.columns))
    print("accounts =", sorted(str(a) for a in tx["account"].dropna().unique()))
    print("uncategorized =", int(tx["budget_category"].isna().sum()))
    print("audit rows =", len(backend.fetch_audit(100000)))
    print("balances =", len(backend.fetch_statement_balances()))
    print("merchants =", len(backend.fetch_merchants()))

    semesters = ordered_semesters(terms)
    print("semesters =", semesters)

    latest = semesters[-1]
    bva = budget_domain.budget_vs_actual(tx, budgets, terms, latest)
    print("budget_vs_actual rows =", len(bva))
    print(bva.head(5).to_string(index=False))

    totals = budget_domain.semester_totals(tx, terms, latest)
    print("totals =", totals)

    results = reconcile.reconcile_all(tx, backend.fetch_statement_balances())
    unbalanced = [r for r in results if not r.balanced]
    print("reconciliation periods =", len(results), "unbalanced =", len(unbalanced))
    for r in unbalanced:
        print("  !", r.explain())

    issues = quality.run_all_checks(tx, budgets, terms)
    print("quality issues =", len(issues))
    for issue in issues:
        print(f"  [{issue.severity}] {issue.title} ({issue.count})")

    # Categorization cost accounting on a fresh batch.
    sample = tx.head(200).to_dict("records")
    run = categorize_records(sample)
    print("categorization:", run.summary_line())
    print("resolved locally =", run.rows_resolved_locally, "sent to model =", run.rows_sent_to_model)
    print("llm skipped reason =", run.llm.skipped_reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
