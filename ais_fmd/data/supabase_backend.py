"""
Production backend.

Not exercised by the sandbox -- the `supabase` package is deliberately absent
from the sandbox venv, and importing this module in sandbox mode raises. It is
included so the fixes have a production implementation to migrate to, and so the
corrected patterns are written down somewhere concrete.

FINDING F1. The original cached one Supabase client with `@st.cache_resource`,
which caches per *process*, not per session. Every visitor shared one client
object, `sign_in_with_password` wrote the auth session onto it, and `sign_out`
revoked it for everybody. Any RLS policy keyed on auth.uid() was evaluated
against whoever logged in most recently.

Here the anonymous client is still shared -- that is safe, it holds no identity
-- but any user-scoped call goes through a client carrying that session's own
access token.
"""

from __future__ import annotations

import pandas as pd

from .. import settings
from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt

PAGE_SIZE = 1000


def _require_production() -> None:
    settings.assert_external_call_allowed("Supabase connection")


class SupabaseBackend(Backend):
    name = "supabase"

    def __init__(self, access_token: str | None = None) -> None:
        _require_production()
        try:
            from supabase import create_client
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "The 'supabase' package is not installed. The sandbox venv omits "
                "it on purpose; install it only in a production environment."
            ) from exc

        import os

        url = os.environ["SUPABASE_URL"]
        self._create_client = create_client
        self._url = url
        self._anon_key = os.environ["SUPABASE_ANON_KEY"]
        self._service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self._access_token = access_token

    # --- clients -------------------------------------------------------------

    @property
    def _anon(self):
        """Identity-free client. Safe to share; holds no session."""
        return self._create_client(self._url, self._anon_key)

    @property
    def _user(self):
        """
        Client carrying *this session's* token.

        This is the F1 fix: identity travels with the request rather than being
        written onto a process-global object.
        """
        client = self._create_client(self._url, self._anon_key)
        if self._access_token:
            client.postgrest.auth(self._access_token)
        return client

    @property
    def _admin(self):
        if not self._service_key:
            raise RuntimeError("SUPABASE_SERVICE_KEY is not configured.")
        return self._create_client(self._url, self._service_key)

    # --- reads ---------------------------------------------------------------

    def _select_all(self, table: str) -> pd.DataFrame:
        """
        Paginated read.

        The original loop issued one extra request whenever the row count was an
        exact multiple of the page size; stopping on a short page avoids that.
        """
        client = self._anon
        rows: list[dict] = []
        offset = 0
        while True:
            response = (
                client.table(table)
                .select("*")
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
            )
            batch = response.data or []
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return pd.DataFrame(rows)

    def fetch_committees(self) -> pd.DataFrame:
        return self._select_all("committees")

    def fetch_terms(self) -> pd.DataFrame:
        return self._select_all("terms")

    def fetch_budgets(self) -> pd.DataFrame:
        return self._select_all("committeebudgets")

    def fetch_transactions(self) -> pd.DataFrame:
        frame = self._select_all("transactions")
        if not frame.empty:
            frame["transaction_date"] = pd.to_datetime(frame["transaction_date"], errors="coerce")
            frame["budget_category"] = frame["budget_category"].astype("Int64")
        return frame

    def fetch_uploaded_files(self) -> pd.DataFrame:
        return self._select_all("uploaded_files")

    def fetch_merchants(self) -> pd.DataFrame:
        return self._select_all("merchants")

    def fetch_audit(self, limit: int = 500) -> pd.DataFrame:
        response = (
            self._anon.table("transaction_audit")
            .select("*")
            .order("audit_id", desc=True)
            .limit(limit)
            .execute()
        )
        return pd.DataFrame(response.data or [])

    def fetch_statement_balances(self) -> pd.DataFrame:
        return self._select_all("statement_balances")

    # --- writes --------------------------------------------------------------

    def insert_transactions(
        self, records: list[dict], file_name: str, actor: str
    ) -> UploadReceipt:
        """
        FINDING F4. Both writes happen inside one Postgres function, so the
        statement marker and the rows commit together or not at all. The RPC is
        defined in schema_postgres.sql as `import_statement`.
        """
        receipt = UploadReceipt(file_name=file_name)
        try:
            response = self._admin.rpc(
                "import_statement",
                {
                    "p_records": records,
                    "p_file_name": file_name,
                    "p_actor": actor,
                },
            ).execute()
            payload = (response.data or [{}])[0]
            receipt.inserted = int(payload.get("inserted", 0))
            receipt.duplicates_skipped = int(payload.get("skipped", 0))
            receipt.committed = True
        except Exception as exc:  # noqa: BLE001
            receipt.error = f"{type(exc).__name__}: {exc}"
        return receipt

    def update_transactions(
        self, changes: list[TransactionChange], actor: str
    ) -> UpdateResult:
        """FINDING F6. One batched RPC rather than one HTTP request per row."""
        result = UpdateResult()
        if not changes:
            return result
        try:
            payload = [
                {
                    "transactionid": change.transaction_id,
                    "purpose": change.purpose,
                    "budget_category": change.budget_category,
                }
                for change in changes
            ]
            response = self._admin.rpc(
                "apply_transaction_edits", {"p_changes": payload, "p_actor": actor}
            ).execute()
            data = (response.data or [{}])[0]
            result.updated = int(data.get("updated", 0))
            result.unchanged = int(data.get("unchanged", 0))
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_budgets(
        self, term_id: str, allocations: dict[int, float], actor: str
    ) -> UpdateResult:
        """
        The original deleted every budget for the term and re-inserted, which
        destroys history and leaves a window where the term has no budget at all.
        An upsert keyed on (termid, committeeid) has neither problem.
        """
        result = UpdateResult()
        try:
            rows = [
                {"termid": term_id, "committeeid": committee_id, "budget_amount": float(amount)}
                for committee_id, amount in allocations.items()
            ]
            self._admin.table("committeebudgets").upsert(
                rows, on_conflict="termid,committeeid"
            ).execute()
            result.updated = len(rows)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def insert_term(
        self, term_id: str, semester: str, start_date: str, end_date: str, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("terms").insert(
                {
                    "TermID": term_id,
                    "Semester": semester,
                    "start_date": start_date,
                    "end_date": end_date,
                }
            ).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_merchants(self, rules: list[dict], actor: str) -> UpdateResult:
        result = UpdateResult()
        if not rules:
            return result
        try:
            self._admin.table("merchants").upsert(
                rules, on_conflict="merchant_key"
            ).execute()
            result.updated = len(rules)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def record_statement_balance(self, balance: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("statement_balances").upsert(
                balance, on_conflict="account,period_start,period_end"
            ).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result
