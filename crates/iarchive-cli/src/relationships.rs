use crate::{Cli, emit, worker::run_bounded};
use anyhow::{Context, Result, ensure};
use iarchive_core::{relationships::Metadata, resolve_input};
use iarchive_fs::{hash_file, volume};
use iarchive_store::Store;
use serde_json::Value;
use std::{
    fmt,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

// Only fixed diagnostics may reach persisted findings; tool output and I/O errors
// can contain private filenames or metadata values.
#[derive(Debug)]
struct MetadataFailure(&'static str);

impl fmt::Display for MetadataFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for MetadataFailure {}

fn metadata_error(error: anyhow::Error) -> Metadata {
    let reason = error
        .downcast_ref::<MetadataFailure>()
        .map_or("Source unavailable or changed", |failure| failure.0);
    Metadata {
        error: Some(format!("Metadata unavailable: {reason}")),
        ..Metadata::default()
    }
}

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
    let filename = input
        .to_str()
        .context(MetadataFailure("Filename is not valid Unicode"))?;
    ensure!(
        input.is_absolute() && !filename.contains(['\r', '\n']),
        MetadataFailure("Filename must be an absolute single-line path")
    );
    // Windows may recode argv before ExifTool sees it. Its UTF-8 argument-file
    // protocol avoids that conversion; stdin avoids another temporary pathname.
    // A fixed end-of-options marker and one filename cannot inject tool options.
    let arguments = format!("--\n{filename}\n");
    let output = run_bounded(
        command(tool).args([
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
            "-@",
            "-",
        ]),
        Some(arguments.as_bytes()),
        Duration::from_secs(30),
    )
    .context(MetadataFailure(
        "ExifTool failed or exceeded process limits",
    ))?;
    parse_metadata(&output, input)
}

fn source_matches(source: &str, input: &Path) -> bool {
    // ExifTool changes Windows backslashes to '/', including the verbatim prefix.
    // Restore native separators before canonicalizing; compare the full identity.
    #[cfg(windows)]
    let source = source.replace('/', "\\");
    let path = Path::new(&source);
    path.is_absolute()
        && crate::review::reject_links(path).is_ok()
        && path.canonicalize().is_ok_and(|path| path == input)
}

fn parse_metadata(output: &[u8], input: &Path) -> Result<Metadata> {
    let value: Vec<Value> = serde_json::from_slice(output)
        .context(MetadataFailure("ExifTool returned invalid JSON"))?;
    ensure!(
        value.len() == 1,
        MetadataFailure("ExifTool response must contain exactly one input")
    );
    let record = value[0]
        .as_object()
        .context(MetadataFailure("ExifTool returned an invalid record"))?;
    ensure!(
        record
            .get("SourceFile")
            .and_then(Value::as_str)
            .is_some_and(|source| source_matches(source, input)),
        MetadataFailure("ExifTool response does not identify the requested file")
    );
    let mut metadata = Metadata::default();
    fn assign(slot: &mut Option<String>, value: &Value) -> Result<()> {
        let text = value
            .as_str()
            .context(MetadataFailure("Metadata identifier is not text"))?;
        ensure!(
            !text.is_empty() && text.len() <= 256 && !text.chars().any(char::is_control),
            MetadataFailure("Invalid metadata identifier")
        );
        ensure!(
            slot.as_deref().is_none_or(|old| old == text),
            MetadataFailure("Conflicting metadata identifiers")
        );
        *slot = Some(text.into());
        Ok(())
    }
    for (key, value) in record {
        let parts: Vec<_> = key.split(':').collect();
        let tag = *parts.last().unwrap();
        if matches!(tag, "Error" | "Warning") {
            anyhow::bail!(MetadataFailure("ExifTool reported an error or warning"));
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
                        .context(MetadataFailure("Invalid original document reference"))?;
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
                MetadataFailure("Inventory finding prevents metadata extraction")
            );
            let input = resolve_input(&root, &fact.relative_path)
                .context(MetadataFailure("Source path is unavailable or unsafe"))?;
            let hash = fact
                .sha256
                .as_deref()
                .context(MetadataFailure("Inventory hash is unavailable"))?;
            let before = hash_file(&input).context(MetadataFailure(
                "Source could not be verified before extraction",
            ))?;
            ensure!(
                before == hash,
                MetadataFailure("Source changed since inventory")
            );
            let metadata = extract(&tool, &input)?;
            let after = hash_file(&input).context(MetadataFailure(
                "Source could not be verified after extraction",
            ))?;
            ensure!(
                after == hash,
                MetadataFailure("Source changed during metadata extraction")
            );
            Ok(metadata)
        })();
        let metadata = result.unwrap_or_else(|error| {
            errors += 1;
            metadata_error(error)
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    #[test]
    fn utf8_filenames_preserve_metadata() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let tool = tool(Some(&repo.join(if cfg!(windows) {
            ".windows-sandbox/exiftool/exiftool.exe"
        } else {
            ".tools/exiftool/exiftool"
        })))
        .unwrap();
        version(&tool).unwrap(); // The actual pinned dependency is required, including in CI.
        let dir = tempfile::Builder::new()
            .prefix("iarchive metadata % # 雪 ")
            .tempdir()
            .unwrap();
        let root = dir.path().canonicalize().unwrap();
        let xmp = r#"<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
xmpMM:DocumentID="xmp.did:synthetic-document"/></rdf:RDF></x:xmpmeta>"#;
        for name in ["metadata % # 雪.xmp", "-overwrite_original.xmp"] {
            let input = root.join(name);
            fs::write(&input, xmp).unwrap();
            let before = (
                hash_file(&input).unwrap(),
                fs::metadata(&input).unwrap().modified().unwrap(),
            );
            let metadata = extract(&tool, &input).unwrap();
            assert_eq!(
                metadata.document_id.as_deref(),
                Some("xmp.did:synthetic-document")
            );
            assert_eq!(metadata.error, None);
            assert_eq!(
                (
                    hash_file(&input).unwrap(),
                    fs::metadata(&input).unwrap().modified().unwrap()
                ),
                before
            );
        }
        assert_eq!(fs::read_dir(root).unwrap().count(), 2);
    }

    #[test]
    fn argument_lines_cannot_inject_options() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        let missing_tool = root.join("missing-exiftool");
        for separator in ['\r', '\n'] {
            let input = root.join(format!("image{separator}-overwrite_original.xmp"));
            assert!(input.is_absolute());
            let finding = metadata_error(extract(&missing_tool, &input).unwrap_err());
            assert_eq!(
                finding.error.as_deref(),
                Some("Metadata unavailable: Filename must be an absolute single-line path")
            );
        }
        // A process error still identifies its stage without echoing a tool path.
        let finding = metadata_error(extract(&missing_tool, &root.join("image.xmp")).unwrap_err());
        assert_eq!(
            finding.error.as_deref(),
            Some("Metadata unavailable: ExifTool failed or exceeded process limits")
        );
        assert_eq!(fs::read_dir(root).unwrap().count(), 0);
    }

    #[test]
    fn metadata_responses_bind_one_source() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        let input = root.join("source 雪.xmp");
        let other = root.join("other.xmp");
        fs::write(&input, b"synthetic source").unwrap();
        fs::write(&other, b"synthetic other source").unwrap();
        let valid = json!({"SourceFile":input,"XMP-xmpMM:DocumentID":"synthetic-document"});
        let mut slashed = valid.clone();
        slashed["SourceFile"] = json!(input.to_str().unwrap().replace('\\', "/"));
        for record in [&valid, &slashed] {
            let metadata =
                parse_metadata(&serde_json::to_vec(&json!([record])).unwrap(), &input).unwrap();
            assert_eq!(metadata.document_id.as_deref(), Some("synthetic-document"));
        }
        let amended = |fields: Value| {
            let mut record = valid.clone();
            record
                .as_object_mut()
                .unwrap()
                .extend(fields.as_object().unwrap().clone());
            json!([record])
        };
        let cases = [
            (
                json!([]),
                "ExifTool response must contain exactly one input",
            ),
            (
                json!([valid, valid]),
                "ExifTool response must contain exactly one input",
            ),
            (json!([null]), "ExifTool returned an invalid record"),
            (
                amended(json!({"SourceFile":other})),
                "ExifTool response does not identify the requested file",
            ),
            (
                amended(json!({"SourceFile":"source 雪.xmp"})),
                "ExifTool response does not identify the requested file",
            ),
            (
                amended(json!({"ExifTool:Warning":"PRIVATE_VALUE"})),
                "ExifTool reported an error or warning",
            ),
            (
                amended(json!({"XMP-xmpMM:Copy1:DocumentID":"PRIVATE_VALUE"})),
                "Conflicting metadata identifiers",
            ),
            (
                amended(json!({"XMP-xmpMM:DocumentID":42})),
                "Metadata identifier is not text",
            ),
            (
                amended(json!({"XMP-xmpMM:DerivedFrom":"PRIVATE_VALUE"})),
                "Invalid original document reference",
            ),
        ];
        for (response, reason) in cases {
            let error =
                parse_metadata(&serde_json::to_vec(&response).unwrap(), &input).unwrap_err();
            let finding = metadata_error(error);
            assert_eq!(
                finding.error,
                Some(format!("Metadata unavailable: {reason}"))
            );
        }
        let finding = metadata_error(parse_metadata(b"PRIVATE_INVALID_JSON", &input).unwrap_err());
        assert_eq!(
            finding.error.as_deref(),
            Some("Metadata unavailable: ExifTool returned invalid JSON")
        );
        let finding = metadata_error(anyhow::anyhow!("PRIVATE_IO_DETAILS"));
        assert_eq!(
            finding.error.as_deref(),
            Some("Metadata unavailable: Source unavailable or changed")
        );
    }
}
