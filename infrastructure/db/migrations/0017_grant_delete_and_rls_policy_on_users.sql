-- Migration 0017: grant DELETE on users to app_user + RLS DELETE policy
--
-- Purpose
-- -------
-- Phase 9 (account deletion) introduced two Lambdas that issue DELETE against
-- the users table:
--
--   - hard_purge (per-user mode):
--       DELETE FROM users WHERE user_id = %s::uuid AND deleted_at IS NOT NULL
--
--   - hard_purge (scheduled mode):
--       per-row DELETE FROM users WHERE user_id = %s::uuid AND deleted_at IS NOT NULL
--       (iterates the eligibility set, one row per transaction, with the
--        RLS GUC set to the row being purged)
--
-- Migration 0007 deliberately withheld DELETE on users (with the assumption
-- that ON DELETE CASCADE from sibling tables would not need it).  That
-- assumption breaks for hard_purge — phase 9's whole purpose is to remove
-- the users row itself.
--
-- In addition, migration 0007 ENABLED + FORCED RLS on users but only declared
-- SELECT / INSERT / UPDATE policies.  With FORCE ROW LEVEL SECURITY, a missing
-- DELETE policy means every DELETE against the table fails-closed (zero rows
-- match) for app_user.  A permissive DELETE policy that scopes to the
-- requesting_user_id GUC is required so hard_purge (which sets the GUC to the
-- user it is removing) can match its target row.
--
-- Soft-delete (soft_delete_aurora) only needs the existing UPDATE policy.
-- Account deletion's chain of state-machine steps is:
--   step 2: soft-delete   — UPDATE under users_update_own_row policy
--   step T+30d: hard-purge — DELETE under THIS migration's new policy
-- Both branches scope to the user being deleted via the GUC, so the policy
-- shape is parallel: USING (user_id = current_setting('app.requesting_user_id', true)::uuid).
--
-- Scope is intentionally narrow:
--   - GRANT DELETE on users (the only newly-deleted table — siblings/etc.
--     still cascade)
--   - CREATE POLICY users_delete_own_row FOR DELETE with the same predicate
--     shape as users_update_own_row from migration 0007

GRANT DELETE ON TABLE users TO app_user;

CREATE POLICY users_delete_own_row
    ON users
    FOR DELETE
    USING (
        user_id = current_setting('app.requesting_user_id', true)::uuid
    );
