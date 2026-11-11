-- Migration 0020: revoke Supabase default anon/authenticated/service_role grants
--
-- Context (phase 14, security hotfix)
-- ----------------------------------
-- Supabase ships two interacting defaults that made our data publicly readable
-- via PostgREST:
--
--   1. Every new table in the public schema automatically receives
--      `GRANT ALL ... TO anon, authenticated, service_role` through a
--      Supabase bootstrap trigger. This is designed for Supabase's
--      "database-as-backend" users who want PostgREST to work out of the box.
--
--   2. Supabase exposes PostgREST at https://<project>.supabase.co/rest/v1/*
--      backed by those same three roles. The `anon` role is used for requests
--      presenting the publishable anon API key — a key the mobile frontend
--      embeds publicly by design.
--
-- Combined effect pre-hotfix: anyone with the anon API key could issue
-- `GET /rest/v1/siblings` (and the same for friendships, friend_requests,
-- bookmarks, blocks, refresh_log) and receive the full table contents.
-- `users` was protected by migration 0007's RLS policies (which fail-closed
-- for anon since no `app.requesting_user_id` GUC is set), but RLS on the
-- other six tables was disabled by migration 0019 because they rely on
-- GRANT-based authorisation for the app_user role — leaving them wide open
-- via PostgREST.
--
-- Verified 2026-11-10 via a live anon-key probe that read a sentinel row
-- inserted into `siblings`. The leak is real.
--
-- Aurora never had this attack surface because Aurora does not expose
-- PostgREST, and the `anon`, `authenticated`, `service_role` roles do not
-- exist there.
--
-- Backend-agnostic design (phase 14 symmetry requirement)
-- -------------------------------------------------------
-- This migration must apply cleanly on both Supabase AND Aurora so the
-- `use_supabase=true -> false` toggle proven in story 14.15 remains valid.
-- On Aurora the three Supabase roles do not exist, and a bare
-- `REVOKE ... FROM anon` would raise `ERROR: role "anon" does not exist`
-- (SQLSTATE 42704) and halt the migration.
--
-- Fix: wrap every REVOKE and ALTER DEFAULT PRIVILEGES in a `DO $$ ... $$`
-- block that first checks pg_roles for each role's existence. On Supabase
-- the role exists -> the statement executes -> the PostgREST exposure is
-- closed. On Aurora the role does not exist -> the block is a no-op.
-- Clarifies the intent: this is a Supabase-specific hardening migration
-- that happens to be invariant-preserving on every other PostgreSQL.

DO $$
DECLARE
  r TEXT;
  role_list TEXT[] := ARRAY['anon', 'authenticated', 'service_role'];
BEGIN
  FOREACH r IN ARRAY role_list LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      -- Application tables
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON TABLE
            users,
            siblings,
            friendships,
            friend_requests,
            bookmarks,
            blocks,
            refresh_log,
            deck_view
         FROM %I',
        r
      );
      -- yoyo bookkeeping (defense in depth)
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON TABLE
            _yoyo_migration,
            _yoyo_log,
            _yoyo_version,
            yoyo_lock,
            _schema_init
         FROM %I',
        r
      );
      -- Future-proofing: stop any CREATE TABLE/SEQUENCE/FUNCTION
      -- performed as the postgres owner from auto-re-granting to this role.
      EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
        r
      );
      EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',
        r
      );
      EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I',
        r
      );
      RAISE NOTICE 'migration 0020: revoked grants from role %', r;
    ELSE
      RAISE NOTICE 'migration 0020: role % does not exist on this backend, skipping', r;
    END IF;
  END LOOP;
END $$;
