CREATE TABLE jobs (
  id TEXT PRIMARY KEY CHECK (length(id) = 32),
  question TEXT NOT NULL,
  status TEXT NOT NULL,
  snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
  command TEXT CHECK (command IS NULL OR json_valid(command)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_token TEXT,
  lease_until INTEGER,
  worker_id TEXT,
  report TEXT,
  CHECK ((lease_token IS NULL) = (lease_until IS NULL))
);
CREATE INDEX jobs_pending ON jobs(created_at) WHERE command IS NOT NULL;
CREATE INDEX jobs_updated ON jobs(updated_at DESC);

CREATE TABLE worker_heartbeat (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  last_seen INTEGER NOT NULL,
  fulltext INTEGER NOT NULL CHECK (fulltext IN (0, 1))
);
