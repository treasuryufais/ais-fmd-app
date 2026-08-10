"""
Backend abstraction.

Two implementations share this interface:

  * `SqliteBackend`  -- the sandbox. A local file, seeded with fake data.
  * `SupabaseBackend` -- production. Refuses to load in sandbox mode, and its
    dependency is not installed in the sandbox venv.

Every method that mutates data takes an `actor`, because the audit trail (M2)
is not optional. There is no write path that skips it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from .. import settings


@dataclass
class UploadReceipt:
    """Result of an atomic statement upload."""

    inserted: int = 0
    duplicates_skipped: int = 0
    file_name: str = ""
    committed: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.committed and self.error is None


@dataclass
class TransactionChange:
    """One pending edit to one transaction."""

    transaction_id: int
    purpose: str | None = None
    budget_category: int | None = None

    def as_values(self) -> dict:
        return {"purpose": self.purpose, "budget_category": self.budget_category}


@dataclass
class UpdateResult:
    updated: int = 0
    unchanged: int = 0
    failed: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed


class Backend(ABC):
    """The complete data interface used by the application."""

    name: str = "abstract"
    read_only: bool = False

    # --- reads ---------------------------------------------------------------

    @abstractmethod
    def fetch_committees(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_terms(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_budgets(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_transactions(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_uploaded_files(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_merchants(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_audit(self, limit: int = 500) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_statement_balances(self) -> pd.DataFrame: ...

    # --- writes --------------------------------------------------------------

    @abstractmethod
    def insert_transactions(
        self, records: list[dict], file_name: str, actor: str
    ) -> UploadReceipt: ...

    @abstractmethod
    def update_transactions(
        self, changes: list[TransactionChange], actor: str
    ) -> UpdateResult: ...

    @abstractmethod
    def upsert_budgets(
        self, term_id: str, allocations: dict[int, float], actor: str
    ) -> UpdateResult: ...

    @abstractmethod
    def insert_term(
        self, term_id: str, semester: str, start_date: str, end_date: str, actor: str
    ) -> UpdateResult: ...

    @abstractmethod
    def upsert_merchants(self, rules: list[dict], actor: str) -> UpdateResult: ...

    @abstractmethod
    def record_statement_balance(self, balance: dict, actor: str) -> UpdateResult: ...


def get_backend() -> Backend:
    """
    Build the backend for the active environment.

    Sandbox is the default and requires no configuration. Reaching the Supabase
    backend requires AIS_FMD_ENV=production *and* the supabase package, which
    the sandbox venv does not install.
    """
    if settings.is_sandbox():
        from .sqlite_backend import SqliteBackend

        return SqliteBackend()

    from .supabase_backend import SupabaseBackend

    return SupabaseBackend()
