use crate::{Cli, emit};
use anyhow::{Context, Result, ensure};
use iarchive_core::{FileFact, absolute_destination, ensure_separate, relationships::Group};
use iarchive_fs::{
    quarantine::{self, Plan},
    replica::{self, Binding, Summary},
    volume,
};
use iarchive_store::{ReviewAction, Store};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

#[derive(Serialize)]
struct Preview {
    operation: &'static str,
    dry_run: bool,
    cycle: String,
    quarantine_root: PathBuf,
    confirmation: String,
    flagged_count: usize,
    flags: Vec<serde_json::Value>,
    files: Vec<FileFact>,
    groups: Vec<Group>,
    blockers: Vec<String>,
    verification: Summary,
    blocked: bool,
    will_write_media: bool,
    will_write_state: bool,
    note: &'static str,
    #[serde(skip)]
    binding: Option<Binding>,
}

pub fn relocations(store: &Store, cycle: &str) -> Result<Vec<replica::Relocation>> {
    store
        .active_jobs(cycle)?
        .into_iter()
        .map(|job| {
            let plan: Plan = serde_json::from_value(job.plan)?;
            plan.check_journal()?;
            Ok(plan.relocation())
        })
        .collect()
}

fn destination(
    store: &Store,
    state: &Path,
    cycle: &str,
    explicit: Option<&Path>,
) -> Result<PathBuf> {
    let source = store.root(cycle)?;
    let path = explicit.map(Path::to_owned).unwrap_or_else(|| {
        source
            .parent()
            .unwrap_or(&source)
            .join("iarchive-quarantine")
            .join(cycle)
    });
    crate::review::reject_links(&path)?;
    let path = absolute_destination(&path)?;
    ensure_separate(&path, state)?;
    for archived in store.cycles()? {
        ensure_separate(&path, Path::new(&archived.source_root))?;
        if let Some(binding) = store.replica_binding(&archived.cycle)? {
            let binding: Binding = serde_json::from_str(&binding)?;
            let secondary = absolute_destination(&binding.secondary_root)?;
            ensure!(
                !path.starts_with(&secondary) && !secondary.starts_with(&path),
                "quarantine overlaps secondary media"
            );
        }
    }
    let mut parent = path.as_path();
    while !parent.exists() {
        parent = parent.parent().context("destination ancestor missing")?;
    }
    ensure!(
        volume::identity(parent)?.volume_id == volume::identity(&source)?.volume_id,
        "quarantine directory must be on the primary volume"
    );
    Ok(path)
}

