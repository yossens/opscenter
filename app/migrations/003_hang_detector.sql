-- ---------------------------------------------------------------------------
-- Migration 003: hang detector.
-- Additive operations only (ALTER/CREATE/INSERT/UPDATE); no existing
-- table/row is recreated or removed.
-- ---------------------------------------------------------------------------

-- Item snooze: local date YYYY-MM-DD until which the item is hidden from the block.
ALTER TABLE deals ADD COLUMN snoozed_until TEXT;

-- Per-stage "track hangs" flag. Terminal stages are turned off.
ALTER TABLE stages ADD COLUMN track_hangs INTEGER NOT NULL DEFAULT 1;
UPDATE stages SET track_hangs = 0 WHERE is_terminal = 1;

-- Ping log (service entries in the item feed; not notes — see design decision 1).
CREATE TABLE deal_pings (
  id              INTEGER PRIMARY KEY,
  deal_id         INTEGER NOT NULL REFERENCES deals(id),
  pinged_at       TEXT    NOT NULL,              -- UTC ISO YYYY-MM-DDTHH:MM:SS
  escalation_step INTEGER NOT NULL,              -- 1..3 at ping time
  ping_text       TEXT    NOT NULL DEFAULT ''    -- snapshot of the ping line
);
CREATE INDEX idx_deal_pings_deal ON deal_pings(deal_id, pinged_at);

-- Detector settings.
INSERT INTO app_meta (key, value) VALUES
  ('ping_template', '{waiting_for}, reminder about {counterparty}: waiting on {stage} for {days} business days. Last status: {last_note}. Any progress?'),
  ('ping_hidden_days', '2');

UPDATE app_meta SET value = '3' WHERE key = 'schema_version';
