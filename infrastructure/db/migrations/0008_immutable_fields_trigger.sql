-- Migration 0008: enforce_immutable_fields trigger function and trigger on users
--
-- Purpose
-- -------
-- Implements defense-in-depth enforcement of immutable-after-set profile fields
-- per architecture.md §5.7.  The API layer and Lambda validators already reject
-- mutations to these fields; this trigger is the final guard at the database
-- boundary so that no code path — however privileged — can silently overwrite
-- identity-critical data.
--
-- Immutable-after-set fields covered:
--   first_name, last_name — anti-catfishing
--   sex                   — hard partition key for the matching algorithm
--   birthday              — identity-critical
--   religion, subsect     — matching-critical
--
-- Note: user_id and email are managed by Cognito and are not mutated via SQL
-- UPDATE paths, so they are not included in the trigger check.
--
-- Bootstrap semantics (phase-3 brainstorm decision):
--   These columns start NULL at signup (the Cognito post-confirmation Lambda
--   inserts a minimal row from whatever Cognito supplied — see 0002 header).
--   They must be settable once at profile completion. The trigger therefore
--   only fires the EXCEPTION when OLD.<field> IS NOT NULL — a NULL → first-value
--   transition is allowed exactly once per field, and value → different-value
--   thereafter is blocked permanently.
--
-- Trigger behaviour:
--   BEFORE UPDATE fires per row BEFORE the change is applied.
--   For each immutable-after-set column, if OLD.<col> IS NOT NULL AND
--   NEW.<col> IS DISTINCT FROM OLD.<col>, the function raises
--   EXCEPTION 'Attempted to modify immutable field', which:
--     - aborts the statement with an error code P0001 (raise_exception)
--     - rolls back the triggering transaction automatically
--   If OLD.<col> IS NULL, any value (including NULL) is allowed — this is the
--   profile-completion path.
--   If none of the immutable columns changed, RETURN NEW allows the UPDATE
--   to proceed normally (mutable fields are unaffected).

-- ---------------------------------------------------------------------------
-- Step 1: trigger function
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_immutable_fields()
RETURNS TRIGGER AS $$
BEGIN
    IF (OLD.first_name IS NOT NULL AND NEW.first_name IS DISTINCT FROM OLD.first_name)
       OR (OLD.last_name IS NOT NULL AND NEW.last_name IS DISTINCT FROM OLD.last_name)
       OR (OLD.sex IS NOT NULL AND NEW.sex IS DISTINCT FROM OLD.sex)
       OR (OLD.birthday IS NOT NULL AND NEW.birthday IS DISTINCT FROM OLD.birthday)
       OR (OLD.religion IS NOT NULL AND NEW.religion IS DISTINCT FROM OLD.religion)
       OR (OLD.subsect IS NOT NULL AND NEW.subsect IS DISTINCT FROM OLD.subsect) THEN
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
