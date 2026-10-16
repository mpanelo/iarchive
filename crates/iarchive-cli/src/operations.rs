use crate::{Cli, emit};
use anyhow::{Context, Result, ensure};
use iarchive_core::{
    ALGORITHM_VERSION, ImageAnalyzer, ImageFeatures, StorageProvider, absolute_destination,
    ensure_separate, resolve_input,
};
use iarchive_fs::{LocalStorage, hash_file};
use iarchive_store::Store;
use std::path::Path;

pub fn doctor(
    cli: &Cli,
    state: &Path,
    primary: Option<&Path>,
    secondary: Option<&Path>,
    version: &str,
) -> Result<bool> {
    use crate::worker::{default_project, python, run_bounded};
    use std::{process::Command, time::Duration};
    let temporary_root = std::env::temp_dir().canonicalize()?;
    for root in primary.into_iter().chain(secondary) {
        ensure!(
            !temporary_root.starts_with(absolute_destination(root)?),
            "system temp directory must be outside archive roots"
        );
    }
    let paths:Vec<_>=[("primary",primary),("secondary",secondary)].into_iter().map(|(label,path)| {
        let available=path.is_some_and(Path::is_dir);
        let space=path.filter(|_|available).and_then(|p|fs2::available_space(p).ok());
        serde_json::json!({"location":label,"path":path,"available":available,"free_bytes":space,"physical_device_identity":"NOT VERIFIED","writable":path.and_then(|p|std::fs::metadata(p).ok()).map(|m|!m.permissions().readonly()),"permission_note":"Metadata only; no archive write probe performed"})
    }).collect();
    let mut checks = Vec::new();
    for (name, args) in [("uv", vec!["--version"]), ("ffprobe", vec!["-version"])] {
        let result = run_bounded(Command::new(name).args(args), None, Duration::from_secs(5));
        checks.push(serde_json::json!({"capability":name,"available":result.is_ok()}));
    }
    let version_result = run_bounded(
        Command::new("icloudpd").arg("--version"),
        None,
        Duration::from_secs(5),
    );
    let help_result = run_bounded(
        Command::new("icloudpd").arg("--help"),
        None,
        Duration::from_secs(5),
    );
    let icloudpd = serde_json::json!({"available_on_path":version_result.is_ok(),"pinned_version_matches":version_result.as_ref().is_ok_and(|r|String::from_utf8_lossy(r).contains(version)),"required_read_only_options":help_result.as_ref().is_ok_and(|r|{let help=String::from_utf8_lossy(r);["--size","--live-photo-size","--library"].iter().all(|option|help.contains(option))}),"uvx_installation":"NOT VERIFIED; doctor never downloads or authenticates"});
    let project = default_project();
    let worker_allowed = primary.is_none_or(|root| ensure_separate(&project, root).is_ok())
        && secondary.is_none_or(|root| !root.is_dir() || ensure_separate(&project, root).is_ok());
    let worker = worker_allowed
        && run_bounded(
            Command::new(python(&project))
                .args(["-B", "-m", "iarchive_worker", "--probe"])
                .current_dir(&project)
                .env_remove("PYTHONPATH"),
            None,
            Duration::from_secs(10),
        )
        .is_ok_and(|bytes| {
            serde_json::from_slice::<serde_json::Value>(&bytes).is_ok_and(|v| {
                v["protocol_version"] == 1 && v["algorithm_version"] == ALGORITHM_VERSION
            })
        });
    emit(
        cli,
        &serde_json::json!({"state_dir":state,"paths":paths,"tools":checks,"icloudpd_pin_candidate":version,"icloudpd":icloudpd,"image_worker":{"available":worker,"project":project},"physical_drives":"NOT VERIFIED; filesystem IDs cannot prove separate physical disks","local_replica":"NOT VERIFIED","cloud_coverage":"NOT VERIFIED","note":"Optional worker/cloud/video capabilities do not block local raw inventory"}),
    )?;
    Ok(primary.is_none_or(Path::is_dir) && secondary.is_none_or(Path::is_dir))
}
pub fn index(cli: &Cli, store: &mut Store, cycle: &str, deep: bool) -> Result<bool> {
    let summary = scan(store, cycle, deep)?;
    let complete = store.status(cycle)?.inventory_complete;
    emit(cli, &summary)?;
    Ok(complete)
}

