"""Verify the new modules against whatever is currently in the sandbox database."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import alerts as alerts_domain
from ais_fmd.domain import dues as dues_domain
from ais_fmd.domain import report as report_domain
from ais_fmd.domain.terms import default_semester, ordered_semesters


def main() -> int:
    backend = SqliteBackend()
    tx = backend.fetch_transactions()
    terms = backend.fetch_terms()
    budgets = backend.fetch_budgets()
    balances = backend.fetch_statement_balances()

    print(f"transactions in sandbox: {len(tx)}")

    print("\n=== M7 DUES, by semester ===")
    comparison = dues_domain.compare_semesters(tx, terms, ordered_semesters(terms))
    populated = comparison[comparison["Payments"] > 0]
    print(populated.to_string(index=False))
    print(f"\n  total dues collected: ${populated['Collected'].sum():,.2f}")
    print(f"  total payments      : {int(populated['Payments'].sum())}")

    busiest = populated.nlargest(1, "Payments")
    if not busiest.empty:
        semester = busiest.iloc[0]["Semester"]
        roster = dues_domain.payer_roster(tx, terms, semester)
        print(f"\n  busiest term: {semester} -- {len(roster)} distinct payers identified")
        curve = dues_domain.collection_curve(tx, terms, semester)
        if not curve.empty:
            print(
                f"  collection curve: {len(curve)} active days, "
                f"first on day {int(curve['Day'].min())}, "
                f"final total ${curve['Cumulative'].iloc[-1]:,.2f}"
            )

    target = default_semester(tx, terms)
    print(f"\n=== M6 ALERTS ({target}) ===")
    alerts = alerts_domain.evaluate(tx, budgets, terms, balances, semester=target)
    if not alerts:
        print("  (none)")
    for alert in alerts:
        print(f"  [{alert.severity:>8}] {alert.title}")
        print(f"             {alert.detail[:100]}")

    print(f"\n=== M12 BOARD PACK ({target}) ===")
    pack = report_domain.build(tx, budgets, terms, target, balances)
    for line in pack.narrative:
        print(f"  {line}")
    document = report_domain.to_html(pack)
    out = Path("sandbox_data") / f"board_pack_{target.replace(' ', '_')}.html"
    out.write_text(document, encoding="utf-8")
    print(f"\n  rendered {len(document):,} bytes -> {out}")
    print(f"  self-contained (no external refs): {'http' not in document}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
