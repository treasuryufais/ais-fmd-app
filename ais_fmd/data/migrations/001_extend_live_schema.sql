-- ===========================================================================
-- Migration 001 -- bring the live schema up to what the rebuilt app expects.
--
-- RUN THIS ONLY AFTER docs/supabase-backup.md IS COMPLETE.
--
-- This migration is deliberately ADDITIVE. It:
--     * adds columns, all nullable or defaulted
--     * creates new tables
--     * creates indexes
--
-- It does NOT drop a column, retype a column, rename anything, or delete a row.
-- That property is what lets the CURRENT app keep running unchanged after this
-- runs: every column it reads is still there, with the same name and type. The
-- two apps can share this database while you evaluate the new one.
--
-- Every statement is idempotent (IF NOT EXISTS), so re-running it is safe and
-- a partial failure can simply be re-run rather than unpicked.
--
-- The live schema this starts from is the DDL recorded in the original repo's
-- .github/copilot-instructions.md. If the real database has drifted from that,
-- scripts/inspect_supabase.py reports the difference -- run it first.
-- ===========================================================================

BEGIN;

-- --- transactions -----------------------------------------------------------
--
-- natural_key is the deduplication identity (see domain/dedupe.py). The live
-- table has no such column, so every existing row starts NULL and cannot
-- participate in dedupe until backfilled -- scripts/backfill_natural_keys.py
-- does that, and must run after this migration.

ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS source_file text;
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS natural_key text;
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

-- Partial unique index: NULLs are excluded, so pre-backfill rows do not collide
-- with each other. This is what makes re-uploading a statement a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_natural_key
    ON public.transactions (natural_key)
    WHERE natural_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_transactions_date
    ON public.transactions (transaction_date);
CREATE INDEX IF NOT EXISTS ix_transactions_committee
    ON public.transactions (budget_category);

-- --- terms ------------------------------------------------------------------
--
-- Period locking (M10) and per-term dues rates. dues_rates is a comma-separated
-- decimal list ("35.00,52.50"); NULL means "fall back to the built-in default",
-- which is exactly the behaviour before rates became data.

ALTER TABLE public.terms ADD COLUMN IF NOT EXISTS locked boolean NOT NULL DEFAULT false;
ALTER TABLE public.terms ADD COLUMN IF NOT EXISTS locked_at timestamptz;
ALTER TABLE public.terms ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE public.terms ADD COLUMN IF NOT EXISTS dues_rates text;
ALTER TABLE public.terms ADD COLUMN IF NOT EXISTS dues_rates_verified boolean NOT NULL DEFAULT false;

-- --- uploaded_files ---------------------------------------------------------

ALTER TABLE public.uploaded_files ADD COLUMN IF NOT EXISTS row_count integer DEFAULT 0;
ALTER TABLE public.uploaded_files ADD COLUMN IF NOT EXISTS uploaded_by text;

-- --- transaction_audit (new) ------------------------------------------------
--
-- Who changed what, and when. The original app had no audit trail at all, so
-- a recategorisation was indistinguishable from data that had always been that
-- way. Nothing backfills this; it starts accumulating from the first edit.

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

CREATE INDEX IF NOT EXISTS ix_audit_txn
    ON public.transaction_audit (transaction_id);
CREATE INDEX IF NOT EXISTS ix_audit_changed_at
    ON public.transaction_audit (changed_at DESC);

-- --- merchants (new) --------------------------------------------------------
--
-- Merchant memory: a human's explicit decision about one merchant, reused on
-- every future statement. Empty until someone confirms mappings.

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

-- --- labeled_examples (new) -------------------------------------------------
--
-- Ground truth AND the decision log in one table. A ledger import is a label
-- with no prediction; a review-queue decision also records what the model said,
-- so agreement rate falls out of one query. Keyed on natural_key so re-importing
-- the same source adds nothing rather than silently doubling the training set.

CREATE TABLE IF NOT EXISTS public.labeled_examples (
    label_id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source            text NOT NULL,          -- 'ledger' | 'review' | 'spot-check'
    source_ref        text,
    era               text,                   -- scopes features that do not survive a handover
    transaction_id    integer,
    transaction_date  date,
    amount            numeric,
    details           text NOT NULL,
    account           text,
    committee_id      integer NOT NULL REFERENCES public.committees ("CommitteeID"),
    purpose           text,
    model_committee   integer,
    model_confidence  numeric,
    model_source      text,
    labeled_by        text NOT NULL,
    labeled_at        timestamptz DEFAULT now(),
    natural_key       text UNIQUE
);

CREATE INDEX IF NOT EXISTS ix_labeled_examples_source
    ON public.labeled_examples (source);
CREATE INDEX IF NOT EXISTS ix_labeled_examples_committee
    ON public.labeled_examples (committee_id);

-- --- members (new) ----------------------------------------------------------
--
-- The expected roster, for dues reconciliation. Until now the app could report
-- who DID pay (recovered from transfer descriptions) but had nothing to compare
-- that against, so "who has not paid" was unanswerable.
--
-- Uploaded from a CSV export of the membership sheet, replacing the roster for
-- that term. Names are matched against payer names extracted from transaction
-- descriptions, which is fuzzy by nature -- match_key holds the normalised form
-- so the matching rule lives in one place.

CREATE TABLE IF NOT EXISTS public.members (
    member_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    term_id       text NOT NULL REFERENCES public.terms ("TermID"),
    full_name     text NOT NULL,
    match_key     text NOT NULL,              -- normalised name, for payer matching
    email         text,
    ufid          text,
    committee_id  integer REFERENCES public.committees ("CommitteeID"),
    notes         text,
    source_file   text,
    uploaded_by   text,
    uploaded_at   timestamptz DEFAULT now(),
    CONSTRAINT members_term_match_key UNIQUE (term_id, match_key)
);

CREATE INDEX IF NOT EXISTS ix_members_term ON public.members (term_id);

COMMIT;

-- ===========================================================================
-- NOT RUN BY THIS MIGRATION
--
-- Reimbursements, receipts, roles/profiles and row-level security are all
-- deferred for the MVP -- see migrations/002_deferred_features.sql. They are
-- written down so the work is not lost, but running them now would create
-- tables nothing reads and RLS policies that would lock out an app which has
-- no login to evaluate them against.
-- ===========================================================================
