-- Migration 0013: aurora_refresh role + refresh_log instrumentation table
--
-- Purpose
-- -------
-- Creates a dedicated Postgres role `aurora_refresh` (LOGIN, NOSUPERUSER,
-- NOBYPASSRLS) that is granted EXECUTE on refresh_deck_view().  This keeps
-- the RLS-bearing app_user surface minimal; the refresh path is isolated so
-- it can be retried, scaled, and observed independently.
--
-- Also creates the `refresh_log` instrumentation table so the refresh Lambda
-- can insert one row per actual refresh performed (skip-paths do NOT insert).
-- The advisory-lock integration test counts rows and asserts exactly 1.
--
-- Steps
-- -----
--   1. Create the aurora_refresh role (LOGIN, NOSUPERUSER, NOBYPASSRLS).
--   2. Grant EXECUTE on refresh_deck_view() to aurora_refresh.
--   3. Create refresh_log(id bigserial PK, refreshed_at timestamptz NOT NULL DEFAULT now()).
--   4. Grant INSERT on refresh_log to aurora_refresh.

-- Step 1: create the aurora_refresh role
-- The password is intentionally a fixed placeholder here; the db_migrator
-- Lambda generates a cryptographically-random password post-yoyo and stores
-- it in the knotify-<env>-aurora-refresh-credential Secrets Manager secret.
CREATE ROLE aurora_refresh WITH
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOBYPASSRLS
    PASSWORD 'changeme-rotated-by-migrator';

-- Step 2: grant EXECUTE on refresh_deck_view() to aurora_refresh
-- refresh_deck_view() is defined in migration 0009 (owner = master/superuser).
-- aurora_refresh connects directly to run the function — app_user is NOT
-- granted EXECUTE on this function (least-privilege boundary).
GRANT EXECUTE ON FUNCTION refresh_deck_view() TO aurora_refresh;

-- Step 3: create the refresh_log instrumentation table
CREATE TABLE refresh_log (
    id           BIGSERIAL    PRIMARY KEY,
    refreshed_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Step 4: grant INSERT on refresh_log to aurora_refresh
-- The refresh Lambda inserts one row per actual refresh; skip-paths do not
-- insert. SELECT on the sequence is required by BIGSERIAL (INSERT needs it).
GRANT INSERT ON refresh_log TO aurora_refresh;
GRANT USAGE, SELECT ON SEQUENCE refresh_log_id_seq TO aurora_refresh;
