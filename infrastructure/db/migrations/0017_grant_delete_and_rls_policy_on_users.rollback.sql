-- Rollback for migration 0017: drop DELETE policy + revoke DELETE on users
DROP POLICY IF EXISTS users_delete_own_row ON users;
REVOKE DELETE ON TABLE users FROM app_user;
