-- Rollback for migration 0012: restore the original 5-field CHECK constraint
--
-- Steps
-- -----
--   1. Drop the widened 34-field CHECK.
--   2. Re-add the original 5-field CHECK with the same constraint name.
--   3. Drop the DEFAULT on marriage_time.
--
-- NOTE: this rollback does NOT restore NULL values in marriage_time.
-- Those rows were NULL before the migration and were backfilled to
-- 'Not Provided' — the sentinel value is a data migration, not a schema
-- requirement, and is safe to leave as-is on rollback.

-- Step 1: drop the widened CHECK
ALTER TABLE users
    DROP CONSTRAINT profile_complete_requires_required_fields;

-- Step 2: restore the original 5-field CHECK
ALTER TABLE users
    ADD CONSTRAINT profile_complete_requires_required_fields CHECK (
        profile_complete_verified = false
        OR (
            first_name IS NOT NULL
            AND last_name IS NOT NULL
            AND sex IS NOT NULL
            AND birthday IS NOT NULL
            AND username IS NOT NULL
        )
    );

-- Step 3: drop the DEFAULT on marriage_time
ALTER TABLE users
    ALTER COLUMN marriage_time DROP DEFAULT;