fn preview(
    store: &Store,
    state: &Path,
    cycle: &str,
    root: Option<&Path>,
    device_ack: bool,
) -> Result<Preview> {
    let _snapshot = store.connection.unchecked_transaction()?;
    let view = store.review_snapshot(cycle)?;
    let kept = store.kept_review_files()?;
    let mut selected = BTreeMap::new();
    for group in &view.groups {
        if let Some(decision) = &group.decision {
            if decision.action == ReviewAction::FlagSelected {
                for file in &group.members {
                    if decision.selected.contains(&file.id) {
                        selected
                            .entry(file.id)
                            .or_insert_with(|| (file, vec![]))
                            .1
                            .push(group.fingerprint.clone());
                    }
                }
            }
        }
    }
    let quarantine_root = destination(store, state, cycle, root)?;
    let mut blockers = vec![];
    if !store.active_jobs(cycle)?.is_empty() {
        blockers.push("Resume or restore the existing quarantine job first".into());
    }
    if !view.status.inventory_complete
        || view.status.file_errors > 0
        || view.status.missing_files > 0
        || view.status.scan_issues > 0
    {
        blockers.push("Recorded inventory is incomplete or contains findings".into());
    }
    let binding: Option<Binding> = store
        .replica_binding(cycle)?
        .map(|v| serde_json::from_str(&v))
        .transpose()?;
    let mut verification = Summary::new(true);
    if let Some(binding) = &binding {
        verification =
            replica::verify_relocated(&store.root(cycle)?, binding, &relocations(store, cycle)?);
        if !verification.complete {
            blockers.push("Fresh source and secondary verification failed".into());
        }
    } else {
        blockers.push("No frozen secondary replica binding; replicate first".into());
    }
    if !device_ack && verification.independent_physical_drives != "VERIFIED BY OS TOPOLOGY" {
        blockers.push("Independent physical backups are NOT VERIFIED".into());
    }
    let relationships = store.relationships(cycle)?;
    let mut groups = vec![];
    let mut flags = vec![];
    let mut seen = BTreeSet::new();
    for (id, (file, review_groups)) in &selected {
        let mut reasons = vec![];
        if kept.contains(id) {
            reasons.push("Conflicting Keep decision protects this file".into());
        }
        if file.cycle != cycle {
            reasons.push("File belongs to another cycle; review it separately".into());
        }
        if !file.present
            || file.file_error.is_some()
            || file.decode_error.is_some()
            || file.sha256.is_none()
            || file.kind != "image"
            || file.features.is_none()
        {
            reasons.push(
                "File is missing, unreadable, unsupported, or lacks successful image analysis"
                    .into(),
            );
        }
        if let Some(group) = relationships
            .iter()
            .find(|g| g.members.iter().any(|m| m.id == *id))
        {
            reasons.extend(group.blockers.clone());
            if group.members.iter().any(|m| kept.contains(&m.id)) {
                reasons.push("Conflicting Keep decision protects an associated member".into());
            }
            for member in &group.members {
                if member.fact.kind == iarchive_core::FileKind::Image {
                    let good = member
                        .fact
                        .sha256
                        .as_deref()
                        .map(|hash| {
                            store.analysis(member.id, hash, iarchive_core::ALGORITHM_VERSION)
                        })
                        .transpose()?
                        .flatten()
                        .is_some();
                    if !good {
                        reasons.push("Associated image lacks successful image analysis".into());
                    }
                }
                if let Some(binding) = &binding {
                    if !binding.files.contains(&member.fact) {
                        reasons.push(
                            "Reviewed file facts do not match the frozen backup snapshot".into(),
                        );
                    }
                }
            }
            if seen.insert(group.fingerprint.clone()) {
                groups.push(group.clone());
            }
        } else {
            reasons.push("Associated file relationships are unknown; no metadata identity".into());
        }
        if let Some(binding) = &binding {
            if !binding.files.iter().any(|f| {
                file.cycle == cycle
                    && f.relative_path == file.relative_path
                    && f.sha256 == file.sha256
                    && f.byte_count == file.byte_count
                    && f.mtime_ns == file.mtime_ns
                    && f.error.is_none()
            }) {
                reasons.push("Reviewed file facts do not match the frozen backup snapshot".into());
            }
        }
        reasons.sort();
        reasons.dedup();
        flags.push(serde_json::json!({"file_id": id, "cycle": file.cycle, "relative_path": file.relative_path,
            "review_groups": review_groups, "blockers": reasons}));
    }
    let files: Vec<_> = groups
        .iter()
        .flat_map(|g| g.members.iter().map(|m| m.fact.clone()))
        .collect();
    let confirmation = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&serde_json::json!({
            "version": 1, "cycle": cycle, "source": store.root(cycle)?, "binding": binding, "quarantine_root": quarantine_root,
            "groups": groups, "review": view.groups, "kept": kept, "allow_unverified_devices": device_ack
        }))?)
    );
    let blocked = !blockers.is_empty()
        || flags
            .iter()
            .any(|f| !f["blockers"].as_array().unwrap().is_empty());
    Ok(Preview {
        operation: "quarantine_preview",
        dry_run: true,
        cycle: cycle.into(),
        quarantine_root,
        confirmation,
        flagged_count: selected.len(),
        flags,
        files,
        groups,
        blockers,
        verification,
        blocked,
        will_write_media: false,
        will_write_state: false,
        binding,
        note: "Confirmation binds the exact reviewed logical groups, paths and frozen bytes. No permanent deletion. Cloud coverage is NOT VERIFIED.",
    })
}

#[allow(clippy::too_many_arguments)]
pub fn run(
    cli: &Cli,
    store: &Store,
    state: &Path,
    cycle: &str,
    root: Option<&Path>,
    confirm: Option<&str>,
    resume: Option<&str>,
    device_ack: bool,
) -> Result<bool> {
    if let Some(id) = resume {
        return resume_job(cli, store, id, Some(cycle), confirm, false, device_ack);
    }
    let preview = preview(store, state, cycle, root, device_ack)?;
    if cli.dry_run {
        emit(cli, &preview)?;
        return Ok(!preview.blocked);
    }
    ensure!(
        !preview.blocked,
        "quarantine plan is blocked; inspect --dry-run findings"
    );
    ensure!(
        confirm == Some(preview.confirmation.as_str()),
        "confirmation differs from the fresh quarantine plan; preview again"
    );
    ensure!(
        !preview.files.is_empty(),
        "no flagged logical groups to quarantine"
    );
    let source = store.root(cycle)?;
    let reserve = serde_json::to_vec(&preview.files)?.len() as u64 * 4 + 16 * 1024 * 1024;
    ensure!(
        fs2::available_space(&source)? > reserve && fs2::available_space(state)? > reserve,
        "insufficient free space for durable move journals"
    );
    let mut random = [0u8; 12];
    getrandom::fill(&mut random).map_err(|_| anyhow::anyhow!("cannot create quarantine ID"))?;
    let id = random
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let parent = quarantine::create_directory(&preview.quarantine_root)?;
    let job = parent.join(&id);
    std::fs::create_dir(&job)?;
    let media = quarantine::create_directory(&job.join("media"))?;
    let plan = Plan {
        id,
        cycle: cycle.into(),
        confirmation: preview.confirmation,
        source,
        quarantine: media.clone(),
        quarantine_identity: volume::identity(&media)?,
        binding: preview.binding.context("binding missing")?,
        files: preview.files,
    };
    quarantine::save_plan(&plan)?;
    store.begin_local_job(&plan.id, cycle, &media, &serde_json::to_value(&plan)?)?;
    execute(cli, store, &plan, false, device_ack)
}

