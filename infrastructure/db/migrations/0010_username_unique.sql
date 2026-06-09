-- Migration 0010: case-insensitive partial unique index on users.username
--
-- Adds database-side enforcement against username races and duplicate handles.
--
-- Design decisions:
--
--   CASE-INSENSITIVE: The index is on lower(username) so that 'Alice', 'alice',
--   and 'ALICE' are treated as the same handle. This matches the lookup contract
--   in GET /v1/profiles?username=... which uses WHERE lower(username) = lower($1).
--
--   PARTIAL (WHERE username IS NOT NULL): The users table follows the
--   minimal-bootstrap-row pattern (migration 0002) — the post-confirmation Lambda
--   inserts a row with username = NULL because the username is not known at signup
--   time. Many such NULL rows may coexist, which would be impossible under a
--   non-partial unique constraint (NULL = NULL evaluates to NULL in SQL, but some
--   databases still enforce uniqueness over NULLs via a non-partial index). The
--   explicit WHERE clause removes all NULL rows from the index entirely, so
--   multiple NULL-username bootstrap rows never collide.
--
--   PURE INDEX ADD: No schema change to the users table is required. The index is
--   additive and applies to the existing column definition from migration 0002.
--
-- API-side rate limiting (1 rename per 30 days per architecture §5.7) is
-- DEFERRED to phase 11 (hardening). This migration provides the DB-side guard;
-- the application-layer throttle ships later.

CREATE UNIQUE INDEX users_username_lower_unique
    ON users (lower(username))
    WHERE username IS NOT NULL;
