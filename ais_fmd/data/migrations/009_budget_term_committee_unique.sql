-- ===========================================================================
-- Migration 009 -- one budget per (term, committee), so budget saves work.
--
-- Independent of 001-008.
--
-- WHY. `supabase_backend.upsert_budgets` writes with
-- `ON CONFLICT (termid, committeeid)`, and Postgres refuses that unless a unique
-- constraint covers exactly those columns. `schema_postgres.sql` declares it
-- (`committeebudgets_term_committee_key`), but the live table predates that file
-- and only has its primary key -- so on the live project every budget save
-- failed with "there is no unique or exclusion constraint matching the ON
-- CONFLICT specification" (42P10). Found in the 2026-10-15 rehearsal on a copy
-- of the live database; no migration had ever added it.
--
-- SAFE ON LIVE DATA. Checked 2026-10-15: the live backup's 125 budget rows have
-- no duplicate (termid, committeeid) pairs. If a later database does, the ADD
-- CONSTRAINT below fails and changes nothing -- find the duplicates with
--
--   SELECT termid, committeeid, count(*) FROM public.committeebudgets
--    GROUP BY 1, 2 HAVING count(*) > 1;
--
-- and decide which amount is right before re-running. Never pick one by guess.
--
--   Undo:  ALTER TABLE public.committeebudgets
--            DROP CONSTRAINT committeebudgets_term_committee_key;
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'committeebudgets_term_committee_key'
           AND conrelid = 'public.committeebudgets'::regclass
    ) THEN
        ALTER TABLE public.committeebudgets
            ADD CONSTRAINT committeebudgets_term_committee_key UNIQUE (termid, committeeid);
    END IF;
END;
$$;
