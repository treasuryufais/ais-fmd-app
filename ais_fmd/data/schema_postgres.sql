-- ===========================================================================
-- UF AIS Financial Management System -- production schema
--
-- The base tables are the DDL that was buried at the bottom of
-- .github/copilot-instructions.md, which was the single most valuable artifact
-- in the original repository and the only accurate part of that file. It is
-- promoted here so it is a real migration rather than a comment.
--
-- Everything below the base tables is new, and each addition names the finding
-- it addresses.
--
-- NOT executed by the sandbox. The sandbox uses SQLite; this is the target for
-- a production migration.
-- ===========================================================================

-- --- Base tables -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.committees (
    "CommitteeID"    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "Committee_Name" text NOT NULL UNIQUE,
    "Committee_Type" text NOT NULL DEFAULT 'committee'
);

CREATE TABLE IF NOT EXISTS public.terms (
    "TermID"   text PRIMARY KEY,
    "Semester" text,
    start_date date,
    end_date   date
);

CREATE TABLE IF NOT EXISTS public.committeebudgets (
    committeebudgetid integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    termid            text NOT NULL REFERENCES public.terms ("TermID"),
    committeeid       integer NOT NULL REFERENCES public.committees ("CommitteeID"),
    budget_amount     numeric,
    -- Lets budgets be upserted instead of deleted-then-reinserted. The original
    -- deleted every row for the term first, which destroyed history and left a
    -- window where the term had no budget at all.
    CONSTRAINT committeebudgets_term_committee_key UNIQUE (termid, committeeid)
);

