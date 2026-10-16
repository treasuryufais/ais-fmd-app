-- ===========================================================================
-- Migration 008 -- "This isn't ours" flags. NOT YET RUN. Treasurer decision.
--
-- Adds the table behind the VP portal's "This isn't ours" button: a VP who thinks
-- a charge booked to their committee is wrong flags it with a note, and it lands
-- in the treasurer's Review Queue. A flag is a message, never an edit -- nothing
-- here changes `transactions`.
--
-- Written but not applied to any database. Run it against a throwaway Supabase
-- project first (see HANDOFF.md section 8.4), then the real one only when the
-- treasurer has decided VPs go live. Until it is run, the VP page and Review
-- Queue hide the feature instead of erroring (the backend reports the table
-- missing and the pages check before drawing).
--
-- Like migration 002, this does not enable row-level security: authorization
-- stays application-level (`auth.require`, plus the check inside
-- `create_flag` that a charge is booked to the caller's committee).
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.transaction_flags (
    flag_id                integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id         integer NOT NULL REFERENCES public.transactions (transactionid),
    -- The committee line the charge sat in when flagged. Scopes a VP's view of
    -- their own flags even if the treasurer later moves the charge elsewhere.
    booked_to_committee_id integer,
    flagged_by             text NOT NULL,
    note                   text NOT NULL
        CHECK (char_length(note) BETWEEN 5 AND 500),
    status                 text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'dismissed')),
    flagged_at             timestamptz DEFAULT now(),
    resolved_at            timestamptz,
    resolved_by            text,
    resolution_note        text
);

CREATE INDEX IF NOT EXISTS ix_flags_status
    ON public.transaction_flags (status);

-- At most one open flag per charge, so a double-click cannot stack duplicates in
-- the treasurer's queue.
CREATE UNIQUE INDEX IF NOT EXISTS ux_flags_one_open
    ON public.transaction_flags (transaction_id) WHERE status = 'open';

COMMIT;
