use crate::{Cli, Config, emit};
use anyhow::{Context, Result, ensure};
use iarchive_core::absolute_destination;
use iarchive_fs::{
    hash_file,
    replica::{self, Binding, Options, Summary},
    volume,
};
use iarchive_store::Store;
use serde_json::{Value, json};
use std::{
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(clap::Args)]
pub struct Args {
    #[arg(long)]
    pub cycle: String,
    #[arg(
        long,
        help = "Existing secondary media directory; defaults to configured secondary"
    )]
    pub secondary: Option<PathBuf>,
    #[arg(
        long,
        help = "Existing artifact directory outside media on the primary volume"
    )]
    pub primary_artifacts: PathBuf,
    #[arg(
        long,
        help = "Existing artifact directory outside media on the secondary volume"
    )]
    pub secondary_artifacts: PathBuf,
    #[arg(
        long,
        help = "Acknowledge byte-only verification when independent physical drives cannot be proved"
    )]
    pub allow_unverified_devices: bool,
}

fn absolute_hint(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    // Normalize Windows verbatim prefixes only after rejecting linked ancestors.
    Ok(volume::checked_directory(&absolute).unwrap_or(absolute))
}

pub fn run(
    cli: &Cli,
    store: &mut Store,
    state: &Path,
    args: &Args,
    copy: bool,
    config: Option<&Config>,
) -> Result<bool> {
    let source = store.root(&args.cycle)?;
    let secondary = absolute_hint(
        args.secondary
            .as_deref()
            .or(config.and_then(|config| config.secondary.as_deref()))
            .context("specify --secondary or configure one with init")?,
    )?;
    let primary_artifacts = absolute_hint(&args.primary_artifacts)?;
    let secondary_artifacts = absolute_hint(&args.secondary_artifacts)?;
    // Check state separation before touching SQLite progress or either artifact tree.
    for root in [&secondary, &primary_artifacts, &secondary_artifacts] {
        let root = absolute_destination(root)?;
        ensure!(
            !root.starts_with(state) && !state.starts_with(&root),
            "replica roots and artifacts must not overlap app state"
        );
    }
    for root in store.roots()? {
        let root = absolute_destination(&root)?;
        for artifacts in [&primary_artifacts, &secondary_artifacts] {
            let artifacts = absolute_destination(artifacts)?;
            ensure!(
                !artifacts.starts_with(&root) && !root.starts_with(&artifacts),
                "artifact and media roots must not overlap"
            );
        }
    }
    let previous: Option<Binding> = store
        .replica_binding(&args.cycle)?
        .map(|json| serde_json::from_str(&json))
        .transpose()?;
    let database_bytes = fs::metadata(state.join("manifest.sqlite"))?.len();
    let reserve = database_bytes
        .checked_mul(4)
        .and_then(|size| size.checked_add(16 * 1024 * 1024))
        .context("artifact reserve overflow")?;
    let options = Options {
        source: &source,
        secondary: &secondary,
        primary_artifacts: &primary_artifacts,
        secondary_artifacts: &secondary_artifacts,
        copy,
        dry_run: cli.dry_run,
        allow_unverified_devices: args.allow_unverified_devices,
        free_space_margin: config
            .map(|config| config.free_space_margin)
            .unwrap_or(0.15),
        artifact_reserve: reserve,
        previous: previous.as_ref(),
    };
    let operation = if copy { "replicate" } else { "verify" };
    let run = if cli.dry_run {
        None
    } else {
        Some(store.begin_replica(&args.cycle, operation)?)
    };
    let mut bound = false;
    let mut summary = replica::execute(&options, &mut |progress| {
        if let Some(run) = run {
            if !bound {
                store.bind_replica(
                    &args.cycle,
                    &secondary,
                    &serde_json::to_string(progress.binding)?,
                )?;
                bound = true;
            }
            store.replica_progress(
                run,
                progress.file,
                progress.state,
                progress.copied_bytes,
                progress.partial,
            )?;
        }
        Ok(())
    });
    let mut report = report_value(&summary, &args.cycle, operation, run, &source, &secondary)?;
    if let Some(run) = run {
        let mut bundle = None;
        let published = (|| -> Result<()> {
            let bundle_parent = state.join("replica");
            fs::create_dir_all(&bundle_parent)?;
            volume::checked_directory(&bundle_parent)?;
            let directory = tempfile::Builder::new()
                .prefix(&format!("{}-run-{run}-", args.cycle))
                .tempdir_in(&bundle_parent)?
                .keep();
            bundle = Some(directory.clone());
            if summary.complete {
                let mut pending = report.clone();
                pending["complete"] = json!(false);
                pending["publication_pending"] = json!(true);
                store.begin_publication(&args.cycle, run, &serde_json::to_string(&pending)?)?;
                publish(
                    store,
                    &summary,
                    &mut report,
                    &directory,
                    &primary_artifacts,
                    &secondary_artifacts,
                )
            } else {
                write_json(&directory.join("summary.json"), &report)
            }
        })();
        if let Err(error) = published {
            summary.complete = false;
            summary.local_replica = "NOT VERIFIED";
            summary
                .issues
                .push(format!("Artifact publication failed: {error:#}"));
            report = report_value(
                &summary,
                &args.cycle,
                operation,
                Some(run),
                &source,
                &secondary,
            )?;
        }
        // Invalidate saved completion before attempting to save a failure artifact.
        store.finish_replica(
            &args.cycle,
            run,
            summary.complete,
            &serde_json::to_string(&report)?,
        )?;
        if let Some(bundle) = bundle {
            if !summary.complete && !bundle.join("summary.json").exists() {
                if let Err(error) = write_json(&bundle.join("failure.json"), &report) {
                    tracing::error!(%error, "could not save failure artifact; SQLite retains incomplete state");
                }
            }
            report["state_bundle"] = json!(bundle);
        }
    }
    emit(cli, &report)?;
    Ok(if cli.dry_run {
        summary.issues.is_empty()
    } else {
        summary.complete
    })
}

