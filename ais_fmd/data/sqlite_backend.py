"""
The sandbox backend: a local SQLite file.

Properties that make this safe to hand to someone for testing:

  * It is created on demand under `sandbox_data/` and can be deleted freely.
  * It never opens a socket. There is no configuration that points it anywhere.
  * Uploads are genuinely atomic -- SQLite gives real transactions, so the
    partial-write failure mode described in FINDING F4 cannot occur.
  * The unique index on `natural_key` enforces deduplication in the database
    rather than in a pandas filter (FINDING F3).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

from .. import settings
from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt

SCHEMA = """
CREATE TABLE IF NOT EXISTS committees (
    CommitteeID     INTEGER PRIMARY KEY,
    Committee_Name  TEXT NOT NULL UNIQUE,
    Committee_Type  TEXT NOT NULL DEFAULT 'committee'
);

CREATE TABLE IF NOT EXISTS terms (
    TermID      TEXT PRIMARY KEY,
    Semester    TEXT,
    start_date  TEXT,
    end_date    TEXT
);

CREATE TABLE IF NOT EXISTS committeebudgets (
    committeebudgetid INTEGER PRIMARY KEY AUTOINCREMENT,
    termid            TEXT NOT NULL,
    committeeid       INTEGER NOT NULL,
    budget_amount     REAL,
    UNIQUE (termid, committeeid),
    FOREIGN KEY (termid) REFERENCES terms (TermID),
    FOREIGN KEY (committeeid) REFERENCES committees (CommitteeID)
);

CREATE TABLE IF NOT EXISTS transactions (
    transactionid    INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_date TEXT NOT NULL,
    amount           REAL NOT NULL,
    details          TEXT,
    budget_category  INTEGER,
    purpose          TEXT,
    account          TEXT,
    source_file      TEXT,
    natural_key      TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (budget_category) REFERENCES committees (CommitteeID)
);

-- FINDING F3: deduplication enforced by the database, not by a pandas filter.
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_natural_key
    ON transactions (natural_key) WHERE natural_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions (transaction_date);
CREATE INDEX IF NOT EXISTS ix_transactions_committee ON transactions (budget_category);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name   TEXT NOT NULL UNIQUE,
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    row_count   INTEGER DEFAULT 0,
    uploaded_by TEXT
);

