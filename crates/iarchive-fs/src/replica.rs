//! Non-destructive copying and uncached verification of a frozen local file set.
use crate::{
    LocalStorage, hash_file,
    volume::{self, VolumeIdentity},
};
use anyhow::{Context, Result, ensure};
use iarchive_core::{FileFact, StorageProvider, collision_key, normalized_relative, resolve_input};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Binding {
    pub source: VolumeIdentity,
    pub secondary: VolumeIdentity,
    pub secondary_root: PathBuf,
    pub files: Vec<FileFact>,
}

pub struct Options<'a> {
    pub source: &'a Path,
    pub secondary: &'a Path,
    pub primary_artifacts: &'a Path,
    pub secondary_artifacts: &'a Path,
    pub copy: bool,
    pub dry_run: bool,
    pub allow_unverified_devices: bool,
    pub free_space_margin: f64,
    pub artifact_reserve: u64,
    pub previous: Option<&'a Binding>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Summary {
    pub complete: bool,
    pub dry_run: bool,
    pub planned_files: u64,
    pub copied_files: u64,
    pub existing_files: u64,
    pub verified_files: u64,
    pub bytes: u64,
    pub required_copy_bytes: u64,
    pub free_bytes: Option<u64>,
    pub missing_files: u64,
    pub extra_files: u64,
    pub hash_mismatches: u64,
    pub file_errors: u64,
    pub local_replica: &'static str,
    pub independent_physical_drives: &'static str,
    pub cloud_coverage: &'static str,
    pub issues: Vec<String>,
    pub retained_partial_directory: Option<PathBuf>,
    #[serde(skip_serializing)]
    pub binding: Option<Binding>,
}

impl Summary {
    pub fn new(dry_run: bool) -> Self {
        Self {
            complete: false,
            dry_run,
            planned_files: 0,
            copied_files: 0,
            existing_files: 0,
            verified_files: 0,
            bytes: 0,
            required_copy_bytes: 0,
            free_bytes: None,
            missing_files: 0,
            extra_files: 0,
            hash_mismatches: 0,
            file_errors: 0,
            local_replica: "NOT VERIFIED",
            independent_physical_drives: "NOT VERIFIED",
            cloud_coverage: "NOT VERIFIED",
            issues: vec![],
            retained_partial_directory: None,
            binding: None,
        }
    }
}

pub struct Progress<'a> {
    pub binding: &'a Binding,
    pub file: &'a FileFact,
    pub state: &'a str,
    pub copied_bytes: u64,
    pub partial: Option<&'a Path>,
}

fn inventory(root: &Path, issues: &mut Vec<String>) -> Result<Vec<FileFact>> {
    let mut files = BTreeMap::new();
    let mut names = BTreeMap::new();
    LocalStorage.visit(root, &mut |mut fact| {
        let mut prefix = String::new();
        for part in fact.relative_path.split('/') {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(part);
            if let Some(previous) = names.insert(collision_key(&prefix), prefix.clone()) {
                if previous != prefix {
                    issues.push(format!("Case/Unicode collision: {previous} | {prefix}"));
                }
            }
        }
        for warning in &fact.warnings {
            if warning.starts_with("Non-portable") {
                issues.push(warning.clone());
            }
        }
        if fact.error.is_none() {
            match resolve_input(root, &fact.relative_path).and_then(|path| hash_file(&path)) {
                Ok(hash) => fact.sha256 = Some(hash),
                Err(error) => fact.error = Some(format!("Unreadable or unstable file: {error:#}")),
            }
        }
        if let Some(error) = &fact.error {
            issues.push(format!("{}: {error}", fact.relative_path));
        }
        files.insert(fact.relative_path.clone(), fact);
        Ok(())
    })?;
    Ok(files.into_values().collect())
}

/// Reject aliases and linked ancestors before allowing any writes.
pub fn validate_roots(options: &Options<'_>) -> Result<()> {
    let roots = [
        options.source,
        options.secondary,
        options.primary_artifacts,
        options.secondary_artifacts,
    ];
    let roots: Vec<_> = roots
        .iter()
        .map(|root| volume::checked_directory(root))
        .collect::<Result<_>>()?;
    for (number, a) in roots.iter().enumerate() {
        for b in &roots[number + 1..] {
            ensure!(
                !a.starts_with(b) && !b.starts_with(a),
                "media and artifact roots must not overlap"
            );
        }
    }
    Ok(())
}

