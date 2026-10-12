use anyhow::{Context, Result, ensure};
use iarchive_core::{FileFact, ImageFeatures, SCHEMA_VERSION};
use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;
use std::path::{Path, PathBuf};

mod review;
pub use review::{
    ReviewAction, ReviewDecision, ReviewFile, ReviewGroup, ReviewInput, ReviewSnapshot,
};

pub struct Store {
    pub connection: Connection,
}
pub type ExactMember = (i64, String, String);
pub type ExactGroup = Vec<ExactMember>;
const STORE_SCHEMA_VERSION: u32 = 3;

fn nonnegative_integer(row: &rusqlite::Row<'_>, column: usize) -> rusqlite::Result<u64> {
    let value: i64 = row.get(column)?;
    u64::try_from(value).map_err(|_| rusqlite::Error::IntegralValueOutOfRange(column, value))
}

#[derive(Debug, Serialize)]
pub struct CycleStatus {
    pub cycle: String,
    pub source_root: String,
    pub stage: String,
    pub inventory_complete: bool,
    pub file_count: u64,
    pub bytes: u64,
    pub hashed_files: u64,
    pub file_errors: u64,
    pub missing_files: u64,
    pub scan_issues: u64,
    pub image_successes: u64,
    pub image_errors: u64,
    pub unsupported_visual_files: u64,
    pub local_replica: &'static str,
    pub replica_state: Option<String>,
    pub last_replica_run: Option<i64>,
    pub cloud_coverage: &'static str,
    pub cloud_asset_count: Option<u64>,
    pub status_note: &'static str,
}

impl Store {
    pub fn open_readonly(path: &Path) -> Result<Self> {
        let connection =
            Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let version: u32 = connection.pragma_query_value(None, "user_version", |r| r.get(0))?;
        ensure!(
            (1..=STORE_SCHEMA_VERSION).contains(&version),
            "unsupported state database schema"
        );
        Ok(Self { connection })
    }
    pub fn open(path: &Path) -> Result<Self> {
        let mut connection = Connection::open(path)?;
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        let version: u32 = connection.pragma_query_value(None, "user_version", |r| r.get(0))?;
        ensure!(
            version <= STORE_SCHEMA_VERSION,
            "state database is newer than this executable"
        );
        connection.pragma_update(None, "foreign_keys", true)?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        if version == 0 {
            let tx = connection.transaction()?;
            tx.execute_batch(include_str!("schema.sql"))?;
            tx.pragma_update(None, "user_version", SCHEMA_VERSION)?;
            tx.commit()?;
        }
        if version < 2 {
            let tx = connection.transaction()?;
            tx.execute_batch(include_str!("replica.sql"))?;
            tx.pragma_update(None, "user_version", 2)?;
            tx.commit()?;
        }
        if version < 3 {
            let tx = connection.transaction()?;
            tx.execute_batch(include_str!("review.sql"))?;
            tx.pragma_update(None, "user_version", STORE_SCHEMA_VERSION)?;
            tx.commit()?;
        }
        Ok(Self { connection })
    }

