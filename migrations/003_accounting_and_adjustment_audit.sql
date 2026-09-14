ALTER TABLE requests ADD COLUMN accounting_assets TEXT NOT NULL DEFAULT '{}';
ALTER TABLE requests ADD COLUMN observed_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN deduplicated_bytes INTEGER NOT NULL DEFAULT 0;

ALTER TABLE manual_adjustments ADD COLUMN reversed_at TEXT;
ALTER TABLE manual_adjustments ADD COLUMN reversed_by_adjustment_id INTEGER REFERENCES manual_adjustments(id);
ALTER TABLE manual_adjustments ADD COLUMN reversal_of_adjustment_id INTEGER REFERENCES manual_adjustments(id);

CREATE INDEX idx_adjustments_created ON manual_adjustments(created_at DESC);
