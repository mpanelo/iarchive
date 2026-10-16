CREATE TABLE relationship_observation(
 file_id INTEGER PRIMARY KEY REFERENCES file_item(id), sha256 TEXT NOT NULL,
 algorithm_version TEXT NOT NULL, extractor_version TEXT NOT NULL,
 metadata_json TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE quarantine_job(
 id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES archive_cycle(id), quarantine_root TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('MOVING','QUARANTINED','RESTORING','RESTORED','INCOMPLETE')),
 plan_json TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO schema_migrations(version) VALUES(4);