fn scan(store: &mut Store, cycle: &str, deep: bool) -> Result<serde_json::Value> {
    let root = store.root(cycle)?;
    let id = store.begin_scan(cycle)?;
    let mut hashed = 0u64;
    let mut cached = 0u64;
    let mut visited = 0u64;
    tracing::info!("starting read-only inventory; prior completion invalidated");
    let result = (|| -> Result<()> {
        ensure!(
            root.is_dir(),
            "source archive unavailable; no completion may be claimed"
        );
        ensure!(
            root.canonicalize()? == root,
            "source mount/path identity changed"
        );
        LocalStorage.visit(&root, &mut |mut fact| {
            if fact.error.is_none() {
                let previous = store.cached(cycle, &fact.relative_path)?;
                if !deep
                    && previous.as_ref().is_some_and(|p| {
                        p.byte_count == fact.byte_count
                            && p.mtime_ns == fact.mtime_ns
                            && p.sha256.is_some()
                            && p.error.is_none()
                    })
                {
                    fact.sha256 = previous.unwrap().sha256;
                    cached += 1;
                } else {
                    match resolve_input(&root, &fact.relative_path).and_then(|p| hash_file(&p)) {
                        Ok(hash) => {
                            fact.sha256 = Some(hash);
                            hashed += 1;
                        }
                        Err(e) => {
                            fact.error = Some(format!("Hash unreadable or unstable: {e:#}"));
                        }
                    }
                }
            }
            store.save_file(cycle, id, &fact)?;
            visited += 1;
            if visited % 1000 == 0 {
                tracing::info!(
                    files = visited,
                    hashes = hashed,
                    cache_hits = cached,
                    "inventory progress"
                );
            }
            Ok(())
        })
    })();
    if let Err(e) = &result {
        store.scan_issue(id, "<traversal>", &format!("{e:#}"))?;
    }
    if result.is_ok() {
        store.mark_absent(cycle, id)?;
        for group in store.case_collisions(cycle)? {
            store.scan_issue(id,&group.join(" | "),"Case/Unicode-normalization collision; portable replica requires explicit resolution")?;
        }
    }
    store.finish_scan(cycle, id, result.is_ok())?;
    let exact = store.exact_groups(cycle)?;
    Ok(
        serde_json::json!({"status":store.status(cycle)?,"hashes_computed":hashed,"hash_cache_hits":cached,"hash_cache_note":"path + size + mtime cache; not deep two-drive verification","exact_duplicate_groups":exact.into_iter().map(|members| members.into_iter().map(|(_,cycle,path)|serde_json::json!({"cycle":cycle,"relative_path":path})).collect::<Vec<_>>()).collect::<Vec<_>>(),"case_collisions":store.case_collisions(cycle)?,"scan_error":result.err().map(|e|format!("{e:#}"))}),
    )
}

