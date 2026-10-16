use crate::{CycleStatus, Store, nonnegative_integer};
use anyhow::{Result, ensure};
use iarchive_core::{ALGORITHM_VERSION, ImageFeatures};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReviewAction {
    KeepAll,
    FlagSelected,
    Defer,
    Reset,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewInput {
    pub fingerprint: String,
    pub action: ReviewAction,
    #[serde(default)]
    pub selected: Vec<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewDecision {
    pub action: ReviewAction,
    pub selected: Vec<i64>,
    pub recorded_at: String,
}

#[derive(Debug, Serialize)]
pub struct ReviewFile {
    pub id: i64,
    pub cycle: String,
    pub relative_path: String,
    pub kind: String,
    pub byte_count: u64,
    pub mtime_ns: String,
    pub sha256: Option<String>,
    pub present: bool,
    pub file_error: Option<String>,
    pub decode_error: Option<String>,
    pub warnings: Vec<String>,
    pub features: Option<serde_json::Value>,
    pub pair_confidence: &'static str,
    pub preview_available: bool,
    pub relationship: Option<serde_json::Value>,
    pub location: String,
    #[serde(skip)]
    pub thumbnail: Option<String>,
    // Bind cross-cycle members to their recorded scans as well.
    pub scan: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct ReviewGroup {
    pub fingerprint: String,
    pub kind: String,
    pub explanation: String,
    pub algorithm_version: String,
    pub members: Vec<ReviewFile>,
    pub decision: Option<ReviewDecision>,
}

#[derive(Debug, Serialize)]
pub struct ReviewSnapshot {
    pub status: CycleStatus,
    pub groups: Vec<ReviewGroup>,
    pub findings: Vec<ReviewFile>,
    pub scan_issues: Vec<serde_json::Value>,
    pub decision_history_count: u64,
    pub note: &'static str,
}

impl Store {
    /// Current Keep decisions protect shared members across all imported cycles.
    pub fn kept_review_files(&self) -> Result<BTreeSet<i64>> {
        let cycles = self
            .connection
            .prepare("SELECT id FROM archive_cycle ORDER BY id")?
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut kept = BTreeSet::new();
        for cycle in cycles {
            for group in self.review_snapshot(&cycle)?.groups {
                if group
                    .decision
                    .is_some_and(|d| d.action == ReviewAction::KeepAll)
                {
                    kept.extend(group.members.into_iter().map(|file| file.id));
                }
            }
        }
        Ok(kept)
    }

    fn review_file(
        &self,
        id: i64,
        relationships: &std::collections::BTreeMap<i64, serde_json::Value>,
        moved: &std::collections::BTreeMap<i64, String>,
    ) -> Result<ReviewFile> {
        let (mut file, warnings, features) = self.connection.query_row(
            "SELECT f.cycle_id,f.relative_path,f.kind,f.byte_count,f.mtime_ns,f.sha256,f.present,f.error,a.error,f.warnings_json,a.features_json,c.active_scan FROM file_item f JOIN archive_cycle c ON c.id=f.cycle_id LEFT JOIN analysis a ON a.file_id=f.id AND a.sha256=f.sha256 AND a.algorithm_version=?2 WHERE f.id=?1",
            params![id, ALGORITHM_VERSION],
            |r| Ok((ReviewFile {
                id, cycle: r.get(0)?, relative_path: r.get(1)?, kind: r.get(2)?,
                byte_count: nonnegative_integer(r,3)?, mtime_ns: r.get(4)?, sha256: r.get(5)?,
                present: r.get(6)?, file_error: r.get(7)?, decode_error: r.get(8)?,
                warnings: Vec::new(), features: None, thumbnail: None,
                preview_available: false, pair_confidence: "unknown", scan: r.get(11)?, relationship:None, location:"primary (recorded)".into(),
            }, r.get::<_,String>(9)?, r.get::<_,Option<String>>(10)?)),
        )?;
        file.warnings = serde_json::from_str(&warnings)?;
        file.relationship = relationships.get(&id).cloned();
        if file
            .relationship
            .as_ref()
            .is_some_and(|r| r["blockers"].as_array().is_some_and(Vec::is_empty))
        {
            file.pair_confidence = "metadata_observed";
        }
        if let Some(location) = moved.get(&id) {
            file.location = location.clone();
        }
        if file.present && file.file_error.is_none() && file.decode_error.is_none() {
            if let Some(features) = features {
                let parsed: ImageFeatures = serde_json::from_str(&features)?;
                file.thumbnail = Some(parsed.thumbnail.clone());
                let mut metrics = serde_json::to_value(parsed)?;
                metrics.as_object_mut().unwrap().remove("thumbnail");
                file.features = Some(metrics);
            }
        }
        Ok(file)
    }

    /// Saved audit observations only; never opens media or proves current byte equality.
    pub fn review_snapshot(&self, cycle: &str) -> Result<ReviewSnapshot> {
        let status = self.status(cycle)?;
        let mut cycles:BTreeSet<String>=self.connection.prepare("SELECT DISTINCT f.cycle_id FROM file_item f JOIN candidate_member m ON m.file_id=f.id JOIN candidate_group g ON g.id=m.group_id WHERE g.cycle_id=?1")?
            .query_map([cycle], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        cycles.insert(cycle.into());
        let mut relationships = std::collections::BTreeMap::new();
        let mut local_relationships = vec![];
        let mut moved = std::collections::BTreeMap::new();
        for related_cycle in cycles {
            for group in self.relationships(&related_cycle)? {
                for member in &group.members {
                    relationships.insert(member.id,serde_json::json!({"group":group.fingerprint,"role":member.role,
                        "members":group.members.iter().map(|m|&m.fact.relative_path).collect::<Vec<_>>(),"blockers":group.blockers}));
                }
                if related_cycle == cycle {
                    local_relationships.push(group);
                }
            }
            for job in self.active_jobs(&related_cycle)? {
                if let Some(files) = job.plan["files"].as_array() {
                    let paths: BTreeSet<_> = files
                        .iter()
                        .filter_map(|f| f["relative_path"].as_str())
                        .collect();
                    for (id, f) in self.file_facts(&related_cycle)? {
                        if paths.contains(f.relative_path.as_str()) {
                            moved.insert(
                                id,
                                if job.state == "QUARANTINED" {
                                    "quarantine (recorded)"
                                } else {
                                    "needs journal reconciliation"
                                }
                                .into(),
                            );
                        }
                    }
                }
            }
        }
        let scan: Option<i64> = self.connection.query_row(
            "SELECT active_scan FROM archive_cycle WHERE id=?1",
            [cycle],
            |r| r.get(0),
        )?;
        let groups = self.connection.prepare(
            "SELECT id,type,explanation,algorithm_version FROM candidate_group WHERE cycle_id=?1 ORDER BY type,id",
        )?.query_map([cycle], |r| Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut reviewed = Vec::new();
        for (id, kind, explanation, algorithm_version) in groups {
            let ids = self
                .connection
                .prepare("SELECT file_id FROM candidate_member WHERE group_id=?1 ORDER BY file_id")?
                .query_map([id], |r| r.get::<_, i64>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let members = ids
                .into_iter()
                .map(|id| self.review_file(id, &relationships, &moved))
                .collect::<Result<Vec<_>>>()?;
            let mut group = ReviewGroup {
                fingerprint: String::new(),
                kind,
                explanation,
                algorithm_version,
                members,
                decision: None,
            };
            // Scan and candidate IDs prevent old decisions from attaching to reused row IDs.
            let bytes = serde_json::to_vec(&(cycle, scan, id, &group))?;
            group.fingerprint = format!("{:x}", Sha256::digest(bytes));
            let decision: Option<(String,String)> = self.connection.query_row(
                "SELECT decision_json,recorded_at FROM review_decision WHERE cycle_id=?1 AND fingerprint=?2 ORDER BY id DESC LIMIT 1",
                params![cycle,group.fingerprint], |r| Ok((r.get(0)?,r.get(1)?)),
            ).optional()?;
            if let Some((json, recorded_at)) = decision {
                let input: ReviewInput = serde_json::from_str(&json)?;
                if input.action != ReviewAction::Reset {
                    group.decision = Some(ReviewDecision {
                        action: input.action,
                        selected: input.selected,
                        recorded_at,
                    });
                }
            }
            reviewed.push(group);
        }
        for related in local_relationships {
            let live = related
                .members
                .iter()
                .any(|m| m.metadata.content_id.is_some());
            let video = related
                .members
                .iter()
                .all(|m| m.fact.kind == iarchive_core::FileKind::Video);
            if !live && !video && related.members.len() < 2 {
                continue;
            }
            let members = related
                .members
                .iter()
                .map(|m| self.review_file(m.id, &relationships, &moved))
                .collect::<Result<Vec<_>>>()?;
            let mut group = ReviewGroup {
                fingerprint: String::new(),
                kind: if live {
                    "live_photo"
                } else if video {
                    "video"
                } else {
                    "variants"
                }
                .into(),
                explanation: if related.blockers.is_empty() {
                    "Metadata identifies these local components. Flagging an image proposes the whole group; quarantine still requires a separately confirmed, freshly verified plan.".into()
                } else {
                    related.blockers.join("; ")
                },
                algorithm_version: iarchive_core::relationships::VERSION.into(),
                members,
                decision: None,
            };
            group.fingerprint = format!(
                "{:x}",
                Sha256::digest(serde_json::to_vec(&(
                    cycle,
                    scan,
                    &related.fingerprint,
                    &group
                ))?)
            );
            let decision:Option<(String,String)>=self.connection.query_row("SELECT decision_json,recorded_at FROM review_decision WHERE cycle_id=?1 AND fingerprint=?2 ORDER BY id DESC LIMIT 1",
                params![cycle,group.fingerprint],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            if let Some((json, recorded_at)) = decision {
                let input: ReviewInput = serde_json::from_str(&json)?;
                if input.action != ReviewAction::Reset {
                    group.decision = Some(ReviewDecision {
                        action: input.action,
                        selected: input.selected,
                        recorded_at,
                    });
                }
            }
            reviewed.push(group);
        }
        let ids = self.connection.prepare(
            "SELECT f.id FROM file_item f LEFT JOIN analysis a ON a.file_id=f.id AND a.sha256=f.sha256 AND a.algorithm_version=?2 WHERE f.cycle_id=?1 AND (f.error IS NOT NULL OR f.present=0 OR a.error IS NOT NULL OR f.kind NOT IN ('image','sidecar') OR (f.kind='image' AND a.features_json IS NULL)) ORDER BY f.relative_path",
        )?.query_map(params![cycle,ALGORITHM_VERSION], |r| r.get::<_,i64>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let findings = ids
            .into_iter()
            .map(|id| self.review_file(id, &relationships, &moved))
            .collect::<Result<Vec<_>>>()?;
        let scan_issues = self
            .connection
            .prepare("SELECT path,error FROM scan_issue WHERE scan_id=?1 ORDER BY id")?
            .query_map([scan], |r| {
                Ok(serde_json::json!({"path":r.get::<_,String>(0)?,"error":r.get::<_,String>(1)?}))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let decision_history_count = self.connection.query_row(
            "SELECT COUNT(*) FROM review_decision WHERE cycle_id=?1",
            [cycle],
            |r| nonnegative_integer(r, 0),
        )?;
        Ok(ReviewSnapshot {
            status,
            groups: reviewed,
            findings,
            scan_issues,
            decision_history_count,
            note: "Saved audit snapshot; no current media verification. Decisions are review notes only. Local relationship labels are metadata observations, not cloud coverage or move approval.",
        })
    }

    /// Append a decision bound to the current recorded group, retaining earlier history.
    pub fn record_review(&mut self, cycle: &str, input: &ReviewInput) -> Result<()> {
        let snapshot = self.review_snapshot(cycle)?;
        let group = snapshot
            .groups
            .into_iter()
            .find(|g| g.fingerprint == input.fingerprint)
            .ok_or_else(|| {
                anyhow::anyhow!("review group changed or is outside this cycle; reload the gallery")
            })?;
        let selected: BTreeSet<_> = input.selected.iter().copied().collect();
        ensure!(
            selected.len() == input.selected.len(),
            "duplicate selection"
        );
        ensure!(
            selected
                .iter()
                .all(|id| group.members.iter().any(|m| m.id == *id)),
            "selection is outside the group"
        );
        match input.action {
            ReviewAction::FlagSelected => ensure!(!selected.is_empty(), "select at least one file"),
            _ => ensure!(
                selected.is_empty(),
                "this decision does not accept a selection"
            ),
        }
        let tx = self.connection.transaction()?;
        tx.execute(
            "INSERT INTO review_decision(cycle_id,fingerprint,decision_json,snapshot_json) VALUES(?1,?2,?3,?4)",
            params![cycle,input.fingerprint,serde_json::to_string(input)?,serde_json::to_string(&group)?],
        )?;
        tx.commit()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use iarchive_core::{FileFact, FileKind};

    #[test]
    fn decisions_follow_recorded_snapshots() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.sqlite");
        let mut store = Store::open(&path).unwrap();
        store.import("x", &dir.path().join("synthetic")).unwrap();
        let mut fact = FileFact {
            relative_path: "photo.jpg".into(),
            path_key: "photo.jpg".into(),
            kind: FileKind::Image,
            byte_count: 3,
            mtime_ns: "1".into(),
            sha256: Some("a".repeat(64)),
            error: None,
            warnings: vec![],
        };
        let scan = store.begin_scan("x").unwrap();
        store.save_file("x", scan, &fact).unwrap();
        store.finish_scan("x", scan, true).unwrap();
        store
            .candidate("x", "dark", "Synthetic dark candidate", &[1])
            .unwrap();
        store
            .candidate("x", "similar", "Overlapping synthetic group", &[1])
            .unwrap();
        let fingerprint = store.review_snapshot("x").unwrap().groups[0]
            .fingerprint
            .clone();
        let mut input = ReviewInput {
            fingerprint: fingerprint.clone(),
            action: ReviewAction::FlagSelected,
            selected: vec![1],
        };
        store.record_review("x", &input).unwrap();
        drop(store);
        let mut store = Store::open(&path).unwrap();
        let snapshot = store.review_snapshot("x").unwrap();
        assert_eq!(
            snapshot.groups[0].decision.as_ref().unwrap().selected,
            vec![1]
        );
        assert_eq!(snapshot.decision_history_count, 1);
        assert!(snapshot.groups[1].decision.is_none());
        store
            .record_review(
                "x",
                &ReviewInput {
                    fingerprint: snapshot.groups[1].fingerprint.clone(),
                    action: ReviewAction::KeepAll,
                    selected: vec![],
                },
            )
            .unwrap();
        for (action, selected) in [
            (ReviewAction::FlagSelected, vec![]),
            (ReviewAction::FlagSelected, vec![1, 1]),
            (ReviewAction::FlagSelected, vec![999]),
            (ReviewAction::KeepAll, vec![1]),
        ] {
            assert!(
                store
                    .record_review(
                        "x",
                        &ReviewInput {
                            fingerprint: fingerprint.clone(),
                            action,
                            selected
                        }
                    )
                    .is_err()
            );
        }
        store.import("other", &dir.path().join("other")).unwrap();
        assert!(store.record_review("other", &input).is_err());
        for action in [
            ReviewAction::KeepAll,
            ReviewAction::Defer,
            ReviewAction::Reset,
        ] {
            input.action = action.clone();
            input.selected.clear();
            store.record_review("x", &input).unwrap();
            let snapshot = store.review_snapshot("x").unwrap();
            assert_eq!(
                snapshot.groups[0]
                    .decision
                    .as_ref()
                    .map(|d| d.action.clone()),
                if action == ReviewAction::Reset {
                    None
                } else {
                    Some(action)
                }
            );
            assert_eq!(
                snapshot.groups[1].decision.as_ref().unwrap().action,
                ReviewAction::KeepAll
            );
        }
        let scan = store.begin_scan("x").unwrap();
        fact.sha256 = Some("b".repeat(64));
        store.save_file("x", scan, &fact).unwrap();
        store.finish_scan("x", scan, true).unwrap();
        // SQLite can reuse a deleted group ID; old decisions still cannot carry forward.
        store
            .candidate("x", "dark", "Synthetic dark candidate", &[1])
            .unwrap();
        let snapshot = store.review_snapshot("x").unwrap();
        assert_ne!(snapshot.groups[0].fingerprint, fingerprint);
        assert!(snapshot.groups[0].decision.is_none());
        assert_eq!(snapshot.decision_history_count, 5);
        assert!(store.record_review("x", &input).is_err());
        assert_eq!(store.status("x").unwrap().stage, "INDEXED");
        let old: String = store
            .connection
            .query_row(
                "SELECT snapshot_json FROM review_decision ORDER BY id LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(old.contains(&"a".repeat(64)));
    }
}
