CREATE TABLE replica_binding(
 cycle_id TEXT PRIMARY KEY REFERENCES archive_cycle(id),
 secondary_root TEXT NOT NULL, binding_json TEXT NOT NULL
);
CREATE TABLE replica_run(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES archive_cycle(id),
 operation TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'RUNNING',
 started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
 summary_json TEXT, error TEXT
);
CREATE TABLE replica_file(
 run_id INTEGER NOT NULL REFERENCES replica_run(id), relative_path TEXT NOT NULL,
 state TEXT NOT NULL, byte_count INTEGER NOT NULL, sha256 TEXT, copied_bytes INTEGER NOT NULL,
 partial_path TEXT, PRIMARY KEY(run_id,relative_path)
);
INSERT INTO schema_migrations(version) VALUES(2);
