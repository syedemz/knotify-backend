-- Migration 0012: widen profile_complete_requires_required_fields CHECK + marriage_time default
--
-- Purpose
-- -------
-- The original CHECK constraint (migration 0002) requires only five fields
-- before profile_complete_verified can be set to true:
--   first_name, last_name, sex, birthday, username
--
-- Story 7.0b expands the set to 34 fields (see WIDENED_CHECK_FIELDS in the
-- phase-7-match PRD).  All 34 clauses are flat IS NOT NULL — no conditional
-- predicates.  marriage_time is intentionally NOT in the required set; instead
-- this migration sets DEFAULT 'Not Provided' on the column and backfills any
-- existing NULLs so the column is never NULL going forward, without requiring
-- the user to fill it in.
--
-- The _REQUIRED_FOR_COMPLETION constant in profile/handler.py is widened to
-- match this constraint 1:1 (same 34 column names, flat frozenset).
--
-- Steps
-- -----
--   1. Drop the original 5-field CHECK constraint.
--   2. Add the new 34-field CHECK constraint with the same name.
--   3. Set a DEFAULT on marriage_time so new INSERTs never leave it NULL.
--   4. Backfill existing NULL marriage_time rows to 'Not Provided'.

-- Step 1: drop the original CHECK
ALTER TABLE users
    DROP CONSTRAINT profile_complete_requires_required_fields;

-- Step 2: add the widened 34-field CHECK (flat IS NOT NULL, no conditional predicates)
ALTER TABLE users
    ADD CONSTRAINT profile_complete_requires_required_fields CHECK (
        profile_complete_verified = false
        OR (
            first_name IS NOT NULL
            AND last_name IS NOT NULL
            AND sex IS NOT NULL
            AND birthday IS NOT NULL
            AND username IS NOT NULL
            AND religion IS NOT NULL
            AND subsect IS NOT NULL
            AND religious_level IS NOT NULL
            AND current_residence_city IS NOT NULL
            AND current_residence_country IS NOT NULL
            AND resident_country_code IS NOT NULL
            AND district IS NOT NULL
            AND education_level IS NOT NULL
            AND highest_degree IS NOT NULL
            AND high_school IS NOT NULL
            AND higher_secondary IS NOT NULL
            AND college_name IS NOT NULL
            AND job_title IS NOT NULL
            AND employer_name IS NOT NULL
            AND employment_type IS NOT NULL
            AND office_address IS NOT NULL
            AND professional_category IS NOT NULL
            AND salary_range IS NOT NULL
            AND fathers_name IS NOT NULL
            AND fathers_job IS NOT NULL
            AND father_retired IS NOT NULL
            AND mothers_name IS NOT NULL
            AND mothers_job IS NOT NULL
            AND mother_retired IS NOT NULL
            AND family_residence_address IS NOT NULL
            AND marital_status IS NOT NULL
            AND has_children IS NOT NULL
            AND move_abroad IS NOT NULL
            AND relation IS NOT NULL
        )
    );

-- Step 3: set DEFAULT on marriage_time so new INSERTs never leave it NULL
ALTER TABLE users
    ALTER COLUMN marriage_time SET DEFAULT 'Not Provided';

-- Step 4: backfill existing NULLs
UPDATE users
    SET marriage_time = 'Not Provided'
    WHERE marriage_time IS NULL;
