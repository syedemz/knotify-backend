-- Migration 0008: enforce_immutable_fields trigger function and trigger on users
--
-- Purpose
-- -------
-- Implements defense-in-depth enforcement of immutable profile fields per
-- architecture.md §5.7.  The API layer and Lambda validators already reject
-- mutations to these fields; this trigger is the final guard at the database
-- boundary so that no code path — however privileged — can silently overwrite
-- identity-critical data.
--
-- Immutable fields covered:
--   first_name, last_name — anti-catfishing
--   sex                   — hard partition key for the matching algorithm
--   birthday              — identity-critical
--   religion, subsect     — matching-critical
--
-- Note: user_id and email are managed by Cognito and are not mutated via SQL
-- UPDATE paths, so they are not included in the trigger check.
--
-- Trigger behaviour:
--   BEFORE UPDATE fires per row BEFORE the change is applied.
--   If any of the six immutable columns differs between OLD and NEW, the
--   function raises EXCEPTION 'Attempted to modify immutable field', which:
--     - aborts the statement with an error code P0001 (raise_exception)
--     - rolls back the triggering transaction automatically
--   If none of the immutable columns changed, RETURN NEW allows the UPDATE
--   to proceed normally (mutable fields are unaffected).

-- ---------------------------------------------------------------------------
-- Step 1: trigger function
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_immutable_fields()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.first_name IS DISTINCT FROM OLD.first_name
       OR NEW.last_name IS DISTINCT FROM OLD.last_name
       OR NEW.sex IS DISTINCT FROM OLD.sex
       OR NEW.birthday IS DISTINCT FROM OLD.birthday
       OR NEW.religion IS DISTINCT FROM OLD.religion
       OR NEW.subsect IS DISTINCT FROM OLD.subsect THEN
        RAISE EXCEPTION 'Attempted to modify immutable field';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Step 2: trigger on users
-- ---------------------------------------------------------------------------

CREATE TRIGGER trg_users_immutable
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION enforce_immutable_fields();
