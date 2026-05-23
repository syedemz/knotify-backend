-- Rollback 0008: remove trg_users_immutable trigger and enforce_immutable_fields function
--
-- Reversal order: drop the trigger before the function it references.

DROP TRIGGER IF EXISTS trg_users_immutable ON users;
DROP FUNCTION IF EXISTS enforce_immutable_fields();
