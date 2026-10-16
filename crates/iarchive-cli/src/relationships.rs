use crate::{Cli, emit, worker::run_bounded};
use anyhow::{Context, Result, ensure};
use iarchive_core::{relationships::Metadata, resolve_input};
use iarchive_fs::{hash_file, volume};
use iarchive_store::Store;
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

pub fn tool(explicit: Option<&Path>) -> Result<PathBuf> {
    let path = explicit.map(Path::to_owned).unwrap_or_else(|| {
        let local = std::env::current_dir()
            .unwrap_or_default()
            .join(if cfg!(windows) {
                ".windows-sandbox/exiftool"
            } else {
                ".tools/exiftool"
            });
        local.join(if cfg!(windows) {
            "exiftool.exe"
        } else {
            "exiftool"
        })
    });
    ensure!(
        path.is_file(),
        "ExifTool is unavailable; run the project setup or scripts/setup_metadata.py"
    );
    crate::review::reject_links(&path)?;
    Ok(path.canonicalize()?)
}

fn command(tool: &Path) -> Command {
    let mut command = if cfg!(windows) {
        Command::new(tool)
    } else {
        let mut command = Command::new("perl");
        command.arg(tool);
        command
    };
    // Ignore user Perl configuration; never load ExifTool's executable user config.
    command
        .env_remove("PERL5OPT")
        .env_remove("PERL5LIB")
        .args(["-config", ""]);
    command
}

pub fn version(tool: &Path) -> Result<String> {
    let output = run_bounded(command(tool).arg("-ver"), None, Duration::from_secs(10))?;
    let version = std::str::from_utf8(&output)?.trim();
    ensure!(
        version == iarchive_core::relationships::EXTRACTOR_VERSION,
        "use the project-pinned ExifTool 13.59"
    );
    Ok(version.into())
}

fn extract(tool: &Path, input: &Path) -> Result<Metadata> {
    let output = run_bounded(
        command(tool)
            .args([
                "-json",
                "-charset",
                "filename=UTF8",
                "-G1:4",
                "-a",
                "-s",
                "-struct",
                "-FileType",
                "-Apple:ContentIdentifier",
                "-Keys:ContentIdentifier",
                "-Apple:ImageUniqueID",
                "-XMP-xmpMM:DocumentID",
                "-XMP-xmpMM:OriginalDocumentID",
                "-XMP-xmpMM:DerivedFrom",
                "-Error",
                "-Warning",
                "--",
            ])
            .arg(input),
        None,
        Duration::from_secs(30),
    )?;
    let value: Vec<Value> = serde_json::from_slice(&output)?;
    ensure!(
        value.len() == 1,
        "metadata response must contain exactly one input"
    );
    let record = value[0].as_object().context("invalid metadata record")?;
    ensure!(
        record
            .get("SourceFile")
            .and_then(Value::as_str)
            .is_some_and(|p| Path::new(p).canonicalize().is_ok_and(|p| p == input)),
        "metadata response does not match the requested file"
    );
    let mut metadata = Metadata::default();
    fn assign(slot: &mut Option<String>, value: &Value) -> Result<()> {
        let text = value.as_str().context("metadata identifier is not text")?;
        ensure!(
            !text.is_empty() && text.len() <= 256 && !text.chars().any(char::is_control),
            "invalid metadata identifier"
        );
        ensure!(
            slot.as_deref().is_none_or(|old| old == text),
            "conflicting metadata identifiers"
        );
        *slot = Some(text.into());
        Ok(())
    }
    for (key, value) in record {
        let parts: Vec<_> = key.split(':').collect();
        let tag = *parts.last().unwrap();
        if matches!(tag, "Error" | "Warning") {
            anyhow::bail!("metadata reader reported an error or warning");
        }
        if parts.contains(&"Apple") {
            match tag {
                "ContentIdentifier" => assign(&mut metadata.content_id, value)?,
                "ImageUniqueID" => assign(&mut metadata.image_id, value)?,
                _ => {}
            }
        } else if parts.contains(&"Keys") && tag == "ContentIdentifier" {
            assign(&mut metadata.content_id, value)?;
        } else if parts.contains(&"XMP-xmpMM") {
            match tag {
                "DocumentID" => assign(&mut metadata.document_id, value)?,
                "OriginalDocumentID" => assign(&mut metadata.original_document_id, value)?,
                "DerivedFrom" => {
                    let parent = value
                        .as_object()
                        .context("invalid original document reference")?;
                    if let Some(value) = parent.get("DocumentID") {
                        assign(&mut metadata.derived_from, value)?;
                    }
                }
                _ => {}
            }
        }
    }
    Ok(metadata)
}

pub fn run(cli: &Cli, store: &Store, cycle: &str, explicit: Option<&Path>) -> Result<bool> {
    ensure!(
        store.active_jobs(cycle)?.is_empty(),
        "restore quarantined files before reading relationships"
    );
    let tool = tool(explicit)?;
    let temporary = volume::checked_directory(&std::env::temp_dir().canonicalize()?)?;
    for media in store.roots()? {
        ensure!(
            !temporary.starts_with(media.canonicalize()?),
            "system temporary files must stay outside archive roots"
        );
        iarchive_core::ensure_separate(
            tool.parent().context("metadata tool directory missing")?,
            &media,
        )?;
    }
    let version = version(&tool)?;
    let root = volume::checked_directory(&store.root(cycle)?)?;
    let identity = volume::identity(&root)?;
    let files = store.file_facts(cycle)?;
    let mut observations = vec![];
    let mut errors = 0;
    for (id, fact) in &files {
        let result = (|| -> Result<Metadata> {
            ensure!(
                fact.error.is_none(),
                "inventory finding prevents metadata extraction"
            );
            let input = resolve_input(&root, &fact.relative_path)?;
            let hash = fact.sha256.as_deref().context("file is not hashed")?;
            ensure!(hash_file(&input)? == hash, "file changed since inventory");
            let metadata = extract(&tool, &input)?;
            ensure!(
                hash_file(&input)? == hash,
                "file changed during metadata extraction"
            );
            Ok(metadata)
        })();
        let metadata = result.unwrap_or_else(|_| {
            errors += 1;
            Metadata {
                error: Some("Metadata unavailable, ambiguous, or source changed".into()),
                ..Metadata::default()
            }
        });
        observations.push((*id, fact, metadata));
    }
    ensure!(
        volume::identity(&root)? == identity,
        "source identity changed during metadata extraction"
    );
    if !cli.dry_run {
        let tx = store.connection.unchecked_transaction()?;
        for (id, fact, metadata) in &observations {
            if let Some(hash) = &fact.sha256 {
                store.save_metadata(*id, hash, &version, metadata)?;
            }
        }
        tx.commit()?;
    }
    let groups = iarchive_core::relationships::group(
        observations
            .into_iter()
            .map(
                |(id, fact, metadata)| iarchive_core::relationships::Member {
                    id,
                    fact: fact.clone(),
                    metadata,
                    role: String::new(),
                },
            )
            .collect(),
    );
    let complete = store.status(cycle)?.inventory_complete
        && errors == 0
        && groups.iter().all(|g| g.blockers.is_empty());
    emit(
        cli,
        &serde_json::json!({"cycle":cycle,"dry_run":cli.dry_run,"extractor_version":version,
        "groups":groups,"metadata_errors":errors,"complete":complete,"media_changed":false,
        "note":"Local metadata relationships only. Missing tags do not prove a standalone file, and cloud coverage is NOT VERIFIED."}),
    )?;
    Ok(complete)
}
