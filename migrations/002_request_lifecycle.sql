ALTER TABLE requests ADD COLUMN requested_at TEXT;
ALTER TABLE requests ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN deleted_at TEXT;
ALTER TABLE requests ADD COLUMN refunded_bytes INTEGER NOT NULL DEFAULT 0;

UPDATE requests SET requested_at = first_seen_at WHERE requested_at IS NULL;
CREATE INDEX idx_requests_deleted ON requests(is_deleted);
