-- ===========================================================================
-- Migration 013 -- the `profiles` table for Google sign-in. Applied to live
-- 2026-10-26.
--
-- Replaces running 002's `profiles` part plus 011. Two differences:
--   * no foreign key to `committees`: live's `committees` table is empty, so
--     the key would reject every committee assignment;
--   * locked down the way 008 locks every table: RLS on, no policies, only
--     service_role can touch it.
--
-- The seven officer mailboxes don't need rows here (config/officer_roster.json).
-- This holds anyone added on the Officer Access page.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.profiles (
    email        text PRIMARY KEY,
    display_name text,
    role         text NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'officer', 'treasurer', 'president', 'admin')),
    committee_id integer,
    created_at   timestamptz DEFAULT now(),
    created_by   text
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.profiles FROM anon, authenticated;
GRANT ALL ON public.profiles TO service_role;

COMMIT;
