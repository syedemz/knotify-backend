-- Rollback 0007: remove RLS policy, disable RLS, revoke grants, drop app_user role
--
-- Reversal order is the strict inverse of the forward migration:
--   1. Drop the policy (references the table)
--   2. Disable and un-force RLS on users
--   3. Revoke table grants from app_user
--   4. Revoke schema usage from app_user
--   5. Drop the app_user role (must have no remaining privileges first)

-- ---------------------------------------------------------------------------
-- Step 1: drop the RLS policies (reverse of step 4 in forward migration)
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS users_update_own_row      ON users;
DROP POLICY IF EXISTS users_insert_own_row      ON users;
DROP POLICY IF EXISTS users_opposite_sex_only   ON users;

-- ---------------------------------------------------------------------------
-- Step 2: disable RLS on the users table
-- ---------------------------------------------------------------------------

ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
ALTER TABLE users DISABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Step 3: revoke table grants from app_user
-- ---------------------------------------------------------------------------

REVOKE SELECT, INSERT, UPDATE ON TABLE users          FROM app_user;
REVOKE SELECT, INSERT, UPDATE ON TABLE siblings       FROM app_user;
REVOKE SELECT, INSERT, UPDATE ON TABLE friendships    FROM app_user;
REVOKE SELECT, INSERT, UPDATE ON TABLE friend_requests FROM app_user;
REVOKE SELECT, INSERT, UPDATE ON TABLE bookmarks      FROM app_user;
REVOKE SELECT, INSERT, UPDATE ON TABLE blocks         FROM app_user;

-- ---------------------------------------------------------------------------
-- Step 4: revoke schema usage
-- ---------------------------------------------------------------------------

REVOKE USAGE ON SCHEMA public FROM app_user;

-- ---------------------------------------------------------------------------
-- Step 5: drop the role
-- ---------------------------------------------------------------------------

DROP ROLE IF EXISTS app_user;