    pub fn roots(&self) -> Result<Vec<PathBuf>> {
        let mut roots = self
            .connection
            .prepare("SELECT source_root FROM archive_cycle")?
            .query_map([], |r| Ok(PathBuf::from(r.get::<_, String>(0)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let version: u32 = self
            .connection
            .pragma_query_value(None, "user_version", |row| row.get(0))?;
        if version >= 2 {
            roots.extend(
                self.connection
                    .prepare("SELECT secondary_root FROM replica_binding")?
                    .query_map([], |row| Ok(PathBuf::from(row.get::<_, String>(0)?)))?
                    .collect::<rusqlite::Result<Vec<_>>>()?,
            );
        }
        Ok(roots)
    }

    pub fn import(&mut self, cycle: &str, root: &Path) -> Result<()> {
        let root = root
            .to_str()
            .ok_or_else(|| anyhow::anyhow!("source root is not Unicode"))?;
        let tx = self.connection.transaction()?;
        let existing: Option<String> = tx
            .query_row(
                "SELECT source_root FROM archive_cycle WHERE id=?1",
                [cycle],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(existing) = existing {
            ensure!(
                existing == root,
                "cycle already belongs to another source; choose a new cycle ID"
            );
        } else {
            tx.execute(
                "INSERT INTO archive_cycle(id,source_root) VALUES(?1,?2)",
                params![cycle, root],
            )?;
            tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,'IMPORTED','Existing local source registered; remote coverage unverified')", [cycle])?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn root(&self, cycle: &str) -> Result<PathBuf> {
        Ok(PathBuf::from(self.connection.query_row(
            "SELECT source_root FROM archive_cycle WHERE id=?1",
            [cycle],
            |r| r.get::<_, String>(0),
        )?))
    }

    pub fn case_collisions(&self, cycle: &str) -> Result<Vec<Vec<String>>> {
        let mut stmt = self.connection.prepare("SELECT path_key FROM file_item WHERE cycle_id=?1 AND present=1 GROUP BY path_key HAVING COUNT(*)>1")?;
        let keys: Vec<String> = stmt
            .query_map([cycle], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        keys.into_iter().map(|key| {
            Ok(self.connection.prepare("SELECT relative_path FROM file_item WHERE cycle_id=?1 AND path_key=?2 AND present=1 ORDER BY relative_path")?.query_map(params![cycle,key], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
        }).collect()
    }

    pub fn exact_groups(&self, cycle: &str) -> Result<Vec<ExactGroup>> {
        let mut stmt = self.connection.prepare("SELECT sha256 FROM file_item f WHERE present=1 AND error IS NULL AND sha256 IS NOT NULL AND NOT EXISTS(SELECT 1 FROM analysis a WHERE a.file_id=f.id AND a.sha256=f.sha256 AND a.error IS NOT NULL) GROUP BY sha256 HAVING COUNT(*)>1 AND SUM(cycle_id=?1)>0 ORDER BY sha256")?;
        let hashes: Vec<String> = stmt
            .query_map([cycle], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        hashes.into_iter().map(|hash| {
            Ok(self.connection.prepare("SELECT id,cycle_id,relative_path FROM file_item f WHERE sha256=?1 AND present=1 AND error IS NULL AND NOT EXISTS(SELECT 1 FROM analysis a WHERE a.file_id=f.id AND a.sha256=f.sha256 AND a.error IS NOT NULL) ORDER BY cycle_id,relative_path")?.query_map([hash], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?.collect::<rusqlite::Result<_>>()?)
        }).collect()
    }

    pub fn visit_manifest(
        &self,
        cycle: &str,
        visitor: &mut dyn FnMut(serde_json::Value) -> Result<()>,
    ) -> Result<()> {
        let root = self.root(cycle)?;
        let mut stmt = self.connection.prepare("SELECT id,relative_path,kind,byte_count,mtime_ns,sha256,error,warnings_json,present FROM file_item WHERE cycle_id=?1 ORDER BY relative_path")?;
        let mut rows = stmt.query([cycle])?;
        while let Some(row) = rows.next()? {
            visitor(serde_json::json!({
                "schema_version":SCHEMA_VERSION,"record_type":"file","archive_id":cycle,
                "file_id":row.get::<_,i64>(0)?,"relative_path":row.get::<_,String>(1)?,
                "role":row.get::<_,String>(2)?,"byte_count":nonnegative_integer(row,3)?,
                "mtime_ns":row.get::<_,String>(4)?,"sha256":row.get::<_,Option<String>>(5)?,
                "error":row.get::<_,Option<String>>(6)?,"warnings":serde_json::from_str::<serde_json::Value>(&row.get::<_,String>(7)?)?,
                "present":row.get::<_,bool>(8)?,"cloud_asset_id":null,"pair_confidence":"unknown",
                "source_root_hint":root,"local_replica":"NOT VERIFIED","cloud_coverage":"NOT VERIFIED"
            }))?;
        }
        Ok(())
    }

    pub fn candidates(&self, cycle: &str) -> Result<Vec<serde_json::Value>> {
        let mut stmt = self.connection.prepare(
            "SELECT id,type,explanation FROM candidate_group WHERE cycle_id=?1 ORDER BY type,id",
        )?;
        let groups: Vec<(i64, String, String)> = stmt
            .query_map([cycle], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
            .collect::<rusqlite::Result<_>>()?;
        groups.into_iter().map(|(id,kind,explanation)| {
            let members: Vec<(String,String)> = self.connection.prepare("SELECT f.cycle_id,f.relative_path FROM candidate_member m JOIN file_item f ON f.id=m.file_id WHERE m.group_id=?1 ORDER BY f.cycle_id,f.relative_path")?.query_map([id],|r|Ok((r.get(0)?,r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
            Ok(serde_json::json!({"type":kind,"explanation":explanation,"members":members.into_iter().map(|(cycle,path)|serde_json::json!({"cycle":cycle,"relative_path":path})).collect::<Vec<_>>(),"review_only":true}))
        }).collect()
    }

    pub fn findings(&self, cycle: &str) -> Result<Vec<serde_json::Value>> {
        let mut stmt = self.connection.prepare("SELECT f.relative_path,f.error,a.error FROM file_item f LEFT JOIN analysis a ON a.file_id=f.id AND a.sha256=f.sha256 WHERE f.cycle_id=?1 AND (f.error IS NOT NULL OR a.error IS NOT NULL) ORDER BY f.relative_path")?;
        Ok(stmt.query_map([cycle],|r|Ok(serde_json::json!({"relative_path":r.get::<_,String>(0)?,"file_error":r.get::<_,Option<String>>(1)?,"decode_error":r.get::<_,Option<String>>(2)?,"review_only":true,"deletion_candidate":false})))?.collect::<rusqlite::Result<_>>()?)
    }

    pub fn mark_absent(&self, cycle: &str, scan: i64) -> Result<()> {
        self.connection.execute("UPDATE file_item SET present=0,error='Missing from latest completed traversal' WHERE cycle_id=?1 AND (seen_scan IS NULL OR seen_scan!=?2)", params![cycle,scan])?;
        self.connection.execute("DELETE FROM candidate_group WHERE id IN (SELECT m.group_id FROM candidate_member m JOIN file_item f ON f.id=m.file_id WHERE f.cycle_id=?1 AND f.present=0)",[cycle])?;
        Ok(())
    }

    pub fn status(&self, cycle: &str) -> Result<CycleStatus> {
        let (root, stage, complete, scan): (String, String, bool, Option<i64>) =
            self.connection.query_row(
                "SELECT source_root,stage,scan_complete,active_scan FROM archive_cycle WHERE id=?1",
                [cycle],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )?;
        let (count, bytes, hashes, errors, unsupported) = self.connection.query_row(
            "SELECT COUNT(*),COALESCE(SUM(byte_count),0),COUNT(sha256),COALESCE(SUM(error IS NOT NULL),0),COALESCE(SUM(kind NOT IN ('image','sidecar')),0) FROM file_item WHERE cycle_id=?1 AND present=1", [cycle], |r| Ok((nonnegative_integer(r,0)?,nonnegative_integer(r,1)?,nonnegative_integer(r,2)?,nonnegative_integer(r,3)?,nonnegative_integer(r,4)?)))?;
        let missing = self.connection.query_row(
            "SELECT COUNT(*) FROM file_item WHERE cycle_id=?1 AND present=0",
            [cycle],
            |r| nonnegative_integer(r, 0),
        )?;
        let issues = self.connection.query_row(
            "SELECT COUNT(*) FROM scan_issue WHERE scan_id=?1",
            [scan],
            |r| nonnegative_integer(r, 0),
        )?;
        let (successes, failures) = self.connection.query_row("SELECT COUNT(a.features_json),COUNT(a.error) FROM analysis a JOIN file_item f ON f.id=a.file_id WHERE f.cycle_id=?1 AND f.present=1 AND a.sha256=f.sha256", [cycle], |r| Ok((nonnegative_integer(r,0)?,nonnegative_integer(r,1)?)))?;
        let version: u32 = self
            .connection
            .pragma_query_value(None, "user_version", |row| row.get(0))?;
        let last: Option<(i64, String)> = if version >= 2 {
            self.connection
                .query_row(
                    "SELECT id,state FROM replica_run WHERE cycle_id=?1 ORDER BY id DESC LIMIT 1",
                    [cycle],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()?
        } else {
            None
        };
        Ok(CycleStatus {
            cycle: cycle.to_owned(),
            source_root: root,
            stage,
            inventory_complete: complete,
            file_count: count,
            bytes,
            hashed_files: hashes,
            file_errors: errors,
            missing_files: missing,
            scan_issues: issues,
            image_successes: successes,
            image_errors: failures,
            unsupported_visual_files: unsupported,
            local_replica: "NOT VERIFIED",
            replica_state: last.as_ref().map(|(_, state)| state.clone()),
            last_replica_run: last.map(|(id, _)| id),
            cloud_coverage: "NOT VERIFIED",
            cloud_asset_count: None,
            status_note: "Last recorded scan only; status does not rehash or prove current filesystem contents. Files are not iCloud assets.",
        })
    }

    pub fn begin_scan(&mut self, cycle: &str) -> Result<i64> {
        let tx = self.connection.transaction()?;
        tx.execute(
            "UPDATE replica_run SET state='STALE' WHERE cycle_id=?1 AND state='VERIFIED'",
            [cycle],
        )?;
        tx.execute(
            "UPDATE scan_run SET state='INTERRUPTED' WHERE cycle_id=?1 AND state='RUNNING'",
            [cycle],
        )?;
        tx.execute("INSERT INTO scan_run(cycle_id) VALUES(?1)", [cycle])?;
        let id = tx.last_insert_rowid();
        tx.execute(
            "UPDATE archive_cycle SET stage='INDEXING',scan_complete=0,active_scan=?2 WHERE id=?1",
            params![cycle, id],
        )?;
        tx.execute("DELETE FROM candidate_group WHERE cycle_id=?1", [cycle])?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,'INDEXING','Scan started; prior completion invalidated')", [cycle])?;
        tx.commit()?;
        Ok(id)
    }

    pub fn cached(&self, cycle: &str, path: &str) -> Result<Option<FileFact>> {
        let json: Option<String> = self.connection.query_row("SELECT json_object('relative_path',relative_path,'path_key',path_key,'kind',kind,'byte_count',byte_count,'mtime_ns',mtime_ns,'sha256',sha256,'error',error,'warnings',json(warnings_json)) FROM file_item WHERE cycle_id=?1 AND relative_path=?2", params![cycle,path], |r| r.get(0)).optional()?;
        json.map(|j| Ok(serde_json::from_str(&j)?)).transpose()
    }

    pub fn save_file(&mut self, cycle: &str, scan: i64, fact: &FileFact) -> Result<()> {
        let byte_count = i64::try_from(fact.byte_count)
            .context("file byte count exceeds SQLite integer range")?;
        let tx = self.connection.transaction()?;
        let old_hash: Option<Option<String>> = tx
            .query_row(
                "SELECT sha256 FROM file_item WHERE cycle_id=?1 AND relative_path=?2",
                params![cycle, fact.relative_path],
                |r| r.get(0),
            )
            .optional()?;
        if old_hash.as_ref().is_some_and(|hash| *hash != fact.sha256) || fact.error.is_some() {
            tx.execute("DELETE FROM candidate_group WHERE id IN (SELECT m.group_id FROM candidate_member m JOIN file_item f ON f.id=m.file_id WHERE f.cycle_id=?1 AND f.relative_path=?2)",params![cycle,fact.relative_path])?;
        }
        let kind = serde_json::to_value(&fact.kind)?;
        tx.execute("INSERT INTO file_item(cycle_id,relative_path,path_key,kind,byte_count,mtime_ns,sha256,error,warnings_json,seen_scan,present) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,1) ON CONFLICT(cycle_id,relative_path) DO UPDATE SET path_key=excluded.path_key,kind=excluded.kind,byte_count=excluded.byte_count,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,error=excluded.error,warnings_json=excluded.warnings_json,seen_scan=excluded.seen_scan,present=1",
            params![cycle,fact.relative_path,fact.path_key,kind.as_str().unwrap(),byte_count,fact.mtime_ns,fact.sha256,fact.error,serde_json::to_string(&fact.warnings)?,scan])?;
        tx.execute("DELETE FROM analysis WHERE file_id IN (SELECT id FROM file_item WHERE cycle_id=?1 AND relative_path=?2 AND (sha256 IS NULL OR sha256!=analysis.sha256))", params![cycle,fact.relative_path])?;
        tx.commit()?;
        Ok(())
    }

    pub fn scan_issue(&self, scan: i64, path: &str, error: &str) -> Result<()> {
        self.connection.execute(
            "INSERT INTO scan_issue(scan_id,path,error) VALUES(?1,?2,?3)",
            params![scan, path, error],
        )?;
        Ok(())
    }

    pub fn finish_scan(&mut self, cycle: &str, scan: i64, traversal_ok: bool) -> Result<()> {
        let tx = self.connection.transaction()?;
        if traversal_ok {
            tx.execute("UPDATE file_item SET present=0,error='Missing from latest completed traversal' WHERE cycle_id=?1 AND (seen_scan IS NULL OR seen_scan!=?2)", params![cycle,scan])?;
        }
        let errors: i64 = tx.query_row("SELECT COUNT(*) FROM file_item WHERE cycle_id=?1 AND (error IS NOT NULL OR present=0 OR sha256 IS NULL)", [cycle], |r| r.get(0))?;
        let issues: i64 = tx.query_row(
            "SELECT COUNT(*) FROM scan_issue WHERE scan_id=?1",
            [scan],
            |r| r.get(0),
        )?;
        let complete = traversal_ok && errors == 0 && issues == 0;
        let stage = if complete {
            "INDEXED"
        } else {
            "INDEX_INCOMPLETE"
        };
        tx.execute(
            "UPDATE scan_run SET state=?2,completed_at=CURRENT_TIMESTAMP WHERE id=?1",
            params![scan, stage],
        )?;
        tx.execute(
            "UPDATE archive_cycle SET stage=?2,scan_complete=?3 WHERE id=?1",
            params![cycle, stage, complete],
        )?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,?2,'Traversal ended; see file errors and scan issues')", params![cycle,stage])?;
        tx.commit()?;
        Ok(())
    }

    pub fn image_files(&self, cycle: &str) -> Result<Vec<(i64, FileFact)>> {
        let mut stmt = self.connection.prepare("SELECT id,relative_path FROM file_item WHERE cycle_id=?1 AND present=1 AND kind='image' AND sha256 IS NOT NULL AND error IS NULL ORDER BY relative_path")?;
        let paths: Vec<(i64, String)> = stmt
            .query_map([cycle], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        paths
            .into_iter()
            .map(|(id, path)| Ok((id, self.cached(cycle, &path)?.unwrap())))
            .collect()
    }

    pub fn analysis(&self, id: i64, hash: &str, version: &str) -> Result<Option<ImageFeatures>> {
        let json: Option<String> = self.connection.query_row("SELECT features_json FROM analysis WHERE file_id=?1 AND sha256=?2 AND algorithm_version=?3 AND error IS NULL", params![id,hash,version], |r| r.get(0)).optional()?.flatten();
        json.map(|j| Ok(serde_json::from_str(&j)?)).transpose()
    }

    pub fn save_analysis(
        &self,
        id: i64,
        hash: &str,
        features: &Result<ImageFeatures>,
        version: &str,
    ) -> Result<()> {
        let (json, error) = match features {
            Ok(f) => (Some(serde_json::to_string(f)?), None),
            Err(e) => (None, Some(e.to_string())),
        };
        self.connection.execute("INSERT INTO analysis(file_id,sha256,algorithm_version,features_json,error) VALUES(?1,?2,?3,?4,?5) ON CONFLICT(file_id) DO UPDATE SET sha256=excluded.sha256,algorithm_version=excluded.algorithm_version,features_json=excluded.features_json,error=excluded.error,analyzed_at=CURRENT_TIMESTAMP", params![id,hash,version,json,error])?;
        Ok(())
    }

    pub fn candidate(
        &mut self,
        cycle: &str,
        kind: &str,
        explanation: &str,
        members: &[i64],
    ) -> Result<()> {
        let tx = self.connection.transaction()?;
        tx.execute("INSERT INTO candidate_group(cycle_id,type,algorithm_version,explanation) VALUES(?1,?2,?3,?4)", params![cycle,kind,iarchive_core::ALGORITHM_VERSION,explanation])?;
        let id = tx.last_insert_rowid();
        for member in members {
            tx.execute(
                "INSERT INTO candidate_member(group_id,file_id) VALUES(?1,?2)",
                params![id, member],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn finish_audit(&mut self, cycle: &str, complete: bool) -> Result<()> {
        let tx = self.connection.transaction()?;
        let stage = if complete {
            "AUDITED"
        } else {
            "AUDIT_INCOMPLETE"
        };
        tx.execute(
            "UPDATE archive_cycle SET stage=?2 WHERE id=?1",
            params![cycle, stage],
        )?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,?2,'Read-only candidates; no deletion decision')", params![cycle,stage])?;
        tx.commit()?;
        Ok(())
    }

    pub fn begin_audit(&mut self, cycle: &str) -> Result<()> {
        let tx = self.connection.transaction()?;
        tx.execute(
            "UPDATE archive_cycle SET stage='AUDITING' WHERE id=?1",
            [cycle],
        )?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,'AUDITING','Image analysis started; partial candidates are not complete')",[cycle])?;
        tx.commit()?;
        Ok(())
    }

    pub fn replica_binding(&self, cycle: &str) -> Result<Option<String>> {
        let version: u32 = self
            .connection
            .pragma_query_value(None, "user_version", |row| row.get(0))?;
        if version < 2 {
            return Ok(None);
        }
        Ok(self
            .connection
            .query_row(
                "SELECT binding_json FROM replica_binding WHERE cycle_id=?1",
                [cycle],
                |row| row.get(0),
            )
            .optional()?)
    }

    pub fn bind_replica(&self, cycle: &str, secondary: &Path, json: &str) -> Result<()> {
        if let Some(previous) = self.replica_binding(cycle)? {
            ensure!(previous == json, "frozen replica binding changed");
        } else {
            self.connection.execute("INSERT INTO replica_binding(cycle_id,secondary_root,binding_json) VALUES(?1,?2,?3)",
                params![cycle, secondary.to_str().context("secondary root is not Unicode")?, json])?;
        }
        Ok(())
    }

    pub fn begin_replica(&mut self, cycle: &str, operation: &str) -> Result<i64> {
        let tx = self.connection.transaction()?;
        tx.execute(
            "UPDATE replica_run SET state='INTERRUPTED' WHERE cycle_id=?1 AND state IN ('RUNNING','PUBLISHING')",
            [cycle],
        )?;
        tx.execute(
            "UPDATE replica_run SET state='STALE' WHERE cycle_id=?1 AND state='VERIFIED'",
            [cycle],
        )?;
        tx.execute(
            "INSERT INTO replica_run(cycle_id,operation) VALUES(?1,?2)",
            params![cycle, operation],
        )?;
        let run = tx.last_insert_rowid();
        tx.execute(
            "UPDATE archive_cycle SET stage=?2 WHERE id=?1",
            params![
                cycle,
                if operation == "replicate" {
                    "REPLICATING"
                } else {
                    "VERIFYING"
                }
            ],
        )?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,'VERIFYING','Replica operation started; prior completion invalidated')", [cycle])?;
        tx.commit()?;
        Ok(run)
    }

    pub fn replica_progress(
        &self,
        run: i64,
        file: &FileFact,
        state: &str,
        copied_bytes: u64,
        partial: Option<&Path>,
    ) -> Result<()> {
        self.connection.execute("INSERT INTO replica_file(run_id,relative_path,state,byte_count,sha256,copied_bytes,partial_path) VALUES(?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(run_id,relative_path) DO UPDATE SET state=excluded.state,copied_bytes=excluded.copied_bytes,partial_path=excluded.partial_path",
            params![run, file.relative_path, state, i64::try_from(file.byte_count)?, file.sha256,
                i64::try_from(copied_bytes)?, partial.map(|path| path.to_str().context("partial path is not Unicode")).transpose()?])?;
        Ok(())
    }

    pub fn finish_replica(
        &mut self,
        cycle: &str,
        run: i64,
        complete: bool,
        summary: &str,
    ) -> Result<()> {
        let tx = self.connection.transaction()?;
        let state = if complete { "VERIFIED" } else { "INCOMPLETE" };
        tx.execute("UPDATE replica_run SET state=?2,completed_at=CURRENT_TIMESTAMP,summary_json=?3 WHERE id=?1", params![run, state, summary])?;
        tx.execute(
            "UPDATE archive_cycle SET stage=?2 WHERE id=?1",
            params![
                cycle,
                if complete {
                    "VERIFIED"
                } else {
                    "REPLICA_INCOMPLETE"
                }
            ],
        )?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,?2,'Uncached replica byte checks; physical independence and cloud coverage are separate')", params![cycle,state])?;
        tx.commit()?;
        Ok(())
    }

    pub fn begin_publication(&mut self, cycle: &str, run: i64, summary: &str) -> Result<()> {
        let tx = self.connection.transaction()?;
        tx.execute(
            "UPDATE replica_run SET state='PUBLISHING',summary_json=?2 WHERE id=?1",
            params![run, summary],
        )?;
        tx.execute(
            "UPDATE archive_cycle SET stage='PUBLISHING' WHERE id=?1",
            [cycle],
        )?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) VALUES(?1,'PUBLISHING','Byte checks passed; backup bundle publication is not complete')", [cycle])?;
        tx.commit()?;
        Ok(())
    }

    /// Snapshot the live WAL database through SQLite's backup API, never by copying its main file.
    pub fn backup(&self, path: &Path) -> Result<()> {
        let file = std::fs::File::create_new(path)?;
        let mut destination = Connection::open(path)?;
        let backup = rusqlite::backup::Backup::new(&self.connection, &mut destination)?;
        backup.run_to_completion(64, std::time::Duration::from_millis(10), None)?;
        drop(backup);
        destination.close().map_err(|(_, error)| error)?;
        // Windows requires a writable handle to flush the completed backup.
        file.sync_all()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_state_migrates_without_losing_inventory() {
        for version in [1, 2] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("db.sqlite");
            let connection = Connection::open(&path).unwrap();
            connection
                .execute_batch(include_str!("schema.sql"))
                .unwrap();
            if version == 2 {
                connection
                    .execute_batch(include_str!("replica.sql"))
                    .unwrap();
            }
            connection
                .pragma_update(None, "user_version", version)
                .unwrap();
            connection
                .execute(
                    "INSERT INTO archive_cycle(id,source_root) VALUES('legacy','synthetic-root')",
                    [],
                )
                .unwrap();
            drop(connection);
            assert_eq!(
                Store::open_readonly(&path)
                    .unwrap()
                    .status("legacy")
                    .unwrap()
                    .replica_state,
                None
            );
            let mut store = Store::open(&path).unwrap();
            assert_eq!(
                store.root("legacy").unwrap(),
                PathBuf::from("synthetic-root")
            );
            assert!(store.review_snapshot("legacy").unwrap().groups.is_empty());
            let first = store.begin_replica("legacy", "replicate").unwrap();
            assert_eq!(
                store.status("legacy").unwrap().replica_state.as_deref(),
                Some("RUNNING")
            );
            store
                .begin_publication("legacy", first, "{\"complete\":false}")
                .unwrap();
            drop(store);
            let mut store = Store::open(&path).unwrap();
            assert_eq!(
                store.status("legacy").unwrap().replica_state.as_deref(),
                Some("PUBLISHING")
            );
            let second = store.begin_replica("legacy", "replicate").unwrap();
            assert!(second > first);
            let interrupted: String = store
                .connection
                .query_row(
                    "SELECT state FROM replica_run WHERE id=?1",
                    [first],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(interrupted, "INTERRUPTED");
            store.finish_replica("legacy", second, false, "{}").unwrap();
            assert_eq!(
                store.status("legacy").unwrap().replica_state.as_deref(),
                Some("INCOMPLETE")
            );
        }
    }

    #[test]
    fn byte_counts_are_checked() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("db.sqlite")).unwrap();
        store.import("test", dir.path()).unwrap();
        let scan = store.begin_scan("test").unwrap();
        let mut fact = FileFact {
            relative_path: "synthetic.bin".into(),
            path_key: "synthetic.bin".into(),
            kind: iarchive_core::FileKind::Unknown,
            byte_count: 0,
            mtime_ns: "1".into(),
            sha256: Some("a".repeat(64)),
            error: None,
            warnings: vec![],
        };
        for bytes in [0, i64::MAX as u64] {
            fact.byte_count = bytes;
            store.save_file("test", scan, &fact).unwrap();
            assert_eq!(store.status("test").unwrap().bytes, bytes);
            assert_eq!(
                store
                    .cached("test", &fact.relative_path)
                    .unwrap()
                    .unwrap()
                    .byte_count,
                bytes
            );
            let mut manifest = Vec::new();
            store
                .visit_manifest("test", &mut |record| {
                    manifest.push(record);
                    Ok(())
                })
                .unwrap();
            assert_eq!(manifest[0]["byte_count"].as_u64(), Some(bytes));
        }
        fact.byte_count = i64::MAX as u64 + 1;
        assert!(store.save_file("test", scan, &fact).is_err());
        assert_eq!(store.status("test").unwrap().bytes, i64::MAX as u64);
        store
            .connection
            .execute("UPDATE file_item SET byte_count=-1", [])
            .unwrap();
        assert!(store.status("test").is_err());
        assert!(store.visit_manifest("test", &mut |_| Ok(())).is_err());
    }

    #[test]
    fn migration_and_import_are_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite");
        for _ in 0..2 {
            let mut s = Store::open(&path).unwrap();
            s.import("test", dir.path()).unwrap();
            s.import("test", dir.path()).unwrap();
            assert_eq!(s.roots().unwrap().len(), 1);
            assert_eq!(s.status("test").unwrap().cloud_coverage, "NOT VERIFIED");
            assert!(s.import("test", Path::new("different")).is_err());
        }
    }

    #[test]
    fn scan_resume_and_hash_invalidation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite");
        let mut store = Store::open(&path).unwrap();
        store.import("x", dir.path()).unwrap();
        let scan = store.begin_scan("x").unwrap();
        let mut fact = FileFact {
            relative_path: "photo.png".into(),
            path_key: "photo.png".into(),
            kind: iarchive_core::FileKind::Image,
            byte_count: 3,
            mtime_ns: "1".into(),
            sha256: Some("a".repeat(64)),
            error: None,
            warnings: vec![],
        };
        store.save_file("x", scan, &fact).unwrap();
        assert!(!store.status("x").unwrap().inventory_complete);
        drop(store);
        let mut store = Store::open(&path).unwrap();
        let resumed = store.begin_scan("x").unwrap();
        assert_eq!(
            store.cached("x", "photo.png").unwrap().unwrap().sha256,
            fact.sha256
        );
        let interrupted: String = store
            .connection
            .query_row("SELECT state FROM scan_run WHERE id=?1", [scan], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(interrupted, "INTERRUPTED");
        store.save_file("x", resumed, &fact).unwrap();
        store.finish_scan("x", resumed, true).unwrap();
        assert!(store.status("x").unwrap().inventory_complete);
        store.connection.execute("INSERT INTO analysis(file_id,sha256,algorithm_version,error) VALUES(1,?1,'v1','decode error')",[fact.sha256.as_deref().unwrap()]).unwrap();
        fact.sha256 = Some("b".repeat(64));
        let new_scan = store.begin_scan("x").unwrap();
        store.save_file("x", new_scan, &fact).unwrap();
        let count: i64 = store
            .connection
            .query_row("SELECT COUNT(*) FROM analysis", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn a_newer_schema_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite");
        let store = Store::open(&path).unwrap();
        store
            .connection
            .pragma_update(None, "user_version", 99)
            .unwrap();
        drop(store);
        assert!(Store::open(&path).is_err());
        assert!(Store::open_readonly(&path).is_err());
    }
}
