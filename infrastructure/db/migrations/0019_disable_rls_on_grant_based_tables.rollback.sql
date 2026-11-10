-- Rollback 0019: re-enable RLS on the 6 GRANT-based tables.
--
-- On Supabase this restores the PostgREST-safety auto-default state that
-- 0019 reversed. On Aurora this ENABLES RLS on tables that previously had
-- it disabled — which fails-closed every Lambda path that relies on these
-- tables (Supabase and Aurora are symmetrically broken on rollback).
--
-- Rollback is safe only when paired with a schema teardown. Do NOT roll
-- 0019 back in isolation on a live environment.

ALTER TABLE siblings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE friendships     ENABLE ROW LEVEL SECURITY;
ALTER TABLE friend_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookmarks       ENABLE ROW LEVEL SECURITY;
ALTER TABLE blocks          ENABLE ROW LEVEL SECURITY;
ALTER TABLE refresh_log     ENABLE ROW LEVEL SECURITY;
