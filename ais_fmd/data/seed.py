"""
Sandbox seed data.

Generates a plausible four-semester history so every page has something real to
show. The generator is deterministic (fixed RNG seed), so the sandbox looks the
same on every reset and tests can rely on it.

Deliberate imperfections are planted so the diagnostic modules have genuine
findings rather than an artificially clean database:

  * one statement period that does not reconcile        -> Reconciliation (M1)
  * a handful of rows with the legacy 'Wells' account   -> Data Quality (F13)
  * two $0.00 rows, the fingerprint of a parse failure  -> Data Quality (F5)
  * ~12% of rows left uncategorized                     -> Review Queue (M3)
  * two genuine same-day same-amount purchases          -> exercises the F3 fix
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from ..config.categories import (
    ACCOUNT_VENMO,
    ACCOUNT_WELLS_FARGO,
    BUDGETED_COMMITTEE_IDS,
    COMMITTEES,
)
from ..domain.categorize.predicates import COMMITTEE_PURPOSE
from ..domain.dedupe import assign_natural_keys
from ..domain.dues import CONFIRMED_DUES_RATES, format_rates
from .sqlite_backend import SqliteBackend

SEED = 20260810

TERMS = [
    ("FA24", "Fall 2024", date(2024, 8, 21), date(2024, 12, 13)),
    ("SP25", "Spring 2025", date(2025, 1, 6), date(2025, 5, 2)),
    ("FA25", "Fall 2025", date(2025, 8, 20), date(2025, 12, 12)),
    ("SP26", "Spring 2026", date(2026, 1, 5), date(2026, 5, 1)),
    ("FA26", "Fall 2026", date(2026, 8, 15), date(2026, 12, 18)),
]

# Seeded from the constant `rule_dues` used before rates became per-term data,
# and deliberately left marked unverified: these are a copy of an assumption,
# not a record of what any term charged. `quality.check_unverified_dues_rates`
# surfaces them until a treasurer confirms each one.
SEED_DUES_RATES = "35.00,52.50"

# Tuned so a realistic mix results: most committees on track, one or two
# approaching, and one genuinely over -- so the semantic chart colouring has
# every state to show.
BUDGET_BASELINE = {
    2: 700.0, 4: 900.0, 5: 2200.0, 6: 1500.0, 7: 1200.0, 8: 2800.0,
    9: 1400.0, 10: 2000.0, 12: 1600.0, 13: 2300.0, 14: 2100.0,
    15: 1900.0, 16: 900.0, 18: 3200.0,
}

FOOD_MERCHANTS = [
    "PUBLIX SUPER MAR", "CHIPOTLE 1462", "PANDA EXPRESS 2201",
    "PIESANOS STONE FIRED", "CHICK-FIL-A #02145", "HANA SUSHI",
    "LAS CARRETAS MEXIC", "MENCHIES GAINESVILLE", "JIMMY JOHNS 1204",
]
BAR_MERCHANTS = [
    "MACDINTONS PUB", "SALTY DOG SALOON", "ARCADE BAR GNV",
    "THE GROVE - GA", "TOTAL WINE AND MORE", "FIRST MAGNITUD BREWING",
]
OTHER_MERCHANTS = [
    ("AMAZON MKTPL 8H21", 15, "Technology"),
    ("CANVA PTY LTD", 15, "Technology"),
    ("VISTAPRINT CORP", 13, "Merch"),
    ("CUSTOMINK LLC", 13, "Merch"),
    ("MARRIOTT ORLANDO", 14, "Road Trip"),
    ("DELTA AIR LINES", 14, "Road Trip"),
    ("EVENTBRITE CONF", 10, "Professional Development"),
    ("META PLATFORMS ADS", 9, "Marketing"),
    ("STICKER MULE", 9, "Marketing"),
    ("UF REITZ UNION", 12, "Misc."),
    ("STAPLES 00114", 12, "Misc."),
]

MEMBER_NAMES = [
    "Ava Mitchell", "Noah Patel", "Sofia Ramirez", "Liam Chen", "Maya Okafor",
    "Ethan Brooks", "Priya Nair", "Jonas Weber", "Zara Ahmed", "Diego Alvarez",
    "Hana Kim", "Owen Fitzgerald", "Ines Duarte", "Caleb Nguyen", "Rania Haddad",
]


def _wells_details(merchant: str, purchase_day: date, card: str = "1234") -> str:
    return (
        f"PURCHASE AUTHORIZED ON {purchase_day.month:02d}/{purchase_day.day:02d} "
        f"{merchant} GAINESVILLE FL S{random.randint(10**11, 10**12 - 1)} CARD {card}"
    )


def _venmo_details(note: str, sender: str, recipient: str = "UF AIS") -> str:
    txn_id = random.randint(3_000_000_000_000_000, 3_999_999_999_999_999)
    return f"{txn_id} | {note} | {sender} | {recipient}"


def _next_weekday(start: date, weekday: int) -> date:
    """First `weekday` (0=Mon) on or after `start`."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def build_records() -> list[dict]:
    rng = random.Random(SEED)
    random.seed(SEED)
    records: list[dict] = []

    for term_id, _semester, start, end in TERMS:
        # --- Dues income, concentrated in the first three weeks -------------
        for _ in range(rng.randint(28, 46)):
            day = start + timedelta(days=rng.randint(0, 21))
            amount = rng.choice([35.00, 35.00, 35.00, 52.50])
            member = rng.choice(MEMBER_NAMES)
            records.append(
                {
                    "transaction_date": day.isoformat(),
                    "amount": amount,
                    "details": _venmo_details("Fall dues", member),
                    "account": ACCOUNT_VENMO,
                }
            )

        # --- Weekly GBM food, always a Tuesday -----------------------------
        meeting_day = _next_weekday(start, 1)
        while meeting_day <= end:
            if rng.random() < 0.82:
                merchant = rng.choice(FOOD_MERCHANTS)
                posted = meeting_day + timedelta(days=rng.randint(0, 2))
                records.append(
                    {
                        "transaction_date": posted.isoformat(),
                        "amount": -round(rng.uniform(58, 240), 2),
                        "details": _wells_details(merchant, meeting_day),
                        "account": ACCOUNT_WELLS_FARGO,
                    }
                )
            meeting_day += timedelta(days=7)

        # --- Membership socials at bars ------------------------------------
        for _ in range(rng.randint(4, 8)):
            day = start + timedelta(days=rng.randint(10, (end - start).days - 5))
            merchant = rng.choice(BAR_MERCHANTS)
            records.append(
                {
                    "transaction_date": day.isoformat(),
                    "amount": -round(rng.uniform(90, 420), 2),
                    "details": _wells_details(merchant, day),
                    "account": ACCOUNT_WELLS_FARGO,
                }
            )

        # --- Consulting card ------------------------------------------------
        for _ in range(rng.randint(3, 6)):
            day = start + timedelta(days=rng.randint(5, (end - start).days - 5))
            records.append(
                {
                    "transaction_date": day.isoformat(),
                    "amount": -round(rng.uniform(40, 260), 2),
                    "details": _wells_details("ZOOM.US 888", day, card="8408"),
                    "account": ACCOUNT_WELLS_FARGO,
                }
            )

        # --- Everything else -------------------------------------------------
        for _ in range(rng.randint(18, 30)):
            merchant, committee_id, purpose = rng.choice(OTHER_MERCHANTS)
            day = start + timedelta(days=rng.randint(3, (end - start).days - 3))
            records.append(
                {
                    "transaction_date": day.isoformat(),
                    "amount": -round(rng.uniform(25, 700), 2),
                    "details": _wells_details(merchant, day),
                    "account": ACCOUNT_WELLS_FARGO,
                    "_hint": (committee_id, purpose),
                }
            )

        # --- Formal ticket income --------------------------------------------
        if term_id.startswith("SP"):
            for _ in range(rng.randint(20, 34)):
                day = start + timedelta(days=rng.randint(40, 80))
                records.append(
                    {
                        "transaction_date": day.isoformat(),
                        "amount": round(rng.uniform(45, 75), 2),
                        "details": _venmo_details("Spring formal ticket", rng.choice(MEMBER_NAMES)),
                        "account": ACCOUNT_VENMO,
                    }
                )

        # --- Reimbursements out ----------------------------------------------
        for _ in range(rng.randint(5, 10)):
            day = start + timedelta(days=rng.randint(5, (end - start).days - 2))
            records.append(
                {
                    "transaction_date": day.isoformat(),
                    "amount": -round(rng.uniform(20, 180), 2),
                    "details": _venmo_details("Reimbursement", "UF AIS", rng.choice(MEMBER_NAMES)),
                    "account": ACCOUNT_VENMO,
                }
            )

        # --- Corporate sponsorship -------------------------------------------
        for _ in range(rng.randint(1, 3)):
            day = start + timedelta(days=rng.randint(10, 60))
            records.append(
                {
                    "transaction_date": day.isoformat(),
                    "amount": round(rng.uniform(750, 3500), 2),
                    "details": f"DEPOSIT CORPORATE SPONSORSHIP REF{rng.randint(10000, 99999)}",
                    "account": ACCOUNT_WELLS_FARGO,
                    "_hint": (6, "Sponsorship / Donation"),
                }
            )

    # --- Planted imperfections ---------------------------------------------

    # Two genuine same-day, same-amount, same-merchant purchases. The old dedupe
    # key would have silently dropped the second; the occurrence ordinal keeps
    # both (FINDING F3).
    twin_day = date(2025, 9, 16)
    for _ in range(2):
        records.append(
            {
                "transaction_date": twin_day.isoformat(),
                "amount": -4.50,
                "details": _wells_details("STARBUCKS 00417", twin_day),
                "account": ACCOUNT_WELLS_FARGO,
            }
        )

    # Legacy account spelling (FINDING F13).
    for offset in range(4):
        day = date(2024, 10, 3) + timedelta(days=offset)
        records.append(
            {
                "transaction_date": day.isoformat(),
                "amount": -round(rng.uniform(30, 90), 2),
                "details": _wells_details("OFFICE DEPOT 2213", day),
                "account": "Wells",  # deliberately non-canonical
            }
        )

    # Zero-amount rows -- the fingerprint of the old parse-failure behaviour.
    for offset in range(2):
        day = date(2025, 2, 11) + timedelta(days=offset)
        records.append(
            {
                "transaction_date": day.isoformat(),
                "amount": 0.0,
                "details": _wells_details("UNREADABLE MERCHANT", day),
                "account": ACCOUNT_WELLS_FARGO,
            }
        )

    records.sort(key=lambda r: r["transaction_date"])
    return records