pub fn validate_export(output: &Path, store: &Store, state: &Path) -> Result<()> {
    let output = absolute_destination(output)?;
    ensure!(
        !output.starts_with(state),
        "explicit manifest exports must be outside app state"
    );
    for root in store.roots()? {
        let root = absolute_destination(&root)?;
        ensure!(
            !output.starts_with(&root),
            "manifest cannot be written inside a media root"
        );
    }
    if let Some(config) = crate::load_config(state)? {
        for root in std::iter::once(config.primary).chain(config.secondary) {
            ensure!(
                !output.starts_with(absolute_destination(&root)?),
                "manifest cannot be written inside a configured media root"
            );
        }
    }
    ensure!(
        !output.exists(),
        "output already exists; select a new filename (exports never overwrite)"
    );
    Ok(())
}
pub fn export(cli: &Cli, store: &Store, cycle: &str, output: &Path, state: &Path) -> Result<bool> {
    use std::io::Write;
    validate_export(output, store, state)?;
    let output = absolute_destination(output)?;
    let parent = output.parent().context("output has no parent directory")?;
    ensure!(
        parent.is_dir(),
        "create the export directory outside media before exporting"
    );
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    let status = store.status(cycle)?;
    serde_json::to_writer(
        temp.as_file_mut(),
        &serde_json::json!({"schema_version":1,"record_type":"archive_summary","archive_id":cycle,"status":status,"snapshot_type":"local_inventory_only"}),
    )?;
    temp.write_all(b"\n")?;
    store.visit_manifest(cycle, &mut |value| {
        serde_json::to_writer(temp.as_file_mut(), &value)?;
        temp.write_all(b"\n")?;
        Ok(())
    })?;
    temp.as_file().sync_all()?;
    temp.persist_noclobber(&output)?;
    emit(cli, &serde_json::json!({"manifest":output,"status":status}))?;
    Ok(status.inventory_complete)
}
#[allow(clippy::too_many_arguments)]
pub fn audit(
    cli: &Cli,
    store: &mut Store,
    cycle: &str,
    state: &Path,
    categories: &str,
    worker_project: Option<&Path>,
    timeout: u64,
    cache_limit_mib: u64,
) -> Result<bool> {
    use crate::worker::{UvAnalyzer, default_project, validate_features};
    use iarchive_core::similarity::HashTree;
    use std::time::Duration;
    ensure!(
        timeout > 0 && timeout <= 3600,
        "timeout must be 1–3600 seconds"
    );
    let categories: std::collections::BTreeSet<_> = categories.split(',').collect();
    ensure!(
        !categories.is_empty()
            && categories
                .iter()
                .all(|c| matches!(*c, "exact" | "similar" | "dark")),
        "categories must be exact,similar,dark"
    );
    let root = store.root(cycle)?;
    let cache_limit = cache_limit_mib
        .checked_mul(1024 * 1024)
        .context("cache limit is too large")?;
    ensure!(cache_limit > 0, "cache limit must be positive");
    let cache = state.join("thumbnails");
    ensure_separate(&cache, &root)?;
    let project = worker_project
        .map(Path::to_owned)
        .unwrap_or_else(default_project);
    let project = absolute_destination(&project)?;
    let visual = categories.contains("similar") || categories.contains("dark");
    if visual {
        for registered in store.roots()? {
            ensure_separate(&project, &registered)?;
        }
        ensure!(
            crate::worker::python(&project).is_file(),
            "worker not installed; run uv sync --project image-worker --group dev first (exact-only audit needs no Python)"
        );
    }
    let _ = scan(store, cycle, false)?;
    store.begin_audit(cycle)?;
    let mut analyzer = UvAnalyzer {
        project,
        timeout: Duration::from_secs(timeout),
    };
    let mut analyzed = Vec::<(i64, ImageFeatures)>::new();
    let mut hits = 0;
    let mut decode_errors = 0;
    let mut cache_paths = std::collections::HashSet::new();
    let mut cache_bytes = 0u64;
    if cache.exists() {
        for entry in std::fs::read_dir(&cache)? {
            let path = entry?.path();
            let metadata = std::fs::symlink_metadata(&path)?;
            ensure!(
                !iarchive_core::is_link(&metadata) && metadata.is_file(),
                "unexpected link/directory in thumbnail cache"
            );
            cache_bytes = cache_bytes
                .checked_add(metadata.len())
                .context("cache size overflow")?;
            cache_paths.insert(path.canonicalize()?);
        }
    }
    if visual {
        for (id, fact) in store.image_files(cycle)? {
            let hash = fact
                .sha256
                .as_deref()
                .context("image is missing its hash")?;
            let result = (|| -> Result<ImageFeatures> {
                let input = resolve_input(&root, &fact.relative_path)?;
                ensure!(
                    hash_file(&input)? == hash,
                    "source changed since inventory; reindex before audit"
                );
                let features = if let Some(cached) = store.analysis(id, hash, ALGORITHM_VERSION)? {
                    if validate_features(&cached, &cache).is_ok() {
                        hits += 1;
                        cached
                    } else {
                        ensure!(
                            cache_bytes.saturating_add(1024 * 1024) <= cache_limit,
                            "thumbnail cache capacity reached; choose a larger --cache-limit-mib"
                        );
                        analyzer.analyze(id, &input, &root, &cache)?
                    }
                } else {
                    ensure!(
                        cache_bytes.saturating_add(1024 * 1024) <= cache_limit,
                        "thumbnail cache capacity reached; choose a larger --cache-limit-mib"
                    );
                    analyzer.analyze(id, &input, &root, &cache)?
                };
                ensure!(
                    hash_file(&input)? == hash,
                    "source changed during image analysis"
                );
                let preview = Path::new(&features.thumbnail).canonicalize()?;
                if cache_paths.insert(preview.clone()) {
                    cache_bytes = cache_bytes
                        .checked_add(std::fs::metadata(preview)?.len())
                        .context("cache size overflow")?;
                }
                ensure!(
                    cache_bytes <= cache_limit,
                    "thumbnail cache capacity exceeded"
                );
                Ok(features)
            })();
            store.save_analysis(id, hash, &result, ALGORITHM_VERSION)?;
            match result {
                Ok(features) => analyzed.push((id, features)),
                Err(_) => decode_errors += 1,
            };
        }
    }
    if categories.contains("exact") {
        for group in store.exact_groups(cycle)? {
            store.candidate(
                cycle,
                "exact",
                "Identical SHA-256 bytes; associated variants remain separate. Human review only.",
                &group.iter().map(|(id, _, _)| *id).collect::<Vec<_>>(),
            )?;
        }
    }
    if categories.contains("dark") {
        for (id, f) in &analyzed {
            if f.dark_candidate_score >= 0.65 {
                store.candidate(cycle, "dark", &f.dark_reason, &[*id])?;
            }
        }
    }
    if categories.contains("similar") {
        let mut tree = HashTree::default();
        for (i, (id, f)) in analyzed.iter().enumerate() {
            let hash = u64::from_str_radix(&f.phash, 16)?;
            if f.edge_energy < 1.0 || f.dark_candidate_score >= 0.65 {
                continue;
            }
            for j in tree.query_limited(hash, 6, 64) {
                let (other_id, other) = &analyzed[j];
                let aspect = f.width as f64 / f.height as f64;
                let other_aspect = other.width as f64 / other.height as f64;
                if (aspect / other_aspect - 1.0).abs() <= 0.10 {
                    store.candidate(cycle,"similar",&format!("64-bit pHash distance {}; similar aspect ratio; no reliable capture-time mapping. Pairwise suggestion; human review only.",(hash^u64::from_str_radix(&other.phash,16)?).count_ones()),&[*other_id,*id])?;
                    break;
                }
            }
            tree.insert(hash, i);
        }
    }
    let status = store.status(cycle)?;
    let pending_images = store.image_files(cycle)?.len() != analyzed.len();
    let complete = status.inventory_complete
        && visual
        && decode_errors == 0
        && !pending_images
        && status.unsupported_visual_files == 0;
    store.finish_audit(cycle, complete)?;
    emit(
        cli,
        &serde_json::json!({"status":store.status(cycle)?,"analysis_cache_hits":hits,"decoded_images":analyzed.len()-hits,"analyzed_images":analyzed.len(),"decode_errors":decode_errors,"thumbnail_cache_bytes":cache_bytes,"thumbnail_cache_limit_bytes":cache_limit,"similarity_search":"BK-tree; at most 64 neighbors and one pair suggestion per image; candidate list is not exhaustive","categories":categories,"candidates":store.candidates(cycle)?,"findings":store.findings(cycle)?,"review_only":true,"note":"No files moved or deleted; videos, RAW and unknown media are not visually audited"}),
    )?;
    Ok(if visual {
        complete
    } else {
        status.inventory_complete
    })
}
pub fn report(cli: &Cli, store: &Store, cycle: &str) -> Result<bool> {
    let images=store.image_files(cycle)?.into_iter().map(|(id,fact)| {
        Ok(serde_json::json!({"relative_path":fact.relative_path,"features":store.analysis(id,fact.sha256.as_deref().unwrap(),ALGORITHM_VERSION)?}))
    }).collect::<Result<Vec<_>>>()?;
    emit(
        cli,
        &serde_json::json!({"status":store.status(cycle)?,"candidates":store.candidates(cycle)?,"findings":store.findings(cycle)?,"images":images,"case_collisions":store.case_collisions(cycle)?,"review_only":true}),
    )?;
    Ok(true)
}
