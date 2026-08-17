"""
Read-only inspection and export of the LIVE Supabase project.

This is the first thing to run against production, and it only ever issues GET
requests -- it cannot modify anything. Two jobs:

    # what is actually there, and does it match what the app expects?
    python scripts/prod/inspect_supabase.py --check

    # pull every table to local CSV, as a readable second copy
    python scripts/prod/inspect_supabase.py --export ./supabase-export

WHY THIS DOES NOT USE THE `supabase` PACKAGE. The sandbox venv deliberately does
not install it -- that absence is one of the three independent layers stopping
sandbox code from reaching a real service. Installing it here to run one export
would weaken that for everyone afterwards. PostgREST is plain HTTP, so the
standard library is enough and the guarantee stays intact.

CREDENTIALS. Read from `.streamlit/secrets.toml` (gitignored) in the same shape
the existing production app already uses:

    [supabase]
    url = "https://<ref>.supabase.co"
    key = "<anon key>"

or from the environment (SUPABASE_URL / SUPABASE_ANON_KEY). Nothing is ever
printed back: errors quote status codes and table names, never headers. The anon
key is sufficient and preferred -- do not put the service key here unless a read
genuinely fails without it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

PAGE_SIZE = 1000

# Columns the ORIGINAL app's DDL declares (.github/copilot-instructions.md in
# the ais-fmd-app repo). Drift from this is the thing worth knowing about before
# a migration runs.
EXPECTED_LIVE = {
    "committees": {"CommitteeID", "Committee_Name", "Committee_Type"},
    "terms": {"TermID", "Semester", "start_date", "end_date"},
    "committeebudgets": {"committeebudgetid", "termid", "committeeid", "budget_amount"},
    "transactions": {
        "transactionid", "transaction_date", "amount", "details",
        "budget_category", "purpose", "account",
    },
    "uploaded_files": {"id", "file_name", "uploaded_at"},
    "stagingtransactions": {
        "transactiondate", "amount", "details", "budget", "purpose", "account",
    },
}

# What migration 001 adds. Reported so you can see whether it has been applied.
MIGRATION_001_ADDS = {
    "transactions": {"source_file", "natural_key", "created_at"},
    "terms": {"locked", "locked_at", "locked_by", "dues_rates", "dues_rates_verified"},
    "uploaded_files": {"row_count", "uploaded_by"},
}

NEW_TABLES = ("transaction_audit", "merchants", "labeled_examples", "members")


def load_credentials() -> tuple[str, str]:
    """(url, key) from the gitignored secrets file, or the environment."""
    import os

    secrets_path = REPO_ROOT / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
            tomllib = None
        if tomllib is not None:
            with secrets_path.open("rb") as handle:
                data = tomllib.load(handle)
            section = data.get("supabase", {})
            url = str(section.get("url", "")).strip()
            key = str(section.get("key", "") or section.get("anon_key", "")).strip()
            if url and key:
                return url.rstrip("/"), key

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if url and key:
        return url.rstrip("/"), key

    raise SystemExit(
        "No Supabase credentials found.\n\n"
        "Create .streamlit/secrets.toml (it is gitignored) containing:\n\n"
        "    [supabase]\n"
        '    url = "https://<your-ref>.supabase.co"\n'
        '    key = "<your anon key>"\n\n'
        "or set SUPABASE_URL and SUPABASE_ANON_KEY in the environment.\n"
        "Do not paste these into a chat or commit them."
    )


def _request(url: str, key: str, path: str, params: dict, extra_headers: dict | None = None):
    """One GET against PostgREST. Returns (parsed_body, headers)."""
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}/rest/v1/{path}?{query}")
    request.add_header("apikey", key)
    request.add_header("Authorization", f"Bearer {key}")
    for name, value in (extra_headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else [], dict(response.headers)


def probe(url: str, key: str, table: str) -> dict:
    """Row count and column names for one table, without downloading it all."""
    try:
        rows, headers = _request(
            url, key, table,
            {"select": "*", "limit": "1"},
            {"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 400):
            return {"exists": False, "rows": 0, "columns": set()}
        return {"exists": False, "rows": 0, "columns": set(), "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Supabase: {exc.reason}") from exc

    total = 0
    content_range = headers.get("Content-Range", "")
    if "/" in content_range:
        tail = content_range.split("/")[-1]
        if tail.isdigit():
            total = int(tail)

    columns = set(rows[0].keys()) if rows else set()
    return {"exists": True, "rows": total, "columns": columns}


def fetch_all(url: str, key: str, table: str) -> list[dict]:
    """Every row, paginated. Stops on a short page."""
    out: list[dict] = []
    offset = 0
    while True:
        rows, _ = _request(
            url, key, table,
            {"select": "*", "limit": str(PAGE_SIZE), "offset": str(offset)},
        )
        out.extend(rows)
        if len(rows) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


def do_check(url: str, key: str) -> int:
    print(f"Connected to {url}\n")

    print("LIVE TABLES")
    drift: list[str] = []
    totals: dict[str, int] = {}
    for table, expected in EXPECTED_LIVE.items():
        info = probe(url, key, table)
        if not info["exists"]:
            print(f"   {table:22} MISSING {info.get('error', '')}")
            drift.append(f"{table}: table not found")
            continue
        totals[table] = info["rows"]
        print(f"   {table:22} {info['rows']:>7} rows")

        if info["columns"]:
            missing = expected - info["columns"]
            extra = info["columns"] - expected - MIGRATION_001_ADDS.get(table, set())
            if missing:
                drift.append(f"{table}: expected columns absent -> {sorted(missing)}")
            if extra:
                drift.append(f"{table}: undocumented columns present -> {sorted(extra)}")

    print("\nMIGRATION 001 STATUS")
    for table, added in MIGRATION_001_ADDS.items():
        info = probe(url, key, table)
        if not info["exists"] or not info["columns"]:
            print(f"   {table:22} cannot tell (no rows to sample)")
            continue
        present = added & info["columns"]
        if present == added:
            state = "applied"
        elif not present:
            state = "not applied"
        else:
            state = f"PARTIAL -- has {sorted(present)}"
        print(f"   {table:22} {state}")

    for table in NEW_TABLES:
        info = probe(url, key, table)
        print(f"   {table:22} {'present' if info['exists'] else 'not created'}")

    print("\nFINANCIAL TOTALS (compare against docs/supabase-backup.md step 6)")
    try:
        rows = fetch_all(url, key, "transactions")
        amounts = [float(r["amount"]) for r in rows if r.get("amount") is not None]
        dates = [str(r.get("transaction_date")) for r in rows if r.get("transaction_date")]
        print(f"   transactions : {len(rows)}")
        print(f"   net          : {sum(amounts):,.2f}")
        print(f"   income       : {sum(a for a in amounts if a > 0):,.2f}")
        print(f"   expenses     : {sum(a for a in amounts if a < 0):,.2f}")
        if dates:
            print(f"   date range   : {min(dates)} .. {max(dates)}")
    except urllib.error.HTTPError as exc:
        print(f"   could not read transactions: HTTP {exc.code}")

    if drift:
        print("\nSCHEMA DRIFT vs the documented DDL")
        for item in drift:
            print(f"   {item}")
        print("\nThe migration assumes the documented shape. Reconcile these first.")
    else:
        print("\nNo schema drift. The live database matches its documented DDL.")

    return 0


def do_export(url: str, key: str, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    tables = list(EXPECTED_LIVE) + list(NEW_TABLES)

    print(f"Exporting to {destination}\n")
    exported = 0
    for table in tables:
        info = probe(url, key, table)
        if not info["exists"]:
            continue
        rows = fetch_all(url, key, table)
        target = destination / f"{table}.csv"
        if rows:
            fieldnames = list(rows[0].keys())
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        else:
            target.write_text("", encoding="utf-8")
        print(f"   {table:22} {len(rows):>7} rows -> {target.name}")
        exported += 1

    print(f"\n{exported} tables exported.")
    print(
        "This is a readable copy, NOT a restorable backup -- it has no schema, "
        "constraints\nor sequences. Take the pg_dump in docs/supabase-backup.md "
        "as well.\n"
        "It contains member names and payment amounts: keep it out of git."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report schema and totals")
    group.add_argument("--export", type=Path, metavar="DIR", help="write every table to CSV")
    args = parser.parse_args()

    url, key = load_credentials()
    if args.check:
        return do_check(url, key)
    return do_export(url, key, args.export)


if __name__ == "__main__":
    raise SystemExit(main())