fn report_value(
    summary: &Summary,
    cycle: &str,
    operation: &str,
    run: Option<i64>,
    source: &Path,
    secondary: &Path,
) -> Result<Value> {
    let mut value = serde_json::to_value(summary)?;
    value["schema_version"] = json!(1);
    value["cycle"] = json!(cycle);
    value["operation"] = json!(operation);
    value["run_id"] = json!(run);
    value["source_root"] = json!(source);
    value["secondary_root"] = json!(secondary);
    value["artifact_bundles"] = json!([]);
    value["status_note"] = json!(
        "Local byte snapshot only; no cloud coverage or deletion approval. Saved reports require fresh verification before reuse."
    );
    if let Some(binding) = &summary.binding {
        value["source_identity"] = json!(binding.source);
        value["secondary_identity"] = json!(binding.secondary);
    }
    Ok(value)
}

fn write_json(path: &Path, value: &Value) -> Result<()> {
    let mut file = File::create_new(path)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

fn publish(
    store: &Store,
    summary: &Summary,
    report: &mut Value,
    state_bundle: &Path,
    primary_artifacts: &Path,
    secondary_artifacts: &Path,
) -> Result<()> {
    let binding = summary
        .binding
        .as_ref()
        .context("verified binding is missing")?;
    let name = state_bundle.file_name().context("bundle name is missing")?;
    let bundles = [
        state_bundle.to_owned(),
        primary_artifacts.join(name),
        secondary_artifacts.join(name),
    ];
    for bundle in &bundles[1..] {
        fs::create_dir(bundle)?;
        volume::checked_directory(bundle)?;
    }
    let manifest = state_bundle.join("manifest.jsonl");
    let mut file = File::create_new(&manifest)?;
    serde_json::to_writer(
        &mut file,
        &json!({"schema_version":1,"record_type":"replica_summary",
        "archive_id":report["cycle"], "run_id":report["run_id"], "snapshot_type":"deep_local_byte_verification",
        "source_root":report["source_root"], "secondary_root":report["secondary_root"],
        "source_identity":binding.source, "secondary_identity":binding.secondary,
        "independent_physical_drives":summary.independent_physical_drives, "cloud_coverage":"NOT VERIFIED"}),
    )?;
    file.write_all(b"\n")?;
    for fact in &binding.files {
        serde_json::to_writer(
            &mut file,
            &json!({"schema_version":1,"record_type":"file","archive_id":report["cycle"],
            "relative_path":fact.relative_path,"role":fact.kind,"byte_count":fact.byte_count,
            "source_mtime_ns":fact.mtime_ns,"source_sha256":fact.sha256,"secondary_sha256":fact.sha256,
            "local_replica":"LOCAL BYTES VERIFIED","cloud_asset_id":null,"pair_confidence":"unknown",
            "cloud_coverage":"NOT VERIFIED","warnings":fact.warnings}),
        )?;
        file.write_all(b"\n")?;
    }
    file.sync_all()?;
    drop(file);
    store.backup(&state_bundle.join("manifest.sqlite"))?;
    let manifest_hash = hash_file(&manifest)?;
    let database_hash = hash_file(&state_bundle.join("manifest.sqlite"))?;
    for bundle in &bundles[1..] {
        for (name, expected) in [
            ("manifest.jsonl", &manifest_hash),
            ("manifest.sqlite", &database_hash),
        ] {
            let mut source = File::open(state_bundle.join(name))?;
            let mut destination = File::create_new(bundle.join(name))?;
            std::io::copy(&mut source, &mut destination)?;
            destination.sync_all()?;
            drop(destination);
            ensure!(
                hash_file(&bundle.join(name))? == *expected,
                "artifact copy digest mismatch"
            );
        }
    }
    ensure!(
        volume::identity(Path::new(
            report["source_root"]
                .as_str()
                .context("source root missing")?
        ))? == binding.source
            && volume::identity(&binding.secondary_root)? == binding.secondary,
        "root identity changed during publication"
    );
    report["artifact_bundles"] = json!(bundles);
    report["manifest_sha256"] = json!(manifest_hash);
    report["database_backup_sha256"] = json!(database_hash);
    // Publish the app-state summary last. A failed publication invalidates live SQLite completion.
    for bundle in bundles.iter().rev() {
        write_json(&bundle.join("summary.json"), report)?;
    }
    #[cfg(unix)]
    for bundle in &bundles {
        File::open(bundle)?.sync_all()?;
    }
    Ok(())
}
