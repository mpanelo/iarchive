use crate::{CycleStatus, Store};
use anyhow::{Result, ensure};
use iarchive_core::{
    FileFact,
    relationships::{self, Group, Member, Metadata},
};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalJob {
    pub id: String,
    pub cycle: String,
    pub state: String,
    pub plan: serde_json::Value,
    pub error: Option<String>,
}

impl Store {
    pub fn cycles(&self) -> Result<Vec<CycleStatus>> {
        let cycles = self
            .connection
            .prepare("SELECT id FROM archive_cycle ORDER BY created_at,id")?
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        cycles.iter().map(|cycle| self.status(cycle)).collect()
    }

    pub fn file_facts(&self, cycle: &str) -> Result<Vec<(i64, FileFact)>> {
        let mut stmt = self.connection.prepare("SELECT id,json_object('relative_path',relative_path,'path_key',path_key,'kind',kind,'byte_count',byte_count,'mtime_ns',mtime_ns,'sha256',sha256,'error',error,'warnings',json(warnings_json)) FROM file_item WHERE cycle_id=?1 AND present=1 ORDER BY relative_path")?;
        let rows = stmt
            .query_map([cycle], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter()
            .map(|(id, json)| Ok((id, serde_json::from_str(&json)?)))
            .collect()
    }

    pub fn save_metadata(
        &self,
        id: i64,
        hash: &str,
        extractor: &str,
        metadata: &Metadata,
    ) -> Result<()> {
        self.connection.execute("INSERT INTO relationship_observation(file_id,sha256,algorithm_version,extractor_version,metadata_json) VALUES(?1,?2,?3,?4,?5) ON CONFLICT(file_id) DO UPDATE SET sha256=excluded.sha256,algorithm_version=excluded.algorithm_version,extractor_version=excluded.extractor_version,metadata_json=excluded.metadata_json,recorded_at=CURRENT_TIMESTAMP",
            params![id,hash,relationships::VERSION,extractor,serde_json::to_string(metadata)?])?;
        Ok(())
    }

    pub fn metadata(&self, id: i64, hash: &str) -> Result<Option<Metadata>> {
        let version: u32 = self
            .connection
            .pragma_query_value(None, "user_version", |r| r.get(0))?;
        if version < 4 {
            return Ok(None);
        }
        let value: Option<String> = self.connection.query_row("SELECT metadata_json FROM relationship_observation WHERE file_id=?1 AND sha256=?2 AND algorithm_version=?3 AND extractor_version=?4",
            params![id,hash,relationships::VERSION,relationships::EXTRACTOR_VERSION],|row| row.get(0)).optional()?;
        value
            .map(|value| Ok(serde_json::from_str(&value)?))
            .transpose()
    }

    pub fn relationships(&self, cycle: &str) -> Result<Vec<Group>> {
        let members = self
            .file_facts(cycle)?
            .into_iter()
            .map(|(id, fact)| {
                let metadata = match fact.sha256.as_deref() {
                    Some(hash) => self.metadata(id, hash)?.unwrap_or_default(),
                    None => Metadata::default(),
                };
                Ok(Member {
                    id,
                    fact,
                    metadata,
                    role: String::new(),
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(relationships::group(members))
    }

    pub fn active_jobs(&self, cycle: &str) -> Result<Vec<LocalJob>> {
        let version: u32 = self
            .connection
            .pragma_query_value(None, "user_version", |r| r.get(0))?;
        if version < 4 {
            return Ok(vec![]);
        }
        let ids = self
            .connection
            .prepare(
                "SELECT id FROM quarantine_job WHERE cycle_id=?1 AND state!='RESTORED' ORDER BY id",
            )?
            .query_map([cycle], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        ids.iter().map(|id| self.local_job(id)).collect()
    }

    pub fn local_job(&self, id: &str) -> Result<LocalJob> {
        let (cycle, state, json, error) = self.connection.query_row(
            "SELECT cycle_id,state,plan_json,error FROM quarantine_job WHERE id=?1",
            [id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<String>>(3)?,
                ))
            },
        )?;
        Ok(LocalJob {
            id: id.into(),
            cycle,
            state,
            plan: serde_json::from_str(&json)?,
            error,
        })
    }

    pub fn begin_local_job(
        &self,
        id: &str,
        cycle: &str,
        root: &Path,
        plan: &serde_json::Value,
    ) -> Result<()> {
        ensure!(root.is_absolute(), "quarantine root must be absolute");
        self.connection.execute("INSERT INTO quarantine_job(id,cycle_id,quarantine_root,state,plan_json) VALUES(?1,?2,?3,'MOVING',?4)",
            params![id,cycle,root.to_str(),serde_json::to_string(plan)?])?;
        self.set_job_state(id, "MOVING", None)
    }

    pub fn set_job_state(&self, id: &str, state: &str, error: Option<&str>) -> Result<()> {
        let tx = self.connection.unchecked_transaction()?;
        ensure!(tx.execute("UPDATE quarantine_job SET state=?2,error=?3,updated_at=CURRENT_TIMESTAMP WHERE id=?1",
            params![id,state,error])? == 1, "quarantine job is missing");
        tx.execute("UPDATE replica_run SET state='STALE' WHERE cycle_id=(SELECT cycle_id FROM quarantine_job WHERE id=?1) AND state='VERIFIED'",[id])?;
        tx.execute("UPDATE archive_cycle SET stage=?2 WHERE id=(SELECT cycle_id FROM quarantine_job WHERE id=?1)",params![id,if state=="RESTORED" {"REVIEWED"} else {state}])?;
        tx.execute("INSERT INTO transition_log(cycle_id,stage,reason) SELECT cycle_id,?2,'Reversible local action; see immutable quarantine plan' FROM quarantine_job WHERE id=?1",params![id,state])?;
        tx.commit()?;
        Ok(())
    }
}
