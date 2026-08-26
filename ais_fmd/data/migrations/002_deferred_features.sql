-- ===========================================================================
-- Migration 002 -- DEFERRED. Do not run this yet.
--
-- Everything here is cut from the MVP but written down so the work is not lost
-- and turning it on later is a decision, not a rebuild:
--
--     * reimbursements and receipts  -- the Reimbursements page is unloaded
--                                       from app.py navigation for now
--     * profiles + row-level security -- there is one operator behind one
--                                       password; per-user roles need a real
--                                       login before RLS means anything
--
-- WHY RLS IS DANGEROUS TO ENABLE EARLY. Every policy below is evaluated against
-- auth.uid(). The MVP has no Supabase Auth session, so auth.uid() is NULL for
-- every request, current_role_rank() returns 0, and ENABLE ROW LEVEL SECURITY
-- would deny reads to the app entirely -- via the anon key. The app would appear
-- to lose all its data. Run this only once a real login exists AND has been
-- tested with the anon/user client (the service key bypasses RLS and will
-- cheerfully tell you everything works when it does not).
-- ===========================================================================

BEGIN;

-- --- Reimbursements ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.receipts (
    receipt_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_name      text NOT NULL,
    stored_path    text NOT NULL,
    content_type   text,
    byte_size      integer,
    uploaded_by    text,
    uploaded_at    timestamptz DEFAULT now(),
    transaction_id integer REFERENCES public.transactions (transactionid),
    request_id     integer,
    vendor         text,
    amount         numeric,
    receipt_date   date
);

CREATE TABLE IF NOT EXISTS public.reimbursements (
    request_id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requester              text NOT NULL,
    committee_id           integer REFERENCES public.committees ("CommitteeID"),
    amount                 numeric NOT NULL,
    description            text,
    incurred_on            date,
    status                 text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'paid')),
    submitted_at           timestamptz DEFAULT now(),
    decided_at             timestamptz,
    decided_by             text,
    decision_note          text,
    matched_transaction_id integer REFERENCES public.transactions (transactionid),
    receipt_id             integer REFERENCES public.receipts (receipt_id)
);

CREATE INDEX IF NOT EXISTS ix_reimbursements_status
    ON public.reimbursements (status);

-- receipts.request_id is declared above without its FK because reimbursements
-- does not exist yet at that point. Added here, once both tables are present.
ALTER TABLE public.receipts
    DROP CONSTRAINT IF EXISTS receipts_request_id_fkey;
ALTER TABLE public.receipts
    ADD CONSTRAINT receipts_request_id_fkey
    FOREIGN KEY (request_id) REFERENCES public.reimbursements (request_id);

-- --- Roles ------------------------------------------------------------------
--
-- REVISED 2026-08-26 -- keyed by email, not by `auth.users`.
--
-- This table was originally designed for Supabase Auth: `user_id uuid
-- REFERENCES auth.users`, populated when someone signs in through Supabase's
-- own auth service, with `current_role_rank()` reading `auth.uid()` out of the
-- Postgres session GUCs that Supabase's PostgREST layer sets per request.
--
-- The VP portal instead uses Streamlit's own native OIDC login (`st.login`,
-- Streamlit >=1.42, via Authlib) against Google directly. The app's backend
-- still connects to Supabase with its own service/anon key -- individual VPs
-- never open a Postgres session of their own -- so `auth.uid()` would be NULL
-- for every request regardless of who is using the app. Keying on it would
-- make every policy below unconditionally deny, exactly the "app loses all its
-- data" failure the original warning in this file describes, just for a
-- different reason than an unconfigured login.
--
-- Keyed on email instead: it is the one identifier Google's identity token and
-- a human-typed roster row can both agree on, and it needs no `auth.users` row
-- to exist first. `ais_fmd.auth.login_gate` looks a signed-in Google identity
-- up here directly.
CREATE TABLE IF NOT EXISTS public.profiles (
    email        text PRIMARY KEY,
    display_name text,
    role         text NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'officer', 'treasurer', 'admin')),
    committee_id integer REFERENCES public.committees ("CommitteeID"),
    created_at   timestamptz DEFAULT now(),
    created_by   text
);

COMMIT;

-- `current_role_rank()` / `auth.uid()`-based RLS, further below, does not
-- apply to this architecture -- see the note on `profiles` above. Authorization
-- stays application-level, the same way it already works for the SQLite
-- backend and for every `auth.require(...)` call in `ais_fmd/views/*.py`. The
-- RLS block is left in place, commented out, only in case a later decision
-- adds real Supabase Auth sessions on top of this for defense-in-depth; as
-- written today it would need `current_role_rank()` rewritten to key on email
-- and a way to get the caller's verified email into the Postgres session
-- (e.g. `SET LOCAL` from the app on each request), neither of which exists.

-- ===========================================================================
-- RLS -- SEPARATE STEP, EVEN MORE DEFERRED
--
-- Left outside the transaction above and commented out deliberately. Enabling
-- these without a working login locks the application out of its own database.
-- Read the warning at the top of this file before uncommenting.
-- ===========================================================================

-- ALTER TABLE public.transactions      ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.committeebudgets  ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.transaction_audit ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.labeled_examples  ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.members           ENABLE ROW LEVEL SECURITY;
--
-- DROP POLICY IF EXISTS transactions_read ON public.transactions;
-- CREATE POLICY transactions_read ON public.transactions
--     FOR SELECT USING (public.current_role_rank() >= 10);
--
-- DROP POLICY IF EXISTS transactions_write ON public.transactions;
-- CREATE POLICY transactions_write ON public.transactions
--     FOR ALL USING (public.current_role_rank() >= 30)
--              WITH CHECK (public.current_role_rank() >= 30);
--
-- DROP POLICY IF EXISTS budgets_read ON public.committeebudgets;
-- CREATE POLICY budgets_read ON public.committeebudgets
--     FOR SELECT USING (public.current_role_rank() >= 10);
--
-- DROP POLICY IF EXISTS budgets_write ON public.committeebudgets;
-- CREATE POLICY budgets_write ON public.committeebudgets
--     FOR ALL USING (public.current_role_rank() >= 30)
--              WITH CHECK (public.current_role_rank() >= 30);
--
-- DROP POLICY IF EXISTS audit_read ON public.transaction_audit;
-- CREATE POLICY audit_read ON public.transaction_audit
--     FOR SELECT USING (public.current_role_rank() >= 30);
--
-- -- members holds personal data: treasurer and above only, no member read.
-- DROP POLICY IF EXISTS members_rw ON public.members;
-- CREATE POLICY members_rw ON public.members
--     FOR ALL USING (public.current_role_rank() >= 30)
--              WITH CHECK (public.current_role_rank() >= 30);
