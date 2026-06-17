-- Rollback 0013: remove aurora_refresh role, grants, and refresh_log table
--
-- Order matters: drop dependent objects before the role.
--   1. Drop the refresh_log table (removes the INSERT grant implicitly).
--   2. Revoke EXECUTE on refresh_deck_view() from aurora_refresh.
--   3. Drop the aurora_refresh role.

-- Step 1: drop the refresh_log table (cascade removes the sequence too)
DROP TABLE IF EXISTS refresh_log;

-- Step 2: revoke EXECUTE on refresh_deck_view from aurora_refresh
-- Use IF EXISTS defensively so a partial-forward migration can still roll back.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aurora_refresh') THEN
        REVOKE EXECUTE ON FUNCTION refresh_deck_view() FROM aurora_refresh;
    END IF;
END
$$;

-- Step 3: drop the role
DROP ROLE IF EXISTS aurora_refresh;
