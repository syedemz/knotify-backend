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
-- Aurora never had this surface because Aurora does not expose PostgREST.
--
-- Fix
-- ---
-- 1. REVOKE every default grant on every knotify table from anon,
--    authenticated, and service_role. Our app Lambdas connect as `app_user`
--    (or `aurora_refresh`) via the Supavisor pooler — those roles are
--    unaffected. Our db_migrator connects as `postgres` — also unaffected.
--
-- 2. ALTER DEFAULT PRIVILEGES in the public schema so any future migrations
--    that CREATE TABLE as the postgres role do NOT re-introduce the hole.
--    (This covers the grants made by the OWNER role; Supabase's bootstrap
--    event trigger may or may not re-grant on CREATE TABLE — defensive
--    posture: if a future migration creates a public-schema table and the
--    probe-scripts fail with a permission_denied-style regression, add an
--    explicit REVOKE in that migration.)
--
-- 3. Leave the Aurora path unaffected. On Aurora there is no anon /
--    authenticated / service_role; the REVOKEs are no-ops there.
--
-- Operator-triggered re-grant (if someone EVER needs PostgREST on a specific
-- table in future): run `GRANT ... TO anon, authenticated` manually and
-- consciously. Do not re-enable globally.

REVOKE ALL PRIVILEGES ON TABLE
    users,
    siblings,
    friendships,
    friend_requests,
    bookmarks,
    blocks,
    refresh_log
  FROM anon, authenticated, service_role;

-- deck_view is a materialized view; same treatment.
REVOKE ALL PRIVILEGES ON TABLE deck_view FROM anon, authenticated, service_role;

-- yoyo bookkeeping — not application data but defense in depth.
REVOKE ALL PRIVILEGES ON TABLE
    _yoyo_migration,
    _yoyo_log,
    _yoyo_version,
    yoyo_lock,
    _schema_init
  FROM anon, authenticated, service_role;

-- Prevent future CREATE TABLE (as the postgres role) from re-introducing
-- the auto-grant. Covers tables + sequences + functions.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE ALL ON FUNCTIONS FROM anon, authenticated, service_role;