pub fn restore(cli: &Cli, store: &Store, id: &str) -> Result<bool> {
    resume_job(cli, store, id, None, None, true, false)
}

fn resume_job(
    cli: &Cli,
    store: &Store,
    id: &str,
    cycle: Option<&str>,
    confirm: Option<&str>,
    restore: bool,
    device_ack: bool,
) -> Result<bool> {
    let job = store.local_job(id)?;
    let plan: Plan = serde_json::from_value(job.plan)?;
    ensure!(
        cycle.is_none_or(|c| c == job.cycle),
        "quarantine job belongs to another cycle"
    );
    ensure!(
        plan.id == id && plan.cycle == job.cycle,
        "quarantine journal identity mismatch"
    );
    let positions = quarantine::positions(&plan)?;
    if cli.dry_run {
        emit(
            cli,
            &serde_json::json!({"dry_run": true, "operation": if restore { "restore_preview" } else { "quarantine_resume_preview" },
            "quarantine_id": id, "cycle": plan.cycle, "state": job.state, "confirmation": plan.confirmation,
            "files": plan.files, "quarantined_files": positions.iter().filter(|q| **q).count(), "will_write_media": false}),
        )?;
        return Ok(true);
    }
    if !restore {
        ensure!(
            job.state != "RESTORED" && job.state != "RESTORING",
            "job is restoring or already restored"
        );
        ensure!(
            confirm == Some(plan.confirmation.as_str()),
            "confirmation does not match the immutable quarantine plan"
        );
    }
    execute(cli, store, &plan, restore, device_ack)
}

fn execute(cli: &Cli, store: &Store, plan: &Plan, restore: bool, device_ack: bool) -> Result<bool> {
    let operation = if restore { "restore" } else { "quarantine" };
    let result = (|| -> Result<()> {
        if !restore {
            let fresh = replica::verify_relocated(
                &plan.source,
                &plan.binding,
                &relocations(store, &plan.cycle)?,
            );
            ensure!(
                fresh.complete,
                "fresh raw backup check blocks quarantine: {}",
                fresh.issues.join("; ")
            );
            ensure!(
                device_ack || fresh.independent_physical_drives == "VERIFIED BY OS TOPOLOGY",
                "physical backups are NOT VERIFIED; explicit byte-only acknowledgment is required"
            );
            let kept = store.kept_review_files()?;
            ensure!(
                !store
                    .file_facts(&plan.cycle)?
                    .iter()
                    .any(|(id, f)| kept.contains(id) && plan.files.contains(f)),
                "a current Keep decision blocks an associated move"
            );
        }
        let state = if restore { "RESTORING" } else { "MOVING" };
        store.set_job_state(&plan.id, state, None)?;
        quarantine::execute(plan, restore, &mut |count| {
            quarantine::progress(plan, state, count)
        })?;
        let final_state = if restore { "RESTORED" } else { "QUARANTINED" };
        if !restore {
            let fresh = replica::verify_relocated(
                &plan.source,
                &plan.binding,
                &relocations(store, &plan.cycle)?,
            );
            ensure!(
                fresh.complete,
                "verification after moving found changes: {}",
                fresh.issues.join("; ")
            );
        }
        quarantine::progress(plan, final_state, plan.files.len())?;
        store.set_job_state(&plan.id, final_state, None)?;
        Ok(())
    })();
    if let Err(error) = result {
        store.set_job_state(&plan.id, "INCOMPLETE", Some(&format!("{error:#}")))?;
        emit(
            cli,
            &serde_json::json!({"operation": operation, "complete": false, "quarantine_id": plan.id,
            "cycle": plan.cycle, "error": format!("{error:#}"), "note": "Bytes retained. Restore or resume this immutable job; do not reindex."}),
        )?;
        return Ok(false);
    }
    emit(
        cli,
        &serde_json::json!({"operation": operation, "complete": true, "quarantine_id": plan.id,
        "cycle": plan.cycle, "file_count": plan.files.len(), "journal": plan.journal(),
        "secondary_media_changed": false, "cloud_coverage": "NOT VERIFIED"}),
    )?;
    Ok(true)
}
