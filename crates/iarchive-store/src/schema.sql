CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE archive_cycle(
 id TEXT PRIMARY KEY, source_root TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 stage TEXT NOT NULL DEFAULT 'IMPORTED', scan_complete INTEGER NOT NULL DEFAULT 0,
 active_scan INTEGER, cloud_coverage TEXT NOT NULL DEFAULT 'NOT VERIFIED'
);
CREATE TABLE transition_log(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES archive_cycle(id),
 stage TEXT NOT NULL, reason TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE scan_run(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES archive_cycle(id),
 started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, state TEXT NOT NULL DEFAULT 'RUNNING'
);
CREATE TABLE file_item(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES archive_cycle(id),
 relative_path TEXT NOT NULL, path_key TEXT NOT NULL, kind TEXT NOT NULL,
 byte_count INTEGER NOT NULL, mtime_ns TEXT NOT NULL, sha256 TEXT, error TEXT,
 warnings_json TEXT NOT NULL DEFAULT '[]', seen_scan INTEGER REFERENCES scan_run(id),
 present INTEGER NOT NULL DEFAULT 1, UNIQUE(cycle_id, relative_path)
);
CREATE INDEX file_hash_idx ON file_item(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX file_cycle_idx ON file_item(cycle_id, present);
CREATE INDEX file_path_key_idx ON file_item(cycle_id, path_key);
CREATE TABLE scan_issue(
 id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scan_run(id), path TEXT NOT NULL, error TEXT NOT NULL
);
CREATE TABLE analysis(
 file_id INTEGER PRIMARY KEY REFERENCES file_item(id), sha256 TEXT NOT NULL,
 algorithm_version TEXT NOT NULL, features_json TEXT, error TEXT,
 analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE candidate_group(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES archive_cycle(id),
 type TEXT NOT NULL, algorithm_version TEXT NOT NULL, explanation TEXT NOT NULL
);
CREATE TABLE candidate_member(
 group_id INTEGER NOT NULL REFERENCES candidate_group(id) ON DELETE CASCADE,
 file_id INTEGER NOT NULL REFERENCES file_item(id), PRIMARY KEY(group_id, file_id)
);
INSERT INTO schema_migrations(version) VALUES(1);
