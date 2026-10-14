//! Read-only observations of review flags, never an executable move plan.
use crate::{Cli, emit};
use anyhow::Result;
use iarchive_fs::replica::{self, Binding, Summary};
use iarchive_store::{ReviewAction, ReviewFile, Store};
use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Serialize)]
struct Flag<'a> {
    file_id: i64,
    cycle: &'a str,
    relative_path: &'a str,
    sha256: &'a Option<String>,
    review_groups: Vec<&'a str>,
    blockers: Vec<&'static str>,
}

pub fn run(cli: &Cli, store: &Store, cycle: &str) -> Result<bool> {
    // Hold one database snapshot for decisions and their recorded file facts.
    let _snapshot = store.connection.unchecked_transaction()?;
    let view = store.review_snapshot(cycle)?;
    let kept = store.kept_review_files()?;
    let mut selected: BTreeMap<i64, (&ReviewFile, Vec<&str>)> = BTreeMap::new();
    for group in &view.groups {
        if let Some(decision) = &group.decision {
            if decision.action == ReviewAction::FlagSelected {
                for file in &group.members {
                    if decision.selected.contains(&file.id) {
                        selected
                            .entry(file.id)
                            .or_insert_with(|| (file, vec![]))
                            .1
                            .push(&group.fingerprint);
                    }
                }
            }
        }
    }
    let mut blockers = vec![];
    if !view.status.inventory_complete
        || view.status.file_errors > 0
        || view.status.missing_files > 0
        || view.status.scan_issues > 0
    {
        blockers.push("Recorded inventory is incomplete or contains findings".to_owned());
    }
    let mut verification = Summary::new(true);
    let binding: Option<Binding> = store
        .replica_binding(cycle)?
        .map(|json| serde_json::from_str(&json))
        .transpose()?;
    if let Some(binding) = &binding {
        let source = store.root(cycle)?;
        verification = replica::verify_frozen(&source, binding);
        if !verification.complete {
            blockers.push("Fresh source and secondary verification failed".to_owned());
        }
    } else {
        blockers.push("No frozen secondary replica binding; replicate first".to_owned());
    }
    if verification.independent_physical_drives != "VERIFIED BY OS TOPOLOGY" {
        blockers.push("Independent physical backups are NOT VERIFIED".to_owned());
    }
    let flags: Vec<_> = selected
        .into_iter()
        .map(|(id, (file, groups))| {
            let mut reasons = vec![];
            if kept.contains(&id) {
                reasons.push("Conflicting Keep decision protects this file");
            }
            if file.cycle != cycle {
                reasons.push("File belongs to another cycle; review it separately");
            }
            if !file.present
                || file.file_error.is_some()
                || file.decode_error.is_some()
                || file.sha256.is_none()
                || file.kind != "image"
                || file.features.is_none()
            {
                reasons.push(
                    "File is missing, unreadable, unsupported, or lacks successful image analysis",
                );
            }
            // There is no proven relationship inventory yet, including proof of
            // standalone status. Exact hashes and filenames cannot establish it.
            reasons.push("Associated file relationships are unknown; no safe move group is proven");
            if let Some(binding) = &binding {
                if !binding.files.iter().any(|f| {
                    file.cycle == cycle
                        && f.relative_path == file.relative_path
                        && f.sha256 == file.sha256
                        && f.byte_count == file.byte_count
                        && f.mtime_ns == file.mtime_ns
                        && f.error.is_none()
                }) {
                    reasons.push("Reviewed file facts do not match the frozen backup snapshot");
                }
            }
            Flag {
                file_id: id,
                cycle: &file.cycle,
                relative_path: &file.relative_path,
                sha256: &file.sha256,
                review_groups: groups,
                blockers: reasons,
            }
        })
        .collect();
    let blocked = !blockers.is_empty() || flags.iter().any(|f| !f.blockers.is_empty());
    emit(
        cli,
        &serde_json::json!({
            "operation":"quarantine_preview", "dry_run":true, "cycle":cycle,
            "will_write_media":false, "will_write_state":false,
            "blocked":blocked, "flagged_count":flags.len(), "flags":flags,
            "blockers":blockers, "verification":verification,
            "note":"Read-only observations, not approval to move files. Quarantine execution and restore are unavailable. Cloud coverage is NOT VERIFIED."
        }),
    )?;
    Ok(!blocked)
}