CREATE TABLE IF NOT EXISTS public.transactions (
    transactionid    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_date date NOT NULL,
    amount           numeric NOT NULL,
    details          text,
    budget_category  integer REFERENCES public.committees ("CommitteeID"),
    purpose          text,
    account          text,
    source_file      text,
    natural_key      text,
    created_at       timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.uploaded_files (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_name   text NOT NULL UNIQUE,
    uploaded_at timestamptz DEFAULT now(),
    row_count   integer DEFAULT 0,
    uploaded_by text
);

-- --- FINDING F3: deduplication enforced by the database ---------------------
--
-- The original deduplicated in pandas on (details, date) only, so two genuine
-- same-day purchases collapsed into one and the second was silently dropped.
-- The key now includes amount and account plus an occurrence ordinal, and it is
-- enforced here rather than by a filter that only runs when someone calls it.

CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_natural_key
    ON public.transactions (natural_key)
    WHERE natural_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_transactions_date ON public.transactions (transaction_date);
CREATE INDEX IF NOT EXISTS ix_transactions_committee ON public.transactions (budget_category);

-- --- Module M2: audit trail -------------------------------------------------

CREATE TABLE IF NOT EXISTS public.transaction_audit (
    audit_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id integer,
    action         text NOT NULL,
    field          text,
    old_value      text,
    new_value      text,
    actor          text,
    changed_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_txn ON public.transaction_audit (transaction_id);
CREATE INDEX IF NOT EXISTS ix_audit_changed_at ON public.transaction_audit (changed_at DESC);

-- --- Module M4: merchant memory ---------------------------------------------

CREATE TABLE IF NOT EXISTS public.merchants (
    merchant_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_key   text NOT NULL UNIQUE,
    canonical_name text,
    committee_id   integer REFERENCES public.committees ("CommitteeID"),
    purpose        text,
    hit_count      integer DEFAULT 0,
    source         text DEFAULT 'learned',
    updated_at     timestamptz DEFAULT now()
);

-- --- Module M1: reconciliation ----------------------------------------------

CREATE TABLE IF NOT EXISTS public.statement_balances (
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account         text NOT NULL,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    opening_balance numeric NOT NULL,
    closing_balance numeric NOT NULL,
    source_file     text,
    recorded_at     timestamptz DEFAULT now(),
    CONSTRAINT statement_balances_period_key UNIQUE (account, period_start, period_end)
);

-- --- FINDING F2: roles ------------------------------------------------------
--
-- The original had open registration and one shared treasury password. Roles
-- live here so authorisation is a property of the user rather than a secret
-- passed around a committee and never rotated.

CREATE TABLE IF NOT EXISTS public.profiles (
    user_id    uuid PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
    email      text,
    role       text NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'officer', 'treasurer', 'admin')),
    committee_id integer REFERENCES public.committees ("CommitteeID"),
    created_at timestamptz DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.current_role_rank()
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT COALESCE(
        (SELECT CASE role
                    WHEN 'admin'     THEN 40
                    WHEN 'treasurer' THEN 30
                    WHEN 'officer'   THEN 20
                    ELSE 10
                END
         FROM public.profiles WHERE user_id = auth.uid()),
        0
    );
$$;

ALTER TABLE public.transactions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.committeebudgets   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transaction_audit  ENABLE ROW LEVEL SECURITY;

-- Any signed-in member may read; only treasurers and above may write. These
-- only mean anything once the app stops sharing one process-global client
-- (FINDING F1) -- until then every policy evaluates against whoever logged in
-- most recently.
DROP POLICY IF EXISTS transactions_read ON public.transactions;
CREATE POLICY transactions_read ON public.transactions
    FOR SELECT USING (public.current_role_rank() >= 10);

DROP POLICY IF EXISTS transactions_write ON public.transactions;
CREATE POLICY transactions_write ON public.transactions
    FOR ALL USING (public.current_role_rank() >= 30)
             WITH CHECK (public.current_role_rank() >= 30);

DROP POLICY IF EXISTS budgets_read ON public.committeebudgets;
CREATE POLICY budgets_read ON public.committeebudgets
    FOR SELECT USING (public.current_role_rank() >= 10);

DROP POLICY IF EXISTS budgets_write ON public.committeebudgets;
CREATE POLICY budgets_write ON public.committeebudgets
    FOR ALL USING (public.current_role_rank() >= 30)
             WITH CHECK (public.current_role_rank() >= 30);

DROP POLICY IF EXISTS audit_read ON public.transaction_audit;
CREATE POLICY audit_read ON public.transaction_audit
    FOR SELECT USING (public.current_role_rank() >= 30);

-- --- FINDING F4: atomic statement import ------------------------------------
--
-- The original inserted transactions and then, as a separate call, marked the
-- file as uploaded. If the first succeeded and the second failed, the statement
-- stayed importable -- and because deduplication was unreliable (F3), a second
-- import duplicated everything.

CREATE OR REPLACE FUNCTION public.import_statement(
    p_records   jsonb,
    p_file_name text,
    p_actor     text
)
RETURNS TABLE (inserted integer, skipped integer)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    v_inserted integer := 0;
    v_total    integer := 0;
    v_id       integer;
    v_row      jsonb;
BEGIN
    FOR v_row IN SELECT * FROM jsonb_array_elements(p_records)
    LOOP
        v_total := v_total + 1;

        INSERT INTO public.transactions
            (transaction_date, amount, details, budget_category,
             purpose, account, source_file, natural_key)
        VALUES (
            (v_row ->> 'transaction_date')::date,
            (v_row ->> 'amount')::numeric,
             v_row ->> 'details',
            NULLIF(v_row ->> 'budget_category', '')::integer,
            NULLIF(v_row ->> 'purpose', ''),
             v_row ->> 'account',
            p_file_name,
             v_row ->> 'natural_key'
        )
        ON CONFLICT (natural_key) WHERE natural_key IS NOT NULL DO NOTHING
        RETURNING transactionid INTO v_id;

        IF v_id IS NOT NULL THEN
            v_inserted := v_inserted + 1;
            INSERT INTO public.transaction_audit
                (transaction_id, action, new_value, actor)
            VALUES (v_id, 'insert', v_row ->> 'details', p_actor);
        END IF;
    END LOOP;

    INSERT INTO public.uploaded_files (file_name, row_count, uploaded_by)
    VALUES (p_file_name, v_inserted, p_actor)
    ON CONFLICT (file_name) DO UPDATE
        SET row_count = EXCLUDED.row_count,
            uploaded_at = now();

    RETURN QUERY SELECT v_inserted, v_total - v_inserted;
END;
$$;

-- --- FINDING F6: batched edits ----------------------------------------------
--
-- The original issued one HTTP request per visible row on every save, because
-- its change comparison was always true. This applies the whole set in one
-- statement and writes the audit rows alongside.

CREATE OR REPLACE FUNCTION public.apply_transaction_edits(
    p_changes jsonb,
    p_actor   text
)
RETURNS TABLE (updated integer, unchanged integer)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    v_updated   integer := 0;
    v_unchanged integer := 0;
    v_row       jsonb;
    v_id        integer;
    v_purpose   text;
    v_committee integer;
    v_old       record;
BEGIN
    FOR v_row IN SELECT * FROM jsonb_array_elements(p_changes)
    LOOP
        v_id        := (v_row ->> 'transactionid')::integer;
        v_purpose   := NULLIF(v_row ->> 'purpose', '');
        v_committee := NULLIF(v_row ->> 'budget_category', '')::integer;

        SELECT purpose, budget_category INTO v_old
        FROM public.transactions WHERE transactionid = v_id;

        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        IF v_old.purpose IS NOT DISTINCT FROM v_purpose
           AND v_old.budget_category IS NOT DISTINCT FROM v_committee THEN
            v_unchanged := v_unchanged + 1;
            CONTINUE;
        END IF;

        UPDATE public.transactions
           SET purpose = v_purpose, budget_category = v_committee
         WHERE transactionid = v_id;

        IF v_old.purpose IS DISTINCT FROM v_purpose THEN
            INSERT INTO public.transaction_audit
                (transaction_id, action, field, old_value, new_value, actor)
            VALUES (v_id, 'update', 'purpose', v_old.purpose, v_purpose, p_actor);
        END IF;

        IF v_old.budget_category IS DISTINCT FROM v_committee THEN
            INSERT INTO public.transaction_audit
                (transaction_id, action, field, old_value, new_value, actor)
            VALUES (v_id, 'update', 'budget_category',
                    v_old.budget_category::text, v_committee::text, p_actor);
        END IF;

        v_updated := v_updated + 1;
    END LOOP;

    RETURN QUERY SELECT v_updated, v_unchanged;
END;
$$;

-- --- FINDING F13: canonicalise account labels -------------------------------
--
-- Checking uploads wrote the literal 'Wells' while the documentation and the
-- AI Assistant's prompt both claimed the value was 'Wells Fargo'. Run once.

-- UPDATE public.transactions SET account = 'Wells Fargo' WHERE account = 'Wells';

-- --- Backfill natural keys for rows imported before F3 -----------------------
--
-- Existing rows have no natural_key, so they cannot participate in
-- deduplication. Generate them in the same shape the application does:
-- sha1 of date | amount | normalised details | account | occurrence ordinal.
-- The application's `assign_natural_keys` is the reference implementation;
-- backfill with a one-off script rather than SQL so the two cannot diverge.
