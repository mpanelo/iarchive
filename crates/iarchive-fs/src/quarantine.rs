//! No-overwrite, same-volume moves with immutable plans and interruption reconciliation.
use crate::{
    hash_file,
    replica::{Binding, Relocation},
    volume::{self, VolumeIdentity},
};
use anyhow::{Context, Result, ensure};
use iarchive_core::{FileFact, is_link, normalized_relative, resolve_input, validate_cycle};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub id: String,
    pub cycle: String,
    pub confirmation: String,
    pub source: PathBuf,
    pub quarantine: PathBuf,
    pub quarantine_identity: VolumeIdentity,
    pub binding: Binding,
    pub files: Vec<FileFact>,
}

impl Plan {
    pub fn relocation(&self) -> Relocation {
        Relocation {
            root: self.quarantine.clone(),
            identity: self.quarantine_identity.clone(),
            paths: self.files.iter().map(|f| f.relative_path.clone()).collect(),
        }
    }
    fn check_roots(&self) -> Result<()> {
        validate_cycle(&self.id)?;
        validate_cycle(&self.cycle)?;
        ensure!(
            volume::identity(&self.source)? == self.binding.source,
            "primary root/volume identity changed"
        );
        ensure!(
            volume::identity(&self.quarantine)? == self.quarantine_identity
                && self.quarantine_identity.volume_id == self.binding.source.volume_id,
            "quarantine must remain on the original primary volume"
        );
        for root in [&self.source, &self.binding.secondary_root] {
            ensure!(
                !self.quarantine.starts_with(root) && !root.starts_with(&self.quarantine),
                "quarantine overlaps raw media"
            );
        }
        ensure!(
            self.quarantine.file_name() == Some(std::ffi::OsStr::new("media"))
                && self.quarantine.parent().and_then(Path::file_name)
                    == Some(std::ffi::OsStr::new(&self.id)),
            "invalid quarantine journal layout"
        );
        Ok(())
    }
    pub fn journal(&self) -> PathBuf {
        self.quarantine
            .parent()
            .expect("quarantine parent")
            .join("plan.json")
    }
    pub fn check_journal(&self) -> Result<()> {
        self.check_roots()?;
        let path = self.journal();
        let metadata = fs::symlink_metadata(&path)?;
        ensure!(
            metadata.is_file() && !is_link(&metadata),
            "quarantine journal is not a regular file"
        );
        ensure!(
            hash_file(&path)? == format!("{:x}", sha2::Sha256::digest(serde_json::to_vec(self)?)),
            "immutable quarantine journal changed"
        );
        Ok(())
    }
}

use sha2::Digest;

/// Validate every ancestor before creating a destination, then validate it again.
pub fn create_directory(path: &Path) -> Result<PathBuf> {
    ensure!(path.is_absolute(), "move directory must be absolute");
    let mut current = PathBuf::new();
    for component in path.components() {
        ensure!(
            !matches!(component, std::path::Component::ParentDir),
            "path traversal refused"
        );
        current.push(component);
        if matches!(component, std::path::Component::Prefix(_)) {
            continue;
        }
        match fs::symlink_metadata(&current) {
            Ok(metadata) => ensure!(
                metadata.is_dir() && !is_link(&metadata),
                "linked or non-directory move destination"
            ),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&current)?;
            }
            Err(e) => return Err(e.into()),
        }
    }
    volume::checked_directory(&current)
}

pub fn save_plan(plan: &Plan) -> Result<()> {
    plan.check_roots()?;
    let mut file = File::create_new(plan.journal())?;
    file.write_all(&serde_json::to_vec(plan)?)?;
    file.sync_all()?;
    sync_directory(
        plan.quarantine
            .parent()
            .context("journal directory missing")?,
    )
}

pub fn progress(plan: &Plan, state: &str, moved: usize) -> Result<()> {
    plan.check_roots()?;
    let parent = plan
        .quarantine
        .parent()
        .context("journal directory missing")?;
    let target = parent.join("progress.json");
    if let Ok(metadata) = fs::symlink_metadata(&target) {
        ensure!(
            metadata.is_file() && !is_link(&metadata),
            "linked progress journal refused"
        );
    }
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(
        &mut file,
        &serde_json::json!({"state":state,"completed_files":moved,"plan_digest":plan.confirmation}),
    )?;
    file.as_file().sync_all()?;
    file.persist(&target).map_err(|e| e.error)?;
    sync_directory(parent)
}

