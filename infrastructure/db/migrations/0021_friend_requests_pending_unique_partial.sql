-- Migration 0021: replace friend_requests unique constraint with a partial
--                  unique index scoped to pending rows.
--
-- Bug this fixes
-- --------------
-- Migration 0005 declared `UNIQUE (from_user_id, to_user_id, status)` on
-- `friend_requests`. The intent was "no duplicate pending request between
-- the same pair in the same direction." The actual effect was far wider:
-- once a request transitioned to `accepted`, the `(from, to, accepted)`
-- tuple was pinned forever. After the pair unfriended and tried to
-- reconnect, a second accept in the same direction collided with the
-- stale historical row:
--
--   UniqueViolation: duplicate key value violates unique constraint
--     "friend_requests_from_user_id_to_user_id_status_key"
--   DETAIL: Key (from_user_id, to_user_id, status)=(..., ..., accepted)
--     already exists.
--
-- Reproduction: users A and B were previously friends (row
-- `(A→B, accepted)` in friend_requests, friendship row in friendships).
-- They unfriend — `_handle_unfriend` DELETEs the friendship row and
-- leaves the accepted request as audit history. A sends a new request
-- to B; B presses Accept; `_UPDATE_REQUEST_ACCEPTED_SQL` would flip the
-- new `(A→B, pending)` row to `accepted`, which collides with the stale
-- `(A→B, accepted)` row from the first handshake. The Lambda returns
-- 500; the frontend silently rolls back its optimistic update and the
-- request reappears in B's inbox.
--
-- Fix
-- ---
-- Replace the whole-table UNIQUE constraint with a partial unique index
-- scoped to pending rows only. This matches the business rule the
-- constraint was supposed to express ("at most one pending request per
-- direction between a given pair") and aligns with the existing partial
-- indexes `idx_fr_to_pending` / `idx_fr_from_pending` from migration
-- 0005. Accepted/declined/cancelled history can accumulate freely.
--
-- Safety
-- ------
-- The new constraint is strictly weaker than the old one (the old one
-- restricted all status values; the new one restricts only `pending`).
-- Any row that satisfied the old constraint also satisfies the new, so
-- no existing data can be invalidated by this migration.
--
-- The constraint-name DROP is defensive: if the constraint was created
-- with a different auto-generated name on a non-canonical replica, the
-- DROP IF EXISTS still succeeds and the CREATE UNIQUE INDEX enforces the
-- correct invariant going forward.

ALTER TABLE friend_requests
    DROP CONSTRAINT IF EXISTS friend_requests_from_user_id_to_user_id_status_key;

CREATE UNIQUE INDEX friend_requests_one_pending_per_direction
    ON friend_requests (from_user_id, to_user_id)
    WHERE status = 'pending';
