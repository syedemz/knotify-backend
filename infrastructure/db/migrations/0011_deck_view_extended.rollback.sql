-- Rollback 0011: restore deck_view to the original 14-column definition
--
-- Reverses migration 0011 by:
--   1. Dropping the extended view and its refresh function.
--   2. Recreating the original 14-column deck_view from migration 0009.
--   3. Recreating idx_deck_user and refresh_deck_view() to match 0009 exactly.
--   4. Restoring the SELECT grant to app_user.
--
-- Rollout note
-- ------------
-- After applying this rollback, invoke SELECT refresh_deck_view() to
-- repopulate the restored 14-column view before traffic resumes.

-- Drop in dependency order (mirrors migration 0011).
DROP FUNCTION IF EXISTS refresh_deck_view();
DROP MATERIALIZED VIEW IF EXISTS deck_view;

-- Restore the original 14-column definition from migration 0009.
CREATE MATERIALIZED VIEW deck_view AS
SELECT user_id, email, age, chosen_profile_avatar, current_residence_city,
       current_residence_country, first_name, job_title, last_name, photo_url,
       profile_complete_verified, resident_country_code, sex, username
FROM users
WHERE deleted_at IS NULL AND profile_complete_verified = true;

CREATE UNIQUE INDEX idx_deck_user ON deck_view (user_id);

CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
$$;

GRANT SELECT ON deck_view TO app_user;
