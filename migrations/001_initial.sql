CREATE TABLE owner (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  plex_id TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  email TEXT,
  claimed_at TEXT NOT NULL
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT NOT NULL,
  contribution_myr REAL NOT NULL DEFAULT 0,
  quota_bytes INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE identity_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL CHECK(provider IN ('seerr','plex','tautulli')),
  external_id TEXT NOT NULL,
  username TEXT NOT NULL,
  normalized_username TEXT NOT NULL,
  UNIQUE(provider, external_id), UNIQUE(user_id, provider)
);

CREATE TABLE requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seerr_request_id INTEGER NOT NULL UNIQUE,
  user_id INTEGER REFERENCES users(id),
  seerr_user_id TEXT NOT NULL,
  display_username TEXT NOT NULL,
  title TEXT NOT NULL,
  media_type TEXT NOT NULL,
  tmdb_id INTEGER,
  tvdb_id INTEGER,
  requested_seasons TEXT NOT NULL DEFAULT '[]',
  servarr_source TEXT,
  charged_bytes INTEGER NOT NULL DEFAULT 0 CHECK(charged_bytes >= 0),
  first_seen_at TEXT NOT NULL,
  last_updated_at TEXT NOT NULL,
  seerr_status TEXT,
  raw_metadata TEXT
);

CREATE TABLE usage_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER REFERENCES requests(id),
  user_id INTEGER REFERENCES users(id),
  delta_bytes INTEGER NOT NULL CHECK(delta_bytes >= 0),
  total_charged_bytes INTEGER NOT NULL CHECK(total_charged_bytes >= 0),
  source TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE manual_adjustments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  bytes INTEGER NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  success INTEGER,
  processed INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  message TEXT
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX idx_requests_user ON requests(user_id);
CREATE INDEX idx_ledger_user ON usage_ledger(user_id);

