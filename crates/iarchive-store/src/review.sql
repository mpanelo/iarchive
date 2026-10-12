CREATE TABLE review_decision(
 id INTEGER PRIMARY KEY,
 cycle_id TEXT NOT NULL REFERENCES archive_cycle(id),
 fingerprint TEXT NOT NULL,
 decision_json TEXT NOT NULL,
 snapshot_json TEXT NOT NULL,
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX review_latest_idx ON review_decision(cycle_id,fingerprint,id DESC);
INSERT INTO schema_migrations(version) VALUES(3);