pub fn positions(plan: &Plan) -> Result<Vec<bool>> {
    plan.check_journal()?;
    ensure!(!plan.files.is_empty(), "quarantine plan contains no files");
    let mut paths = std::collections::BTreeSet::new();
    let expected: std::collections::BTreeMap<_, _> = plan
        .binding
        .files
        .iter()
        .map(|f| (f.relative_path.as_str(), f))
        .collect();
    plan.files
        .iter()
        .map(|fact| {
            ensure!(
                paths.insert(&fact.relative_path),
                "duplicate quarantine plan member"
            );
            ensure!(
                normalized_relative(Path::new(&fact.relative_path))? == fact.relative_path,
                "unsafe original path"
            );
            ensure!(
                expected
                    .get(fact.relative_path.as_str())
                    .is_some_and(|f| *f == fact),
                "move member is not in the frozen backup"
            );
            let primary = plan.source.join(&fact.relative_path);
            let quarantined = plan.quarantine.join(&fact.relative_path);
            let present = |root: &Path, path: &Path| -> Result<bool> {
                // Resolve parents even when the leaf is absent; no linked ancestor is allowed.
                let relative = path.strip_prefix(root)?;
                let mut component = root.to_owned();
                for part in relative.components() {
                    component.push(part);
                    match fs::symlink_metadata(&component) {
                        Ok(metadata) => ensure!(!is_link(&metadata), "linked move path refused"),
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(false),
                        Err(e) => return Err(e.into()),
                    }
                }
                Ok(true)
            };
            let a = present(&plan.source, &primary)?;
            let b = present(&plan.quarantine, &quarantined)?;
            ensure!(
                a != b,
                "move member is missing or exists in both locations: {}",
                fact.relative_path
            );
            let (root, path) = if a {
                (&plan.source, primary)
            } else {
                (&plan.quarantine, quarantined)
            };
            let resolved = resolve_input(root, &fact.relative_path)?;
            ensure!(
                resolved == path
                    && fs::metadata(&resolved)?.len() == fact.byte_count
                    && Some(hash_file(&resolved)?) == fact.sha256,
                "move member bytes changed: {}",
                fact.relative_path
            );
            Ok(b)
        })
        .collect()
}

pub fn execute(
    plan: &Plan,
    restore: bool,
    durable: &mut dyn FnMut(usize) -> Result<()>,
) -> Result<usize> {
    let locations = positions(plan)?;
    let mut completed = 0;
    for (fact, quarantined) in plan.files.iter().zip(locations) {
        if quarantined == restore {
            plan.check_roots()?;
            let (from_root, to_root) = if restore {
                (&plan.quarantine, &plan.source)
            } else {
                (&plan.source, &plan.quarantine)
            };
            let from = resolve_input(from_root, &fact.relative_path)?;
            let to = to_root.join(&fact.relative_path);
            create_directory(to.parent().context("destination parent missing")?)?;
            ensure!(
                Some(hash_file(&from)?) == fact.sha256,
                "file changed before move"
            );
            rename_no_replace(&from, &to)?;
            #[cfg(windows)]
            File::options()
                .read(true)
                .write(true)
                .open(&to)?
                .sync_all()?;
            #[cfg(not(windows))]
            File::open(&to)?.sync_all()?;
            sync_directory(from.parent().unwrap())?;
            sync_directory(to.parent().unwrap())?;
            ensure!(
                Some(hash_file(&resolve_input(to_root, &fact.relative_path)?)?) == fact.sha256,
                "file changed after move"
            );
        }
        completed += 1;
        durable(completed)?;
    }
    ensure!(
        positions(plan)?.iter().all(|q| *q != restore),
        "logical group move is incomplete"
    );
    Ok(completed)
}

fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

#[cfg(target_os = "macos")]
fn rename_no_replace(from: &Path, to: &Path) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let from = std::ffi::CString::new(from.as_os_str().as_bytes())?;
    let to = std::ffi::CString::new(to.as_os_str().as_bytes())?;
    // RENAME_EXCL makes destination conflicts atomic; ordinary rename overwrites.
    let result = unsafe { libc::renamex_np(from.as_ptr(), to.as_ptr(), libc::RENAME_EXCL) };
    ensure!(
        result == 0,
        "move refused: {}",
        std::io::Error::last_os_error()
    );
    Ok(())
}

#[cfg(target_os = "linux")]
fn rename_no_replace(from: &Path, to: &Path) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let from = std::ffi::CString::new(from.as_os_str().as_bytes())?;
    let to = std::ffi::CString::new(to.as_os_str().as_bytes())?;
    let result = unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            from.as_ptr(),
            libc::AT_FDCWD,
            to.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    ensure!(
        result == 0,
        "move refused: {}",
        std::io::Error::last_os_error()
    );
    Ok(())
}

#[cfg(windows)]
fn rename_no_replace(from: &Path, to: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    let from: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let to: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn MoveFileExW(from: *const u16, to: *const u16, flags: u32) -> i32;
    }
    // WRITE_THROUGH without REPLACE_EXISTING or COPY_ALLOWED: same-volume move only.
    let result = unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), 8) };
    ensure!(
        result != 0,
        "move refused: {}",
        std::io::Error::last_os_error()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn atomic_moves_preserve_destination_conflicts() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("source");
        let target = dir.path().join("target");
        fs::write(&source, b"source bytes").unwrap();
        fs::write(&target, b"existing bytes").unwrap();
        assert!(rename_no_replace(&source, &target).is_err());
        assert_eq!(fs::read(&source).unwrap(), b"source bytes");
        assert_eq!(fs::read(&target).unwrap(), b"existing bytes");
    }
}
