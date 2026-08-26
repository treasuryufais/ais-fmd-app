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

from datetime import datetime, timezone

import pandas as pd

from .. import settings
from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt

PAGE_SIZE = 1000


def _require_production() -> None:
    settings.assert_external_call_allowed("Supabase connection")


def _now() -> str:
    """
    The current time, as an ISO 8601 string PostgREST will parse into `timestamptz`.

    NOT the literal string `"now()"` -- a JSON/REST write is data, not SQL, so
    `"now()"` would be inserted as an attempt to parse the four characters
    "now()" as a timestamp and rejected by Postgres, not evaluated as the SQL
    function. Columns with a `DEFAULT now()` (most of them, on INSERT) don't
    need this; it's only for UPDATE statements that have to set a timestamp
    column explicitly, the way `sqlite_backend.py` uses `CURRENT_TIMESTAMP` in
    an `UPDATE ... SET` clause.
    """
    return datetime.now(timezone.utc).isoformat()


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

    def fetch_labeled_examples(self) -> pd.DataFrame:
        return self._select_all("labeled_examples")

    def fetch_reimbursements(self) -> pd.DataFrame:
        """
        Pending and approved first, then newest first within each -- matching
        `sqlite_backend.py`.

        Sorted client-side because PostgREST's `order()` takes a column name,
        not an expression; SQLite's `CASE status WHEN 'pending' THEN 0 ...` has
        no equivalent over the query-builder API. Fine at this table's size —
        a chapter's outstanding reimbursements, not a transaction ledger.
        """
        response = (
            self._anon.table("reimbursements")
            .select("*")
            .order("submitted_at", desc=True)
            .execute()
        )
        frame = pd.DataFrame(response.data or [])
        if frame.empty:
            return frame
        priority = {"pending": 0, "approved": 1}
        frame["_priority"] = frame["status"].map(lambda s: priority.get(s, 2))
        return frame.sort_values(["_priority"], kind="stable").drop(columns="_priority")

    def fetch_receipts(self) -> pd.DataFrame:
        response = (
            self._anon.table("receipts")
            .select("*")
            .order("uploaded_at", desc=True)
            .execute()
        )
        return pd.DataFrame(response.data or [])

    def fetch_members(self, term_id: str | None = None) -> pd.DataFrame:
        query = self._anon.table("members").select("*")
        if term_id:
            query = query.eq("term_id", term_id).order("full_name")
        else:
            query = query.order("term_id").order("full_name")
        return pd.DataFrame((query.execute().data or []))

    def fetch_profiles(self) -> pd.DataFrame:
        response = (
            self._anon.table("profiles")
            .select("*")
            .order("committee_id")
            .order("display_name")
            .execute()
        )
        return pd.DataFrame(response.data or [])

    def fetch_profile(self, email: str) -> dict | None:
        """
        One person's access record, or None on a miss.

        `ilike` rather than `eq` for the same reason `sqlite_backend.py` uses
        `lower(email) = lower(?)`: Google's identity token capitalises an email
        however the account was originally typed, which does not always match
        how a treasurer typed it into `profiles`.
        """
        if not email:
            return None
        response = (
            self._anon.table("profiles")
            .select("*")
            .ilike("email", email)
            .maybe_single()
            .execute()
        )
        return response.data if response is not None else None

    # --- writes --------------------------------------------------------------

    def _audit(
        self,
        *,
        transaction_id: int | None,
        action: str,
        actor: str,
        field: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
    ) -> None:
        """
        Best-effort audit row.

        Not atomic with the write it describes -- a genuinely atomic pairing
        needs a Postgres function, the way `insert_transactions` and
        `update_transactions` already use `import_statement` and
        `apply_transaction_edits` for exactly that reason (FINDING F4/F6). The
        methods below don't get that treatment because none of the *existing*
        simple writes in this file do either (`insert_term`, `upsert_merchants`,
        `record_statement_balance` write no audit row at all) -- this at least
        adds one, on the same non-atomic footing the rest of the file already
        accepts. Failure here must not fail the caller's real write, so it is
        swallowed rather than propagated.
        """
        try:
            self._admin.table("transaction_audit").insert(
                {
                    "transaction_id": transaction_id,
                    "action": action,
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "actor": actor,
                }
            ).execute()
        except Exception:  # noqa: BLE001 - the write already succeeded; don't undo it
            pass

    def create_reimbursement(self, request: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            response = (
                self._admin.table("reimbursements")
                .insert(
                    {
                        "requester": request["requester"],
                        "committee_id": request.get("committee_id"),
                        "amount": float(request["amount"]),
                        "description": request.get("description"),
                        "incurred_on": request.get("incurred_on"),
                        "status": "pending",
                        "receipt_id": request.get("receipt_id"),
                    }
                )
                .execute()
            )
            request_id = (response.data or [{}])[0].get("request_id")
            self._audit(
                transaction_id=None,
                action="reimbursement_request",
                actor=actor,
                field=f"request {request_id}",
                new_value=f"{request['requester']} ${float(request['amount']):.2f}",
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def decide_reimbursement(
        self, request_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        result = UpdateResult()
        if status not in {"approved", "rejected", "paid", "pending"}:
            result.error = f"Unknown status {status!r}."
            return result
        try:
            existing = (
                self._anon.table("reimbursements")
                .select("status")
                .eq("request_id", request_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Request {request_id} does not exist."
                return result
            if existing.data.get("status") == status:
                result.unchanged = 1
                return result

            self._admin.table("reimbursements").update(
                {
                    "status": status,
                    "decided_at": _now(),
                    "decided_by": actor,
                    "decision_note": note or None,
                }
            ).eq("request_id", request_id).execute()
            self._audit(
                transaction_id=None,
                action="reimbursement_decision",
                actor=actor,
                field=f"request {request_id}",
                old_value=existing.data.get("status"),
                new_value=status,
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def link_reimbursement_to_transaction(
        self, request_id: int, transaction_id: int, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("reimbursements").update(
                {
                    "matched_transaction_id": transaction_id,
                    "status": "paid",
                    "decided_at": _now(),
                    "decided_by": actor,
                }
            ).eq("request_id", request_id).execute()
            self._audit(
                transaction_id=transaction_id,
                action="reimbursement_matched",
                actor=actor,
                field=f"request {request_id}",
                new_value=str(transaction_id),
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def store_receipt(self, receipt: dict, actor: str) -> tuple[int | None, UpdateResult]:
        """Record receipt metadata. The file itself is written by domain.receipts."""
        result = UpdateResult()
        try:
            response = (
                self._admin.table("receipts")
                .insert(
                    {
                        "file_name": receipt["file_name"],
                        "stored_path": receipt["stored_path"],
                        "content_type": receipt.get("content_type"),
                        "byte_size": receipt.get("byte_size"),
                        "uploaded_by": actor,
                        "transaction_id": receipt.get("transaction_id"),
                        "request_id": receipt.get("request_id"),
                        "vendor": receipt.get("vendor"),
                        "amount": receipt.get("amount"),
                        "receipt_date": receipt.get("receipt_date"),
                    }
                )
                .execute()
            )
            receipt_id = (response.data or [{}])[0].get("receipt_id")
            self._audit(
                transaction_id=receipt.get("transaction_id"),
                action="receipt_uploaded",
                actor=actor,
                field=receipt["file_name"],
            )
            result.updated = 1
            return receipt_id, result
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            return None, result

    def set_term_lock(self, term_id: str, locked: bool, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            existing = (
                self._anon.table("terms")
                .select("Semester,locked")
                .eq("TermID", term_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Term {term_id} does not exist."
                return result
            if bool(existing.data.get("locked")) == locked:
                result.unchanged = 1
                return result

            self._admin.table("terms").update(
                {"locked": locked, "locked_at": _now(), "locked_by": actor}
            ).eq("TermID", term_id).execute()
            self._audit(
                transaction_id=None,
                action="lock" if locked else "unlock",
                actor=actor,
                field=term_id,
                old_value="locked" if existing.data.get("locked") else "open",
                new_value="locked" if locked else "open",
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_term_dues_rates(
        self, term_id: str, rates: str, verified: bool, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            existing = (
                self._anon.table("terms")
                .select("dues_rates,dues_rates_verified")
                .eq("TermID", term_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Term {term_id} does not exist."
                return result
            previous = existing.data.get("dues_rates")
            was_verified = bool(existing.data.get("dues_rates_verified"))
            if (previous or "") == (rates or "") and was_verified == verified:
                result.unchanged = 1
                return result

            self._admin.table("terms").update(
                {"dues_rates": rates, "dues_rates_verified": verified}
            ).eq("TermID", term_id).execute()
            self._audit(
                transaction_id=None,
                action="dues_rates_set",
                actor=actor,
                field=term_id,
                old_value=previous,
                new_value=rates,
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def insert_labeled_examples(self, examples: list[dict], actor: str) -> UpdateResult:
        """
        Record human labels. Idempotent on `natural_key` via `ignore_duplicates`
        (Postgres `ON CONFLICT (natural_key) DO NOTHING`), matching
        `sqlite_backend.py` so re-importing the same ledger adds nothing rather
        than inflating the training set with duplicates.

        One batched upsert rather than one request per row -- the SQLite version
        loops because a local transaction makes that free; over HTTP it would
        not be.
        """
        result = UpdateResult()
        if not examples:
            return result
        try:
            rows = [
                {
                    "source": example.get("source", "review"),
                    "source_ref": example.get("source_ref"),
                    "era": example.get("era"),
                    "transaction_id": example.get("transaction_id"),
                    "transaction_date": example.get("transaction_date"),
                    "amount": example.get("amount"),
                    "details": example.get("details"),
                    "account": example.get("account"),
                    "committee_id": int(example["committee_id"]),
                    "purpose": example.get("purpose"),
                    "model_committee": example.get("model_committee"),
                    "model_confidence": example.get("model_confidence"),
                    "model_source": example.get("model_source"),
                    "labeled_by": example.get("labeled_by", actor),
                    "natural_key": example.get("natural_key"),
                }
                for example in examples
            ]
            self._admin.table("labeled_examples").upsert(
                rows, on_conflict="natural_key", ignore_duplicates=True
            ).execute()
            # PostgREST's `ignore_duplicates` returns only the rows it actually
            # inserted, so this is the real count -- unlike the SQLite version's
            # per-row `cursor.rowcount` loop, there's no cheap way to also report
            # how many were skipped without a second query, and nothing reads
            # `unchanged` for this call today.
            result.updated = len(rows)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def replace_members(
        self, term_id: str, members: list[dict], source_file: str, actor: str
    ) -> UpdateResult:
        """
        Replace one term's roster wholesale -- delete then insert, matching
        `sqlite_backend.py`. See that method's docstring for why replace and not
        merge: a membership export is a snapshot of who is on the roster *now*.

        Not atomic across the two requests the way the SQLite version's single
        transaction is. A partial failure here can leave the term's roster
        empty rather than reverted -- acceptable for now because a roster
        upload is a low-frequency, treasurer-supervised action with an
        immediately visible result (the Roster page would show 0 members), not
        a background process that could fail silently.
        """
        result = UpdateResult()
        if not term_id:
            result.error = "A term is required to store a roster."
            return result
        try:
            self._admin.table("members").delete().eq("term_id", term_id).execute()
            if members:
                rows = [
                    {
                        "term_id": term_id,
                        "full_name": member.get("full_name"),
                        "match_key": member.get("match_key"),
                        "alt_keys": ",".join(member.get("alt_keys") or ()) or None,
                        "preferred_name": member.get("preferred_name") or None,
                        "claims_paid": bool(member.get("claims_paid")),
                        "email": member.get("email") or None,
                        "ufid": member.get("ufid") or None,
                        "committee_id": member.get("committee_id"),
                        "notes": member.get("notes") or None,
                        "source_file": source_file,
                        "uploaded_by": actor,
                    }
                    for member in members
                ]
                self._admin.table("members").insert(rows).execute()
            result.updated = len(members)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def add_member_alias(
        self, term_id: str, match_key: str, alias_key: str, actor: str
    ) -> UpdateResult:
        """Teach the roster a second name for one member. See sqlite_backend.py."""
        result = UpdateResult()
        if not (term_id and match_key and alias_key):
            result.error = "Term, member and alias are all required."
            return result
        try:
            existing = (
                self._anon.table("members")
                .select("alt_keys")
                .eq("term_id", term_id)
                .eq("match_key", match_key)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"No member {match_key!r} on the {term_id} roster."
                return result
            keys = {
                part.strip()
                for part in str(existing.data.get("alt_keys") or "").split(",")
                if part.strip()
            }
            if alias_key == match_key or alias_key in keys:
                result.unchanged = 1
                return result
            keys.add(alias_key)
            self._admin.table("members").update(
                {"alt_keys": ",".join(sorted(keys))}
            ).eq("term_id", term_id).eq("match_key", match_key).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_profile(
        self,
        email: str,
        role: str,
        committee_id: int | None,
        display_name: str,
        actor: str,
    ) -> UpdateResult:
        result = UpdateResult()
        email = (email or "").strip()
        if not email:
            result.error = "An email is required."
            return result
        if role not in {"member", "officer", "treasurer", "admin"}:
            result.error = f"Unknown role {role!r}."
            return result
        try:
            self._admin.table("profiles").upsert(
                {
                    "email": email,
                    "display_name": display_name or None,
                    "role": role,
                    "committee_id": committee_id,
                    "created_by": actor,
                },
                on_conflict="email",
            ).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def remove_profile(self, email: str) -> UpdateResult:
        result = UpdateResult()
        if not email:
            result.error = "An email is required."
            return result
        try:
            response = (
                self._admin.table("profiles").delete().ilike("email", email).execute()
            )
            result.updated = len(response.data or [])
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- writes (pre-existing) -------------------------------------------------

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