fn same_contents(a: &FileFact, b: &FileFact) -> bool {
    a.byte_count == b.byte_count && a.sha256.is_some() && a.sha256 == b.sha256 && b.error.is_none()
}

/// Read and independently hash both media roots without progress or artifact writes.
/// This observes the frozen replica; it does not authorize a later operation.
pub fn verify_frozen(source: &Path, binding: &Binding) -> Summary {
    let mut report = Summary::new(true);
    let result = (|| -> Result<()> {
        let source =
            volume::checked_directory(source).context("source root is unavailable or linked")?;
        let secondary = volume::checked_directory(&binding.secondary_root)
            .context("bound secondary root is unavailable or linked")?;
        ensure!(
            !source.starts_with(&secondary) && !secondary.starts_with(&source),
            "source and secondary roots overlap"
        );
        let check_identity = || -> Result<()> {
            ensure!(
                volume::identity(&source)? == binding.source
                    && volume::identity(&secondary)? == binding.secondary,
                "source or bound secondary root/volume changed or disconnected"
            );
            Ok(())
        };
        check_identity()?;
        if volume::independent(&binding.source, &binding.secondary) {
            report.independent_physical_drives = "VERIFIED BY OS TOPOLOGY";
        }
        ensure!(
            !binding.files.is_empty(),
            "frozen archive contains no files"
        );
        let current = inventory(&source, &mut report.issues)?;
        let copies = inventory(&secondary, &mut report.issues)?;
        report.planned_files = binding.files.len() as u64;
        report.file_errors = current
            .iter()
            .chain(&copies)
            .filter(|f| f.error.is_some())
            .count() as u64;
        let expected: BTreeMap<_, _> = binding
            .files
            .iter()
            .map(|f| (f.relative_path.as_str(), f))
            .collect();
        ensure!(
            expected.len() == binding.files.len(),
            "duplicate paths in frozen replica binding"
        );
        report.bytes = binding.files.iter().try_fold(0u64, |total, f| {
            total
                .checked_add(f.byte_count)
                .context("archive byte total overflow")
        })?;
        for (label, files) in [("source", &current), ("secondary", &copies)] {
            for file in files {
                if !expected.contains_key(file.relative_path.as_str()) {
                    report.extra_files += 1;
                    report
                        .issues
                        .push(format!("Extra {label} entry: {}", file.relative_path));
                }
            }
        }
        let current: BTreeMap<_, _> = current
            .iter()
            .map(|f| (f.relative_path.as_str(), f))
            .collect();
        let copies: BTreeMap<_, _> = copies
            .iter()
            .map(|f| (f.relative_path.as_str(), f))
            .collect();
        for file in &binding.files {
            match (
                current.get(file.relative_path.as_str()),
                copies.get(file.relative_path.as_str()),
            ) {
                (Some(now), Some(copy))
                    if *now == file && file.error.is_none() && same_contents(file, copy) =>
                {
                    report.verified_files += 1;
                }
                (None, _) | (_, None) => {
                    report.missing_files += 1;
                    report.issues.push(format!(
                        "Missing source or secondary file: {}",
                        file.relative_path
                    ));
                }
                _ => {
                    report.hash_mismatches += 1;
                    report.issues.push(format!(
                        "Source facts or replica bytes changed: {}",
                        file.relative_path
                    ));
                }
            }
        }
        check_identity()?;
        ensure!(
            report.issues.is_empty(),
            "fresh replica check contains findings"
        );
        report.complete = true;
        report.local_replica = "LOCAL BYTES VERIFIED";
        Ok(())
    })();
    if let Err(error) = result {
        report.issues.push(format!("{error:#}"));
    }
    report
}

