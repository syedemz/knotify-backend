-- Rollback for 0001_enable_extensions
--
-- Drops the three extensions in reverse creation order.
-- CASCADE is required because subsequent migrations (e.g. 0002_create_users)
-- create objects that depend on these extensions (vector columns, GIN indexes,
-- gen_random_uuid() calls). Rolling back 0001 without first rolling back those
-- dependents is only valid after 0002 has been rolled back first.
--
-- yoyo rolls back migrations in reverse order automatically, so by the time
-- this script runs, the users table (and its vector column) is already gone.

DROP EXTENSION IF EXISTS pgcrypto CASCADE;
DROP EXTENSION IF EXISTS pg_trgm CASCADE;
DROP EXTENSION IF EXISTS vector CASCADE;
