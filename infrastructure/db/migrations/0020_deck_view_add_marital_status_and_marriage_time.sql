-- Migration 0020: extend deck_view with marital_status and marriage_time
--
-- Purpose
-- -------
-- Adds two columns to the deck_view materialized view so GET /match/deck
-- can ship marital status + the user's marriage-timeline preference to
-- the swipe deck UI:
--   - marital_status   TEXT   — "Never Married", "Divorced", etc. Collected
--                               during onboarding, requiredForCompletion.
--   - marriage_time    TEXT   — "4-12 months", "1-2 years", etc. Backfilled
--                               by migration 0012 so this column is never
--                               NULL for new rows.
--
-- Why DROP + CREATE instead of ALTER
-- -----------------------------------
-- Postgres does not support ALTER MATERIALIZED VIEW ... ADD COLUMN.  Same
-- pattern as migration 0011 (deck_view_extended): drop the view and the
-- refresh function, recreate both with the updated SELECT list, then
-- recreate the dependent index and GRANT.
--
-- Rollout note
-- ------------
-- Running this migration against a populated cluster will momentarily make
-- deck_view empty until the first refresh.  Immediately after applying,
-- invoke SELECT refresh_deck_view() to repopulate the view before traffic
-- resumes.  The refresh Lambda's CloudWatch schedule (every 15 minutes)
-- guarantees eventual repopulation; the manual post-migration refresh
-- eliminates the gap.
--
-- Column count
-- ------------
-- Pre-0020 (migration 0011 state): 16 columns.
-- New columns added here (+2): marital_status, marriage_time.
-- Total post-0020: 18 columns.

-- Drop in dependency order: function references the view; drop function
-- first so the subsequent DROP MATERIALIZED VIEW is clean.
DROP FUNCTION IF EXISTS refresh_deck_view();
DROP MATERIALIZED VIEW IF EXISTS deck_view;

-- Recreate the materialized view with the extended SELECT list.
-- Column order: original 16 columns preserved verbatim, two new columns
-- appended.
CREATE MATERIALIZED VIEW deck_view AS
SELECT user_id, email, age, chosen_profile_avatar, current_residence_city,
       current_residence_country, first_name, job_title, last_name, photo_url,
       profile_complete_verified, resident_country_code, sex, username,
       preference_vector, religion,
       marital_status, marriage_time
FROM users
WHERE deleted_at IS NULL AND profile_complete_verified = true;

-- UNIQUE index on user_id is required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX idx_deck_user ON deck_view (user_id);

-- Recreate the refresh function — body identical to migration 0011.  Kept
-- as a thin SQL wrapper so every caller (scheduler Lambda, admin) invokes
-- through a stable name rather than issuing the REFRESH statement directly.
CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
$$;

-- Restore the SELECT grant so the match Lambda (running as app_user) can
-- read deck_view.  refresh_deck_view() is intentionally NOT granted to
-- app_user — refresh is an admin/scheduler operation.
GRANT SELECT ON deck_view TO app_user;