pub fn capacity(required: u64, reserve: u64, available: u64, margin: f64) -> Result<()> {
    ensure!(
        margin.is_finite() && (0.0..1.0).contains(&margin),
        "invalid free-space margin"
    );
    let total = required
        .checked_add(reserve)
        .context("required capacity overflow")?;
    ensure!(
        (total as f64) <= (available as f64) * (1.0 - margin),
        "insufficient free space with configured reserve: need {total} bytes, available {available}"
    );
    Ok(())
}

fn check_binding(options: &Options<'_>, binding: &Binding) -> Result<()> {
    ensure!(
        volume::identity(options.source)? == binding.source
            && volume::identity(options.secondary)? == binding.secondary,
        "source or secondary root/volume identity changed or disconnected"
    );
    ensure!(
        volume::identity(options.primary_artifacts)?.volume_id == binding.source.volume_id
            && volume::identity(options.secondary_artifacts)?.volume_id
                == binding.secondary.volume_id,
        "artifact directories must reside on their corresponding media volumes"
    );
    Ok(())
}

/// Each invocation rehashes both roots. A callback durably records per-file progress.
pub fn execute(
    options: &Options<'_>,
    progress: &mut dyn FnMut(Progress<'_>) -> Result<()>,
) -> Summary {
    let mut report = Summary::new(options.dry_run);
    if let Err(error) = execute_inner(options, progress, &mut report) {
        report.complete = false;
        report.local_replica = "NOT VERIFIED";
        report.issues.push(format!("{error:#}"));
    }
    report
}

fn execute_inner(
    options: &Options<'_>,
    progress: &mut dyn FnMut(Progress<'_>) -> Result<()>,
    report: &mut Summary,
) -> Result<()> {
    validate_roots(options)?;
    let source_identity = volume::identity(options.source)?;
    let secondary_identity = volume::identity(options.secondary)?;
    ensure!(
        source_identity.volume_id != secondary_identity.volume_id
            || source_identity.root_id != secondary_identity.root_id,
        "source and secondary resolve to the same directory"
    );
    if let Some(previous) = options.previous {
        ensure!(
            previous.source == source_identity
                && previous.secondary == secondary_identity
                && previous.secondary_root == options.secondary,
            "source or bound secondary root/volume changed; no media was read"
        );
    }
    let source = inventory(options.source, &mut report.issues)?;
    let binding = Binding {
        source: source_identity,
        secondary: secondary_identity,
        secondary_root: options.secondary.to_owned(),
        files: source,
    };
    check_binding(options, &binding)?;
    ensure!(
        !binding.files.is_empty(),
        "source archive contains no files"
    );
    report.planned_files = binding.files.len() as u64;
    report.bytes = binding.files.iter().try_fold(0u64, |total, file| {
        total
            .checked_add(file.byte_count)
            .context("archive byte total overflow")
    })?;
    report.file_errors = binding
        .files
        .iter()
        .filter(|file| file.error.is_some())
        .count() as u64;
    report.binding = Some(binding.clone());
    if let Some(previous) = options.previous {
        ensure!(
            previous == &binding,
            "frozen archive or bound secondary changed; restore it or use a new cycle"
        );
    }
    if volume::independent(&binding.source, &binding.secondary) {
        report.independent_physical_drives = "VERIFIED BY OS TOPOLOGY";
    } else {
        ensure!(
            options.allow_unverified_devices,
            "physical drive independence is NOT VERIFIED; use --allow-unverified-devices to acknowledge a byte-only test"
        );
    }
    let target = inventory(options.secondary, &mut report.issues)?;
    report.file_errors += target.iter().filter(|file| file.error.is_some()).count() as u64;
    let target: BTreeMap<_, _> = target
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();
    let expected: BTreeMap<_, _> = binding
        .files
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();
    for file in target.values() {
        if !expected.contains_key(file.relative_path.as_str()) {
            report.extra_files += 1;
            report.issues.push(format!(
                "Extra secondary entry retained: {}",
                file.relative_path
            ));
        }
    }
    for file in &binding.files {
        match target.get(file.relative_path.as_str()) {
            Some(copy) if same_contents(file, copy) => report.existing_files += 1,
            Some(_) => {
                report.hash_mismatches += 1;
                report.issues.push(format!(
                    "Conflicting secondary file retained: {}",
                    file.relative_path
                ));
            }
            None => {
                report.required_copy_bytes = report
                    .required_copy_bytes
                    .checked_add(file.byte_count)
                    .context("copy byte total overflow")?;
                if !options.copy {
                    report.missing_files += 1;
                    report
                        .issues
                        .push(format!("Missing secondary file: {}", file.relative_path));
                }
            }
        }
    }
    report.free_bytes = Some(fs2::available_space(options.secondary)?);
    let records_reserve = binding.files.iter().try_fold(0u64, |total, file| {
        let text_bytes =
            file.relative_path.len() + file.warnings.iter().map(String::len).sum::<usize>();
        total
            .checked_add(2048)
            .and_then(|total| total.checked_add((text_bytes as u64).saturating_mul(8)))
            .context("manifest capacity estimate overflow")
    })?;
    let bundle_reserve = options
        .artifact_reserve
        .checked_add(records_reserve)
        .context("artifact reserve overflow")?;
    // Reserve conservatively for all three bundles even when some live on other volumes.
    let all_bundles = bundle_reserve
        .checked_mul(3)
        .context("artifact reserve overflow")?;
    capacity(
        report.required_copy_bytes,
        all_bundles,
        report.free_bytes.unwrap(),
        options.free_space_margin,
    )?;
    capacity(
        0,
        bundle_reserve,
        fs2::available_space(options.primary_artifacts)?,
        options.free_space_margin,
    )?;
    ensure!(
        report.issues.is_empty(),
        "preflight findings block completion; no files copied"
    );
    if options.dry_run {
        return Ok(());
    }
    // Freeze source facts before copying so retries cannot quietly adopt changed input.
    for file in &binding.files {
        progress(Progress {
            binding: &binding,
            file,
            state: "PLANNED",
            copied_bytes: 0,
            partial: None,
        })?;
    }
    let partials = tempfile::Builder::new()
        .prefix("partials-")
        .tempdir_in(options.secondary_artifacts)?
        .keep();
    report.retained_partial_directory = Some(partials.clone());
    for file in &binding.files {
        check_binding(options, &binding)?;
        if target.contains_key(file.relative_path.as_str()) {
            progress(Progress {
                binding: &binding,
                file,
                state: "MATCHED",
                copied_bytes: file.byte_count,
                partial: None,
            })?;
            continue;
        }
        copy_file(options, &binding, file, &partials, progress)?;
        report.copied_files += 1;
    }
    // Fresh complete traversals detect additions, removals and edits during transfer.
    let after = inventory(options.source, &mut report.issues)?;
    if after != binding.files {
        report
            .issues
            .push("source inventory changed during the operation".into());
    }
    let after_target = inventory(options.secondary, &mut report.issues)?;
    report.file_errors = after
        .iter()
        .chain(&after_target)
        .filter(|file| file.error.is_some())
        .count() as u64;
    let source_now: BTreeMap<_, _> = after
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();
    let target_now: BTreeMap<_, _> = after_target
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();
    for file in &after_target {
        if !expected.contains_key(file.relative_path.as_str()) {
            report.extra_files += 1;
            report.issues.push(format!(
                "Extra secondary entry retained: {}",
                file.relative_path
            ));
        }
    }
    for file in &binding.files {
        match (
            source_now.get(file.relative_path.as_str()),
            target_now.get(file.relative_path.as_str()),
        ) {
            (Some(source), Some(copy)) if *source == file && same_contents(file, copy) => {
                progress(Progress {
                    binding: &binding,
                    file,
                    state: "VERIFIED",
                    copied_bytes: file.byte_count,
                    partial: None,
                })?;
                report.verified_files += 1;
            }
            (None, _) | (_, None) => {
                report.missing_files += 1;
                report.issues.push(format!(
                    "Missing source or secondary file in final inventory: {}",
                    file.relative_path
                ));
            }
            _ => {
                report.hash_mismatches += 1;
                report.issues.push(format!(
                    "Final independent hashes or source metadata differ: {}",
                    file.relative_path
                ));
            }
        }
    }
    check_binding(options, &binding)?;
    ensure!(
        report.issues.is_empty(),
        "final inventory contains findings"
    );
    report.complete = true;
    report.local_replica = "LOCAL BYTES VERIFIED";
    Ok(())
}

fn destination(root: &Path, relative: &str) -> Result<PathBuf> {
    ensure!(
        normalized_relative(Path::new(relative))? == relative,
        "non-normalized replica path"
    );
    let path = root.join(relative);
    let mut current = root.to_owned();
    let parent = Path::new(relative).parent().context("file has no parent")?;
    for part in parent.components() {
        current.push(part);
        match fs::create_dir(&current) {
            Ok(()) => (),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(error) => return Err(error.into()),
        }
        volume::checked_directory(&current)?;
    }
    Ok(path)
}

fn copy_file(
    options: &Options<'_>,
    binding: &Binding,
    fact: &FileFact,
    partials: &Path,
    progress: &mut dyn FnMut(Progress<'_>) -> Result<()>,
) -> Result<()> {
    let source = resolve_input(options.source, &fact.relative_path)?;
    let mut input = File::open(&source)?;
    let modified = input.metadata()?.modified()?;
    let mut temp = tempfile::Builder::new()
        .prefix("copy-")
        .suffix(".partial")
        .tempfile_in(partials)?;
    temp.disable_cleanup(true); // interruption/error leaves evidence, never a completed media file
    let mut digest = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = vec![0u8; 1024 * 1024];
    progress(Progress {
        binding,
        file: fact,
        state: "COPYING",
        copied_bytes: 0,
        partial: Some(temp.path()),
    })?;
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        bytes = bytes
            .checked_add(count as u64)
            .context("copy length overflow")?;
        ensure!(bytes <= fact.byte_count, "source grew during copy");
        temp.write_all(&buffer[..count])?;
        digest.update(&buffer[..count]);
        progress(Progress {
            binding,
            file: fact,
            state: "COPYING",
            copied_bytes: bytes,
            partial: Some(temp.path()),
        })?;
    }
    ensure!(
        bytes == fact.byte_count && Some(format!("{:x}", digest.finalize())) == fact.sha256,
        "source bytes changed during copy"
    );
    temp.as_file().set_modified(modified)?;
    temp.as_file().sync_all()?;
    check_binding(options, binding)?;
    let target = destination(options.secondary, &fact.relative_path)?;
    temp.persist_noclobber(&target)
        .map_err(|error| error.error)?;
    #[cfg(unix)]
    File::open(target.parent().context("target parent unavailable")?)?.sync_all()?;
    ensure!(
        hash_file(&resolve_input(options.secondary, &fact.relative_path)?)?
            == fact.sha256.as_deref().unwrap(),
        "new secondary file failed independent verification"
    );
    progress(Progress {
        binding,
        file: fact,
        state: "COPIED",
        copied_bytes: bytes,
        partial: None,
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capacity_reserves_space_and_checks_overflow() {
        assert!(capacity(85, 0, 100, 0.15).is_ok());
        for (required, reserve, available, margin) in [
            (86, 0, 100, 0.15),
            (80, 10, 100, 0.15),
            (u64::MAX, 1, u64::MAX, 0.0),
            (0, 0, 1, f64::NAN),
        ] {
            assert!(capacity(required, reserve, available, margin).is_err());
        }
    }

    #[test]
    fn interrupted_copy_resumes_without_trusting_partials() {
        let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let paths: Vec<_> = ["source", "secondary", "primary", "reports"]
            .iter()
            .map(|name| dir.path().join(name))
            .collect();
        for path in &paths {
            fs::create_dir(path).unwrap();
        }
        fs::write(paths[0].join("video.mov"), vec![7u8; 2 * 1024 * 1024]).unwrap();
        let options = Options {
            source: &paths[0],
            secondary: &paths[1],
            primary_artifacts: &paths[2],
            secondary_artifacts: &paths[3],
            copy: true,
            dry_run: false,
            allow_unverified_devices: true,
            free_space_margin: 0.15,
            artifact_reserve: 0,
            previous: None,
        };
        let failed = execute(&options, &mut |step| {
            if step.state == "COPYING" && step.copied_bytes > 0 {
                anyhow::bail!("injected disconnection");
            }
            Ok(())
        });
        assert!(!failed.complete);
        assert!(!paths[1].join("video.mov").exists());
        let partials = fs::read_dir(failed.retained_partial_directory.unwrap())
            .unwrap()
            .collect::<Vec<_>>();
        assert_eq!(partials.len(), 1);
        assert_eq!(
            partials[0].as_ref().unwrap().metadata().unwrap().len(),
            1024 * 1024
        );
        let retry = execute(&options, &mut |_| Ok(()));
        assert!(retry.complete, "{:?}", retry.issues);
        assert_eq!(retry.copied_files, 1);
        assert_eq!(
            fs::read(paths[1].join("video.mov")).unwrap(),
            vec![7u8; 2 * 1024 * 1024]
        );
        assert!(partials[0].as_ref().unwrap().path().exists());
    }

    #[test]
    fn replica_faults_prevent_completion() {
        for fault in ["capacity", "swap", "conflict", "source", "postcopy"] {
            let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
            let paths: Vec<_> = ["source", "secondary", "primary", "reports"]
                .iter()
                .map(|name| dir.path().join(name))
                .collect();
            for path in &paths {
                fs::create_dir(path).unwrap();
            }
            fs::write(paths[0].join("photo.heic"), b"synthetic original").unwrap();
            let options = Options {
                source: &paths[0],
                secondary: &paths[1],
                primary_artifacts: &paths[2],
                secondary_artifacts: &paths[3],
                copy: true,
                dry_run: false,
                allow_unverified_devices: true,
                free_space_margin: 0.15,
                artifact_reserve: if fault == "capacity" {
                    fs2::available_space(&paths[1]).unwrap()
                } else {
                    0
                },
                previous: None,
            };
            let mut injected = false;
            let report = execute(&options, &mut |step| {
                if !injected
                    && ((fault == "postcopy" && step.state == "COPIED")
                        || (fault != "postcopy"
                            && step.state == "COPYING"
                            && step.copied_bytes > 0))
                {
                    injected = true;
                    match fault {
                        "swap" => {
                            fs::rename(&paths[1], dir.path().join("old-secondary"))?;
                            fs::create_dir(&paths[1])?;
                        }
                        "conflict" => {
                            fs::write(paths[1].join("photo.heic"), b"arrived during copy")?;
                        }
                        "source" => {
                            fs::write(paths[0].join("photo.heic"), b"changed source")?;
                        }
                        "postcopy" => {
                            fs::write(paths[1].join("photo.heic"), b"corrupted after copy")?;
                        }
                        _ => unreachable!(),
                    }
                }
                Ok(())
            });
            assert!(!report.complete, "{fault}");
            let expected = match fault {
                "capacity" => "insufficient free space",
                "swap" => "identity changed",
                "conflict" => "exists",
                "source" => "source inventory changed",
                "postcopy" => "final independent hashes",
                _ => unreachable!(),
            };
            assert!(
                report
                    .issues
                    .iter()
                    .any(|issue| issue.to_lowercase().contains(expected)),
                "{fault}: {:?}",
                report.issues
            );
            if fault == "postcopy" {
                assert_eq!(report.hash_mismatches, 1);
                assert_eq!(report.verified_files, 0);
            }
            if fault == "conflict" {
                assert_eq!(
                    fs::read(paths[1].join("photo.heic")).unwrap(),
                    b"arrived during copy"
                );
            }
            if matches!(fault, "capacity" | "swap") {
                assert_eq!(fs::read_dir(&paths[1]).unwrap().count(), 0);
            }
        }
    }

    #[test]
    fn topology_requires_distinct_known_disks() {
        let a = VolumeIdentity {
            volume_id: "a".into(),
            root_id: "1".into(),
            physical_disks: Some(vec![1]),
        };
        let mut b = VolumeIdentity {
            volume_id: "b".into(),
            root_id: "2".into(),
            physical_disks: Some(vec![2]),
        };
        assert!(volume::independent(&a, &b));
        for disks in [None, Some(vec![]), Some(vec![1]), Some(vec![1, 2])] {
            b.physical_disks = disks;
            assert!(!volume::independent(&a, &b));
        }
    }
}
