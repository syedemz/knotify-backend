-- Migration 0018: allow immutable-field rewrite during the soft-delete transition
--
-- Purpose
-- -------
-- Phase 9's soft_delete_aurora Lambda issues a single UPDATE per §11.1 step 2 that:
--   - sets deleted_at = NOW()
--   - nulls PII columns (email, phone_number, photo_url, ...)
--   - rewrites first_name='Deleted', last_name='User', username='[deleted-user]'
--
-- Migration 0008 installed enforce_immutable_fields() as a BEFORE UPDATE trigger
-- which raises EXCEPTION when first_name / last_name / sex / birthday / religion /
-- subsect transition from a non-NULL value to a different value. That guard is
-- correct for normal profile updates but blocks the soft-delete rewrite of
-- first_name and last_name, so account-deletion currently fails at step 2 with:
--   "Attempted to modify immutable field"
--
-- Fix: teach the trigger about the one-shot soft-delete transition. When the
-- row transitions OLD.deleted_at IS NULL → NEW.deleted_at IS NOT NULL, the
-- immutability checks are skipped. The row is on its way to hard-purge after
-- the 30-day retention window, so anti-catfishing / matching-key invariants no
-- longer apply.
--
-- Outside the soft-delete transition (normal UPDATEs, or any UPDATE on an
-- already-soft-deleted row) the original guard is unchanged: NULL → first-value
-- is allowed once; value → different-value is blocked permanently.
--
-- Scope: redefines the function only. The trigger binding from 0008 is reused.

CREATE OR REPLACE FUNCTION enforce_immutable_fields()
RETURNS TRIGGER AS $$
BEGIN
    -- Soft-delete transition: row is being closed for retention/purge.
    -- Allow the canonical PII-redaction rewrite to proceed unblocked.
    IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
        RETURN NEW;
    END IF;

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
