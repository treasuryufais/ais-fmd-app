"""
The one caching layer.

FINDING (caching). The original had two. `utils.py` loaders were correctly
decorated with `@st.cache_data(ttl=300)`, and then all four pages wrapped them
in a second, un-TTL'd `@st.cache_data`. The outer cache never expired, so the
inner TTL never fired -- data refreshed only when something called the global
`st.cache_data.clear()`, which wipes every cached function for every user at
once. Four separate wrappers also meant four copies of the transaction table
resident at the same time.

Here there is exactly one cached read per table, keyed on a process-global
version counter. A write bumps the counter, which invalidates precisely the data
that changed for everyone, without nuking unrelated caches.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import streamlit as st

from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt, get_backend


@st.cache_resource(show_spinner=False)
def _version_holder() -> dict:
    """Process-global version counter. Shared across sessions, unlike session_state."""
    return {"value": 0}


def data_version() -> int:
    return _version_holder()["value"]


def invalidate() -> None:
    """Called after every write. Granular by design -- no global cache clear."""
    _version_holder()["value"] += 1


@st.cache_resource(show_spinner=False)
def backend() -> Backend:
    """
    The backend object holds no user identity, so caching it per process is safe.

    This is the distinction the original missed: caching a *connection factory*
    is fine, caching an object that carries an authenticated session is not
    (FINDING F1).
    """
    return get_backend()


# --- Cached reads ------------------------------------------------------------
# Each takes the version counter so a bump invalidates it.

@st.cache_data(ttl=300, show_spinner=False)
def _committees(version: int) -> pd.DataFrame:
    return backend().fetch_committees()


@st.cache_data(ttl=300, show_spinner=False)
def _terms(version: int) -> pd.DataFrame:
    return backend().fetch_terms()


@st.cache_data(ttl=300, show_spinner=False)
def _budgets(version: int) -> pd.DataFrame:
    return backend().fetch_budgets()


@st.cache_data(ttl=300, show_spinner=False)
def _transactions(version: int) -> pd.DataFrame:
    return backend().fetch_transactions()


@st.cache_data(ttl=300, show_spinner=False)
def _uploaded_files(version: int) -> pd.DataFrame:
    return backend().fetch_uploaded_files()


@st.cache_data(ttl=300, show_spinner=False)
def _merchants(version: int) -> pd.DataFrame:
    return backend().fetch_merchants()


@st.cache_data(ttl=60, show_spinner=False)
def _audit(version: int, limit: int) -> pd.DataFrame:
    return backend().fetch_audit(limit)


@st.cache_data(ttl=300, show_spinner=False)
def _statement_balances(version: int) -> pd.DataFrame:
    return backend().fetch_statement_balances()


# --- Public API --------------------------------------------------------------

def load_committees() -> pd.DataFrame:
    return _committees(data_version())


def load_terms() -> pd.DataFrame:
    return _terms(data_version())


def load_budgets() -> pd.DataFrame:
    return _budgets(data_version())


def load_transactions() -> pd.DataFrame:
    return _transactions(data_version())


def load_uploaded_files() -> pd.DataFrame:
    return _uploaded_files(data_version())


def load_merchants() -> pd.DataFrame:
    return _merchants(data_version())


def load_audit(limit: int = 500) -> pd.DataFrame:
    return _audit(data_version(), limit)


def load_statement_balances() -> pd.DataFrame:
    return _statement_balances(data_version())


def load_merchant_memory():
    from ..domain.categorize.merchants import MerchantMemory

    frame = load_merchants()
    records = frame.to_dict("records") if not frame.empty else []
    return MerchantMemory.from_records(records)


@dataclass
class DataBundle:
    """Everything a page needs, loaded once."""

    committees: pd.DataFrame
    terms: pd.DataFrame
    budgets: pd.DataFrame
    transactions: pd.DataFrame


def load_bundle() -> DataBundle:
    return DataBundle(
        committees=load_committees(),
        terms=load_terms(),
        budgets=load_budgets(),
        transactions=load_transactions(),
    )


# --- Writes (each invalidates) -----------------------------------------------

def insert_transactions(records: list[dict], file_name: str, actor: str) -> UploadReceipt:
    receipt = backend().insert_transactions(records, file_name, actor)
    if receipt.ok:
        invalidate()
    return receipt


def update_transactions(changes: list[TransactionChange], actor: str) -> UpdateResult:
    result = backend().update_transactions(changes, actor)
    if result.updated:
        invalidate()
    return result


def upsert_budgets(term_id: str, allocations: dict[int, float], actor: str) -> UpdateResult:
    result = backend().upsert_budgets(term_id, allocations, actor)
    if result.updated:
        invalidate()
    return result


def insert_term(term_id: str, semester: str, start_date: str, end_date: str, actor: str) -> UpdateResult:
    result = backend().insert_term(term_id, semester, start_date, end_date, actor)
    if result.updated:
        invalidate()
    return result


def upsert_merchants(rules: list[dict], actor: str) -> UpdateResult:
    result = backend().upsert_merchants(rules, actor)
    if result.updated:
        invalidate()
    return result


def record_statement_balance(balance: dict, actor: str) -> UpdateResult:
    result = backend().record_statement_balance(balance, actor)
    if result.updated:
        invalidate()
    return result


# --- Reimbursements, receipts, locking ---------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def _reimbursements(version: int) -> pd.DataFrame:
    return backend().fetch_reimbursements()


@st.cache_data(ttl=60, show_spinner=False)
def _receipts(version: int) -> pd.DataFrame:
    return backend().fetch_receipts()


def load_reimbursements() -> pd.DataFrame:
    return _reimbursements(data_version())


def load_receipts() -> pd.DataFrame:
    return _receipts(data_version())


def create_reimbursement(request: dict, actor: str) -> UpdateResult:
    result = backend().create_reimbursement(request, actor)
    if result.updated:
        invalidate()
    return result


def decide_reimbursement(request_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
    result = backend().decide_reimbursement(request_id, status, actor, note)
    if result.updated:
        invalidate()
    return result


def link_reimbursement(request_id: int, transaction_id: int, actor: str) -> UpdateResult:
    result = backend().link_reimbursement_to_transaction(request_id, transaction_id, actor)
    if result.updated:
        invalidate()
    return result


def store_receipt(receipt: dict, actor: str) -> tuple[int | None, UpdateResult]:
    receipt_id, result = backend().store_receipt(receipt, actor)
    if result.updated:
        invalidate()
    return receipt_id, result


def set_term_lock(term_id: str, locked: bool, actor: str) -> UpdateResult:
    result = backend().set_term_lock(term_id, locked, actor)
    if result.updated:
        invalidate()
    return result


@st.cache_data(ttl=60, show_spinner=False)
def _labeled_examples(version: int) -> pd.DataFrame:
    return backend().fetch_labeled_examples()


def load_labeled_examples() -> pd.DataFrame:
    """M19 training labels: ledger imports plus every review-queue decision."""
    return _labeled_examples(data_version())


def record_labels(examples: list[dict], actor: str) -> UpdateResult:
    result = backend().insert_labeled_examples(examples, actor)
    if result.updated:
        invalidate()
    return result


def locked_semesters() -> set[str]:
    """Names of terms currently closed to edits."""
    terms = load_terms()
    if terms.empty or "locked" not in terms.columns:
        return set()
    return set(terms[terms["locked"].fillna(0).astype(int) == 1]["Semester"].dropna())


def is_file_uploaded(file_name: str) -> bool:
    frame = load_uploaded_files()
    if frame.empty or "file_name" not in frame.columns:
        return False
    return bool((frame["file_name"] == file_name).any())
