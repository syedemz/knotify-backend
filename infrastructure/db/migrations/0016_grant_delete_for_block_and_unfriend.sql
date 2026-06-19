-- Migration 0016: grant DELETE on friendships / friend_requests / blocks to app_user
--
-- Purpose
-- -------
-- Phase 8 (chat) introduced the block and unfriend flows. Both paths issue
-- explicit DELETE statements:
--
--   - blocks Lambda (POST /v1/blocks):
--       DELETE FROM friendships       -- after recording the block, severs the friendship row
--       DELETE FROM friend_requests   -- removes any pending requests between the pair
--
--   - blocks Lambda (DELETE /v1/blocks/{userId}):
--       DELETE FROM blocks            -- remove the block row on unblock
--
--   - friends Lambda (DELETE /v1/friends/{userId} — unfriend):
--       DELETE FROM friendships       -- remove the canonical pair row
--
-- Migration 0007 deliberately withheld DELETE on these tables, on the assumption
-- that ON DELETE CASCADE from users would handle removals. That assumption holds
-- for account-deletion but not for block/unfriend, which leave the user rows in
-- place. Without an explicit GRANT DELETE, the Lambda hits "permission denied
-- for table friendships" on the first DELETE in the block transaction.
--
-- Scope is intentionally narrow:
--   - friendships     — required by block + unfriend
--   - friend_requests — required by block (purge in-flight requests)
--   - blocks          — required by unblock
--
-- DELETE is NOT granted on `users`, `siblings`, or `bookmarks`. Those tables'
-- removal semantics still flow through ON DELETE CASCADE from users.

GRANT DELETE ON TABLE friendships     TO app_user;
GRANT DELETE ON TABLE friend_requests TO app_user;
GRANT DELETE ON TABLE blocks          TO app_user;
