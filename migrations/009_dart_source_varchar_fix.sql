-- =========================================
-- 009: Fix VARCHAR(20) overflow on dart_*.source
-- =========================================
-- Bug: migration 008 declared:
--   source VARCHAR(20) DEFAULT 'dart_fnltt_single_acnt'  -- 22 chars
--   source VARCHAR(20) DEFAULT 'dart_fnltt_single_indx'  -- 22 chars
-- Both default values overflow the column. PostgreSQL accepted the
-- DEFAULT clause at CREATE TABLE time but rejects every INSERT that
-- attempts to use it.
--
-- Fix: widen to VARCHAR(40). Safe for both existing rows (none yet)
-- and new inserts. ALTER TYPE on a longer VARCHAR is metadata-only
-- when the new size is larger, so this is fast even on large tables.

ALTER TABLE dart_financials  ALTER COLUMN source TYPE VARCHAR(40);
ALTER TABLE dart_indicators  ALTER COLUMN source TYPE VARCHAR(40);
