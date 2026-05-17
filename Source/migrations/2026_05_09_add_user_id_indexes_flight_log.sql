-- Add missing tenant indexes on flight_log (column user_id already present, DEFAULT 2).
-- Both indexes were absent; no ALTER TABLE needed.
CREATE INDEX IF NOT EXISTS idx_flight_log_user_pair  ON flight_log(user_id, pair, timestamp);
CREATE INDEX IF NOT EXISTS idx_flight_log_user_stage ON flight_log(user_id, stage, timestamp);
