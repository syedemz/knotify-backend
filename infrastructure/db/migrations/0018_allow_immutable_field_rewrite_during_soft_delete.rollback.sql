-- Rollback for migration 0018: restore the original 0008 enforce_immutable_fields()
-- (no soft-delete transition bypass).

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