def _categorize_for_seed(records: list[dict]) -> list[dict]:
    """
    Apply the real pipeline, then leave a slice uncategorized on purpose so the
    review queue has genuine work in it.
    """
    from ..domain.categorize.pipeline import categorize_records

    hints = [record.pop("_hint", None) for record in records]
    run = categorize_records(records)

    rng = random.Random(SEED + 1)
    out: list[dict] = []
    for record, hint, classification in zip(records, hints, run.classifications):
        committee_id = classification.committee_id
        purpose = classification.purpose
        if committee_id is None and hint is not None:
            committee_id, purpose = hint
        # Leave roughly 12% deliberately unassigned.
        if committee_id is not None and rng.random() < 0.12:
            committee_id, purpose = None, None
        out.append(
            {
                **record,
                "budget_category": committee_id,
                "purpose": purpose or (COMMITTEE_PURPOSE.get(committee_id) if committee_id else None),
            }
        )
    return out


def statement_balances(records: list[dict]) -> list[dict]:
    """
    Build reconcilable statement periods, then break exactly one of them so the
    reconciliation page has a real discrepancy to surface.
    """
    from collections import defaultdict

    periods: dict[tuple[str, str], list[float]] = defaultdict(list)
    for record in records:
        account = record["account"]
        canonical = "Wells Fargo" if account.lower().startswith("wells") else account
        month = record["transaction_date"][:7]
        periods[(canonical, month)].append(float(record["amount"]))

    balances = []
    running: dict[str, float] = defaultdict(lambda: 5000.0)
    for (account, month), amounts in sorted(periods.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        year, mon = int(month[:4]), int(month[5:7])
        period_start = date(year, mon, 1)
        period_end = (
            date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
        )
        opening = running[account]
        closing = round(opening + sum(amounts), 2)
        running[account] = closing
        balances.append(
            {
                "account": account,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "opening_balance": round(opening, 2),
                "closing_balance": closing,
                "source_file": f"{account.replace(' ', '')}_{month}.csv",
            }
        )

    # Plant one discrepancy: a $124.80 gap, as if a transaction were never imported.
    for balance in balances:
        if balance["account"] == "Wells Fargo" and balance["period_start"].startswith("2025-10"):
            balance["closing_balance"] = round(balance["closing_balance"] - 124.80, 2)
            break

    return balances


def seed(backend: SqliteBackend | None = None, *, reset: bool = True) -> dict:
    """Populate the sandbox database. Returns a summary for display."""
    backend = backend or SqliteBackend()
    if reset:
        backend.reset()

    actor = "seed@sandbox.local"

    with backend._connect() as connection:  # noqa: SLF001 - seeding is backend-internal
        connection.executemany(
            "INSERT OR REPLACE INTO committees (CommitteeID, Committee_Name, Committee_Type) "
            "VALUES (?, ?, ?)",
            [(c.id, c.name, "committee" if c.kind == "committee" else "ledger") for c in COMMITTEES],
        )
        # Terms treasury has confirmed carry their real rates and a verified
        # flag; the rest keep the seeded assumption and stay flagged, so a fresh
        # sandbox reproduces the same mix of confirmed and outstanding that the
        # real database has.
        connection.executemany(
            "INSERT OR REPLACE INTO terms "
            "(TermID, Semester, start_date, end_date, dues_rates, dues_rates_verified) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    term_id,
                    semester,
                    start.isoformat(),
                    end.isoformat(),
                    format_rates(CONFIRMED_DUES_RATES[semester])
                    if semester in CONFIRMED_DUES_RATES
                    else SEED_DUES_RATES,
                    1 if semester in CONFIRMED_DUES_RATES else 0,
                )
                for term_id, semester, start, end in TERMS
            ],
        )
        connection.commit()

    rng = random.Random(SEED + 2)
    for term_id, *_ in TERMS:
        allocations = {
            committee_id: round(BUDGET_BASELINE.get(committee_id, 500.0) * rng.uniform(0.85, 1.25), 2)
            for committee_id in BUDGETED_COMMITTEE_IDS
        }
        backend.upsert_budgets(term_id, allocations, actor)

    raw = build_records()
    categorized = _categorize_for_seed(raw)
    keyed = assign_natural_keys(categorized)
    receipt = backend.insert_transactions(keyed, "sandbox_seed.csv", actor)

    for balance in statement_balances(raw):
        backend.record_statement_balance(balance, actor)

    # Pre-seed merchant memory so M4 has a baseline.
    #
    # Keys must be derived from a *representative full bank description*, not a
    # bare merchant name -- the normaliser strips location and store-number
    # noise, so "PUBLIX SUPER MAR" and the real statement line reduce to
    # different keys unless both go through the same path.
    from ..domain.categorize.merchants import merchant_key

    known = [
        ("PUBLIX SUPER MAR", 8, "Meeting Food"),
        ("CHIPOTLE 1462", 8, "Meeting Food"),
        ("MACDINTONS PUB", 5, "Food & Drink"),
        ("VISTAPRINT CORP", 13, "Merch"),
        ("CANVA PTY LTD", 15, "Technology"),
        ("AMAZON MKTPL 8H21", 15, "Technology"),
        ("CUSTOMINK LLC", 13, "Merch"),
        ("EVENTBRITE CONF", 10, "Professional Development"),
    ]
    reference_day = date(2025, 9, 16)
    rules = []
    for name, committee_id, purpose in known:
        key = merchant_key(_wells_details(name, reference_day))
        if key:
            rules.append(
                {
                    "merchant_key": key,
                    "canonical_name": name.title(),
                    "committee_id": committee_id,
                    "purpose": purpose,
                    "hit_count": 5,
                    "source": "seeded",
                }
            )
    backend.upsert_merchants(rules, actor)

    return {
        "transactions": receipt.inserted,
        "duplicates_skipped": receipt.duplicates_skipped,
        "terms": len(TERMS),
        "committees": len(COMMITTEES),
        "merchants": len(rules),
        "database": str(backend.path),
    }
