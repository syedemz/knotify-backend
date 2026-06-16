-- Migration 0011: extend deck_view with preference_vector and religion columns
--
-- Purpose
-- -------
-- Adds two columns to the deck_view materialized view:
--   - preference_vector  vector(20)  — used by the match Lambda to rank
--                                      candidates by cosine similarity when
--                                      building the swipe deck
--   - religion           text        — used by the deck handler to enforce
--                                      the religion hard filter (v1)
--
-- Why DROP + CREATE instead of ALTER
-- -----------------------------------
-- Postgres does not support ALTER MATERIALIZED VIEW ... ADD COLUMN.  The
-- standard pattern is to drop the view and recreate it with the updated
-- SELECT list.  All dependent objects (idx_deck_user, the GRANT) must be
-- recreated afterward.
--
-- Rollout note
-- ------------
-- Running this migration against a populated cluster will momentarily make
-- deck_view temporarily stale for the CONCURRENT refresh window (old columns
-- gone, new view empty until the first refresh).  Immediately after applying,
-- invoke SELECT refresh_deck_view() to repopulate the view before traffic
-- resumes.  The refresh Lambda's CloudWatch schedule (every 15 minutes, story
-- 7.4) guarantees eventual repopulation; the manual post-migration refresh
-- eliminates the gap.
--
-- Column count
-- ------------
-- Original 14 columns (migration 0009):
--   user_id, email, age, chosen_profile_avatar, current_residence_city,
--   current_residence_country, first_name, job_title, last_name, photo_url,
--   profile_complete_verified, resident_country_code, sex, username
-- New columns added here (+2):
--   preference_vector, religion
-- Total post-migration: 16 columns.

-- Drop in dependency order: function references the view; drop function first
-- so the subsequent DROP MATERIALIZED VIEW is clean.
DROP FUNCTION IF EXISTS refresh_deck_view();
DROP MATERIALIZED VIEW IF EXISTS deck_view;

-- Recreate the materialized view with the extended SELECT list.
-- Column order: original 14 columns preserved verbatim, two new columns appended.
CREATE MATERIALIZED VIEW deck_view AS
SELECT user_id, email, age, chosen_profile_avatar, current_residence_city,
       current_residence_country, first_name, job_title, last_name, photo_url,
       profile_complete_verified, resident_country_code, sex, username,
       preference_vector, religion
FROM users
WHERE deleted_at IS NULL AND profile_complete_verified = true;

-- UNIQUE index on user_id is required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX idx_deck_user ON deck_view (user_id);

-- Recreate the refresh function — body is identical to migration 0009.
-- The function is intentionally kept as a thin SQL wrapper so any caller
-- (scheduler Lambda, profile PATCH handler, admin) invokes it through a
-- stable name rather than issuing the REFRESH statement directly.
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
