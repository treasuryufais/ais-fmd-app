-- ===========================================================================
-- Migration 012 -- sponsorship goal per term, sponsor name per deposit. NOT YET RUN.
--
-- Backs the sponsorship tracking (M20) on the Dashboard and the Treasury page.
-- Mirrors the two columns `sqlite_backend._migrate` adds:
--
--   terms.sponsorship_goal    the term's sponsorship dollar goal. NULL means
--                             "no goal set", which is not the same as $0.
--   transactions.sponsor_name who a sponsorship deposit was from. Typed in by
--                             whoever reconciles it; bank lines never carry it.
--
-- Additive and nullable: no existing row changes, and the app reads both with
-- `.get()`, so it works before and after this runs. Until it runs, saving a
-- goal or a sponsor name fails with the column-missing error and changes nothing.
--
-- New columns on existing tables keep those tables' RLS and grants from
-- 008_lock_down.sql, so nothing else is needed here.
--
--   Undo:  ALTER TABLE public.terms DROP COLUMN IF EXISTS sponsorship_goal;
--          ALTER TABLE public.transactions DROP COLUMN IF EXISTS sponsor_name;
-- ===========================================================================

BEGIN;

ALTER TABLE public.terms        ADD COLUMN IF NOT EXISTS sponsorship_goal numeric(12, 2);
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS sponsor_name     text;

COMMIT;