-- Module M2: audit trail.
CREATE TABLE IF NOT EXISTS transaction_audit (
    audit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER,
    action         TEXT NOT NULL,
    field          TEXT,
    old_value      TEXT,
    new_value      TEXT,
    actor          TEXT,
    changed_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_audit_txn ON transaction_audit (transaction_id);

-- Module M4: merchant memory.
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_key   TEXT NOT NULL UNIQUE,
    canonical_name TEXT,
    committee_id   INTEGER,
    purpose        TEXT,
    hit_count      INTEGER DEFAULT 0,
    source         TEXT DEFAULT 'learned',
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Module M1: reconciliation inputs.
CREATE TABLE IF NOT EXISTS statement_balances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    opening_balance REAL NOT NULL,
    closing_balance REAL NOT NULL,
    source_file     TEXT,
    recorded_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account, period_start, period_end)
);
"""


class SqliteBackend(Backend):
    name = "sandbox-sqlite"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else settings.sandbox_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # --- plumbing ------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.commit()

    def _read(self, query: str, params: tuple = ()) -> pd.DataFrame:
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        if not rows:
            return pd.DataFrame(columns=_columns_for(query))
        return pd.DataFrame([dict(row) for row in rows])

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        transaction_id: int | None,
        action: str,
        actor: str,
        field: str | None = None,
        old_value: object = None,
        new_value: object = None,
    ) -> None:
        connection.execute(
            "INSERT INTO transaction_audit "
            "(transaction_id, action, field, old_value, new_value, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                transaction_id,
                action,
                field,
                None if old_value is None else str(old_value),
                None if new_value is None else str(new_value),
                actor,
            ),
        )

    # --- reads ---------------------------------------------------------------

    def fetch_committees(self) -> pd.DataFrame:
        return self._read("SELECT * FROM committees ORDER BY CommitteeID")

    def fetch_terms(self) -> pd.DataFrame:
        return self._read("SELECT * FROM terms ORDER BY start_date")

    def fetch_budgets(self) -> pd.DataFrame:
        return self._read("SELECT * FROM committeebudgets ORDER BY termid, committeeid")

    def fetch_transactions(self) -> pd.DataFrame:
        frame = self._read("SELECT * FROM transactions ORDER BY transaction_date, transactionid")
        if not frame.empty:
            frame["transaction_date"] = pd.to_datetime(frame["transaction_date"], errors="coerce")
            frame["budget_category"] = frame["budget_category"].astype("Int64")
        return frame

    def fetch_uploaded_files(self) -> pd.DataFrame:
        return self._read("SELECT * FROM uploaded_files ORDER BY uploaded_at DESC")

    def fetch_merchants(self) -> pd.DataFrame:
        return self._read("SELECT * FROM merchants ORDER BY hit_count DESC")

    def fetch_audit(self, limit: int = 500) -> pd.DataFrame:
        return self._read(
            "SELECT * FROM transaction_audit ORDER BY audit_id DESC LIMIT ?", (limit,)
        )

    def fetch_statement_balances(self) -> pd.DataFrame:
        return self._read("SELECT * FROM statement_balances ORDER BY account, period_start")

    def is_file_uploaded(self, file_name: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM uploaded_files WHERE file_name = ?", (file_name,)
            ).fetchone()
        return row is not None

    # --- writes --------------------------------------------------------------

    def insert_transactions(
        self, records: list[dict], file_name: str, actor: str
    ) -> UploadReceipt:
        """
        FINDING F4. Transactions and the uploaded_files marker are written inside
        one transaction. Either both land or neither does, so a partial failure
        can no longer leave a statement importable a second time.
        """
        receipt = UploadReceipt(file_name=file_name)
        if not records:
            receipt.error = "No records to insert."
            return receipt

        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                inserted = 0
                skipped = 0
                for record in records:
                    # Targeted conflict clause, not a blanket INSERT OR IGNORE.
                    #
                    # OR IGNORE swallows *every* constraint violation, including
                    # foreign-key failures -- so a transaction referencing a
                    # committee that does not exist would vanish silently and the
                    # upload would report success. Naming the natural_key index as
                    # the conflict target means duplicates are skipped while any
                    # other violation raises and rolls the whole import back.
                    cursor = connection.execute(
                        "INSERT INTO transactions "
                        "(transaction_date, amount, details, budget_category, purpose, "
                        " account, source_file, natural_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (natural_key) WHERE natural_key IS NOT NULL "
                        "DO NOTHING",
                        (
                            record.get("transaction_date"),
                            record.get("amount"),
                            record.get("details"),
                            record.get("budget_category"),
                            record.get("purpose"),
                            record.get("account"),
                            file_name,
                            record.get("natural_key"),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                        self._audit(
                            connection,
                            transaction_id=cursor.lastrowid,
                            action="insert",
                            actor=actor,
                            new_value=f"{record.get('transaction_date')} "
                            f"{record.get('amount')} {record.get('details')}",
                        )
                    else:
                        skipped += 1

                connection.execute(
                    "INSERT OR REPLACE INTO uploaded_files "
                    "(file_name, row_count, uploaded_by) VALUES (?, ?, ?)",
                    (file_name, inserted, actor),
                )
                connection.commit()

            receipt.inserted = inserted
            receipt.duplicates_skipped = skipped
            receipt.committed = True
        except sqlite3.Error as exc:
            receipt.error = f"{type(exc).__name__}: {exc}"
        return receipt

    def update_transactions(
        self, changes: list[TransactionChange], actor: str
    ) -> UpdateResult:
        """
        FINDING F6. The original compared an int against a display string, so the
        change test was always true and every visible row was written back one
        HTTP request at a time. Only genuinely changed fields are written here,
        and the whole set goes in one database transaction.
        """
        result = UpdateResult()
        if not changes:
            return result

        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                for change in changes:
                    row = connection.execute(
                        "SELECT purpose, budget_category FROM transactions "
                        "WHERE transactionid = ?",
                        (change.transaction_id,),
                    ).fetchone()
                    if row is None:
                        result.failed.append(change.transaction_id)
                        continue

                    current = {"purpose": row["purpose"], "budget_category": row["budget_category"]}
                    desired = change.as_values()
                    dirty = {
                        field: value
                        for field, value in desired.items()
                        if _normalized(value) != _normalized(current[field])
                    }
                    if not dirty:
                        result.unchanged += 1
                        continue

                    assignments = ", ".join(f"{field} = ?" for field in dirty)
                    connection.execute(
                        f"UPDATE transactions SET {assignments} WHERE transactionid = ?",
                        (*dirty.values(), change.transaction_id),
                    )
                    for field, value in dirty.items():
                        self._audit(
                            connection,
                            transaction_id=change.transaction_id,
                            action="update",
                            actor=actor,
                            field=field,
                            old_value=current[field],
                            new_value=value,
                        )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_budgets(
        self, term_id: str, allocations: dict[int, float], actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                for committee_id, amount in allocations.items():
                    existing = connection.execute(
                        "SELECT budget_amount FROM committeebudgets "
                        "WHERE termid = ? AND committeeid = ?",
                        (term_id, committee_id),
                    ).fetchone()
                    previous = existing["budget_amount"] if existing else None
                    if previous is not None and float(previous) == float(amount):
                        result.unchanged += 1
                        continue

                    connection.execute(
                        "INSERT INTO committeebudgets (termid, committeeid, budget_amount) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT (termid, committeeid) "
                        "DO UPDATE SET budget_amount = excluded.budget_amount",
                        (term_id, committee_id, float(amount)),
                    )
                    self._audit(
                        connection,
                        transaction_id=None,
                        action="budget",
                        actor=actor,
                        field=f"{term_id}/committee {committee_id}",
                        old_value=previous,
                        new_value=amount,
                    )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def insert_term(
        self, term_id: str, semester: str, start_date: str, end_date: str, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT 1 FROM terms WHERE TermID = ?", (term_id,)
                ).fetchone()
                if existing:
                    result.error = f"Term {term_id} already exists."
                    return result
                connection.execute(
                    "INSERT INTO terms (TermID, Semester, start_date, end_date) "
                    "VALUES (?, ?, ?, ?)",
                    (term_id, semester, start_date, end_date),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="term",
                    actor=actor,
                    field=term_id,
                    new_value=f"{semester} {start_date}..{end_date}",
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_merchants(self, rules: list[dict], actor: str) -> UpdateResult:
        result = UpdateResult()
        if not rules:
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                for rule in rules:
                    connection.execute(
                        "INSERT INTO merchants "
                        "(merchant_key, canonical_name, committee_id, purpose, hit_count, source) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (merchant_key) DO UPDATE SET "
                        "  canonical_name = excluded.canonical_name, "
                        "  committee_id  = excluded.committee_id, "
                        "  purpose       = excluded.purpose, "
                        "  hit_count     = merchants.hit_count + 1, "
                        "  updated_at    = CURRENT_TIMESTAMP",
                        (
                            rule["merchant_key"],
                            rule.get("canonical_name"),
                            rule.get("committee_id"),
                            rule.get("purpose"),
                            rule.get("hit_count", 1),
                            rule.get("source", "learned"),
                        ),
                    )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def record_statement_balance(self, balance: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO statement_balances "
                    "(account, period_start, period_end, opening_balance, closing_balance, source_file) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (account, period_start, period_end) DO UPDATE SET "
                    "  opening_balance = excluded.opening_balance, "
                    "  closing_balance = excluded.closing_balance, "
                    "  source_file     = excluded.source_file",
                    (
                        balance["account"],
                        balance["period_start"],
                        balance["period_end"],
                        float(balance["opening_balance"]),
                        float(balance["closing_balance"]),
                        balance.get("source_file"),
                    ),
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- sandbox-only --------------------------------------------------------

    def reset(self) -> None:
        """Drop everything and rebuild. Sandbox convenience, no production analogue."""
        with self._connect() as connection:
            tables = [
                "transaction_audit", "statement_balances", "merchants",
                "uploaded_files", "transactions", "committeebudgets", "terms", "committees",
            ]
            for table in tables:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        self._ensure_schema()


def _normalized(value: object) -> object:
    """Treat None, '' and whitespace as the same absent value when diffing."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _columns_for(query: str) -> list[str]:
    """Column names for an empty result, so downstream code sees a stable shape."""
    known = {
        "committees": ["CommitteeID", "Committee_Name", "Committee_Type"],
        "terms": ["TermID", "Semester", "start_date", "end_date"],
        "committeebudgets": ["committeebudgetid", "termid", "committeeid", "budget_amount"],
        "transactions": [
            "transactionid", "transaction_date", "amount", "details",
            "budget_category", "purpose", "account", "source_file",
            "natural_key", "created_at",
        ],
        "uploaded_files": ["id", "file_name", "uploaded_at", "row_count", "uploaded_by"],
        "transaction_audit": [
            "audit_id", "transaction_id", "action", "field",
            "old_value", "new_value", "actor", "changed_at",
        ],
        "merchants": [
            "merchant_id", "merchant_key", "canonical_name",
            "committee_id", "purpose", "hit_count", "source", "updated_at",
        ],
        "statement_balances": [
            "id", "account", "period_start", "period_end",
            "opening_balance", "closing_balance", "source_file", "recorded_at",
        ],
    }
    lowered = query.lower()
    for table, columns in known.items():
        if f"from {table}" in lowered:
            return columns
    return []
