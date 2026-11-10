-- Migration 0019: disable RLS on tables that rely on GRANTs, not policies
--
-- Context (phase 14, story 14.14)
-- -------------------------------
-- The knotify data model uses two authorisation strategies:
--
--   1. Row Level Security on the `users` table (migration 0007 + 0017):
--      app_user sees only rows that match app.requesting_user_id / sex GUCs.
--      This is the deliberate security surface.
--
--   2. GRANT-based authorisation on every OTHER user-facing table
--      (siblings, friendships, friend_requests, bookmarks, blocks,
--      refresh_log): app_user holds explicit INSERT/UPDATE/DELETE grants,
--      scoped by WHERE clauses in the Lambda handlers. No RLS on these
--      tables by design.
--
-- Aurora never auto-enabled RLS on CREATE TABLE, so the 6 tables above
-- lived without RLS and the migrations below were implicit (the DEFAULT
-- is DISABLED). Supabase hosts a PostgREST-safety event trigger that
-- auto-ENABLES RLS on every new public-schema table to protect against
-- accidental data exposure via the anon / authenticated roles. When our
-- yoyo migrations create those 6 tables on Supabase, each CREATE TABLE
-- is followed (invisibly, by Supabase) with
-- `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY` — and because no policies
-- exist on those tables, RLS fail-closes for app_user and aurora_refresh.
-- The first symptom was refresh_deck_view hitting
-- `new row violates row-level security policy for table "refresh_log"`.
--
-- Fix
-- ---
-- Explicitly `DISABLE ROW LEVEL SECURITY` on the 6 GRANT-based tables.
-- On Aurora this is a no-op (RLS was already disabled). On Supabase this
-- reverses the auto-enable so app_user and aurora_refresh can exercise
-- their GRANTs without being blocked by the Supabase PostgREST default.
--
-- The `users` table is explicitly LEFT with ENABLE ROW LEVEL SECURITY +
-- FORCE ROW LEVEL SECURITY; its 4 policies from migrations 0007 and 0017
-- stay intact. This is the ONE table that intentionally uses RLS.

ALTER TABLE siblings        DISABLE ROW LEVEL SECURITY;
ALTER TABLE friendships     DISABLE ROW LEVEL SECURITY;
ALTER TABLE friend_requests DISABLE ROW LEVEL SECURITY;
ALTER TABLE bookmarks       DISABLE ROW LEVEL SECURITY;
ALTER TABLE blocks          DISABLE ROW LEVEL SECURITY;
ALTER TABLE refresh_log     DISABLE ROW LEVEL SECURITY;
