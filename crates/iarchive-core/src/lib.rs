use anyhow::{Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::path::{Component, Path, PathBuf};
use unicode_normalization::UnicodeNormalization;
pub mod relationships;
pub mod similarity;

pub const SCHEMA_VERSION: u32 = 1;
pub const ALGORITHM_VERSION: &str = "iarchive-image-v1";
pub const NOT_VERIFIED: &str = "NOT VERIFIED";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FileKind {
    Image,
    Raw,
    Video,
    Sidecar,
    Unknown,
    Partial,
    ExcludedLink,
}

pub fn classify(path: &Path) -> FileKind {
    let ext = path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_lowercase();
    match ext.as_str() {
        "heic" | "heif" | "hif" | "jpg" | "jpeg" | "png" | "tif" | "tiff" | "webp" | "gif"
        | "bmp" => FileKind::Image,
        "dng" | "cr2" | "cr3" | "nef" | "arw" | "orf" | "rw2" | "raf" => FileKind::Raw,
        "mov" | "mp4" | "m4v" | "avi" | "mts" | "3gp" => FileKind::Video,
        "xmp" | "aae" | "json" => FileKind::Sidecar,
        "partial" | "tmp" | "part" => FileKind::Partial,
        _ => FileKind::Unknown,
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileFact {
    pub relative_path: String,
    pub path_key: String,
    pub kind: FileKind,
    pub byte_count: u64,
    pub mtime_ns: String,
    pub sha256: Option<String>,
    pub error: Option<String>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageFeatures {
    pub algorithm_version: String,
    pub phash: String,
    pub width: u32,
    pub height: u32,
    pub p50: f64,
    pub p95: f64,
    pub near_black_share: f64,
    pub edge_energy: f64,
    pub sharpness: f64,
    pub dark_candidate_score: f64,
    pub dark_reason: String,
    pub thumbnail: String,
}

/// Narrow replaceable worker interface. No archive or deletion policy in the worker.
pub trait ImageAnalyzer {
    fn analyze(
        &mut self,
        file_id: i64,
        input: &Path,
        root: &Path,
        cache: &Path,
    ) -> Result<ImageFeatures>;
}

/// Cloud operations deliberately expose inventory only.
pub trait CloudProvider {
    fn inventory(&self) -> Result<Vec<String>>;
}

pub struct UnavailableCloud;
impl CloudProvider for UnavailableCloud {
    fn inventory(&self) -> Result<Vec<String>> {
        bail!("cloud coverage is NOT VERIFIED; cloud integration is unavailable")
    }
}

pub trait StorageProvider {
    fn visit(&self, root: &Path, visitor: &mut dyn FnMut(FileFact) -> Result<()>) -> Result<()>;
}

pub fn validate_cycle(id: &str) -> Result<()> {
    ensure!(
        !id.is_empty()
            && id.len() <= 80
            && id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'),
        "cycle must contain 1–80 ASCII letters, digits, hyphens or underscores"
    );
    Ok(())
}

/// Convert a native relative path to a portable, forward-slash manifest path.
pub fn normalized_relative(path: &Path) -> Result<String> {
    ensure!(!path.as_os_str().is_empty(), "empty relative path");
    let mut parts = Vec::new();
    for c in path.components() {
        match c {
            Component::Normal(s) => {
                let s = s
                    .to_str()
                    .ok_or_else(|| anyhow::anyhow!("non-Unicode filename"))?;
                ensure!(
                    !s.contains('\\') && !s.contains(':') && !s.contains('\0'),
                    "non-portable path component"
                );
                parts.push(s);
            }
            _ => bail!("relative path contains traversal or a root"),
        }
    }
    Ok(parts.join("/"))
}

pub fn collision_key(relative: &str) -> String {
    relative.nfc().flat_map(char::to_lowercase).collect()
}

pub fn resolve_input(root: &Path, relative: &str) -> Result<PathBuf> {
    ensure!(
        normalized_relative(Path::new(relative))? == relative,
        "non-normalized path"
    );
    let mut current = root.to_path_buf();
    for c in Path::new(relative).components() {
        current.push(c.as_os_str());
        let metadata = std::fs::symlink_metadata(&current)?;
        ensure!(!is_link(&metadata), "symlink/reparse point refused");
    }
    let resolved = current.canonicalize()?;
    ensure!(resolved.starts_with(root), "path escapes archive root");
    ensure!(resolved.is_file(), "input is not a regular file");
    Ok(resolved)
}

pub fn is_link(metadata: &std::fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

/// Resolve an output even when it does not yet exist; existing ancestors are canonicalized.
pub fn absolute_destination(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    ensure!(
        absolute.is_absolute(),
        "drive-relative paths are not supported; use an absolute path"
    );
    ensure!(
        !absolute
            .components()
            .any(|c| matches!(c, Component::ParentDir)),
        "output path contains traversal"
    );
    let mut ancestor = absolute.as_path();
    let mut suffix = Vec::new();
    while !ancestor.exists() {
        ensure!(!ancestor.is_symlink(), "dangling symlink in output path");
        if ancestor.parent().is_none() {
            #[cfg(windows)]
            if matches!(ancestor.components().next(), Some(Component::Prefix(_))) {
                // A disconnected drive has no existing ancestor to canonicalize.
                // Retain its absolute root hint so doctor can report it unavailable.
                return Ok(absolute.clone());
            }
            bail!("output root is unavailable");
        }
        suffix.push(
            ancestor
                .file_name()
                .ok_or_else(|| anyhow::anyhow!("invalid output path"))?
                .to_owned(),
        );
        ancestor = ancestor
            .parent()
            .ok_or_else(|| anyhow::anyhow!("invalid output ancestor"))?;
    }
    let mut result = ancestor.canonicalize()?;
    for name in suffix.into_iter().rev() {
        result.push(name);
    }
    Ok(result)
}

pub fn ensure_separate(output: &Path, root: &Path) -> Result<()> {
    let output = absolute_destination(output)?;
    let root = root.canonicalize()?;
    ensure!(
        !output.starts_with(&root) && !root.starts_with(&output),
        "state/output and archive roots must be separate, non-overlapping directories"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn portable_paths() {
        assert_eq!(
            normalized_relative(Path::new("a space/旅行😀.JPG")).unwrap(),
            "a space/旅行😀.JPG"
        );
        for s in [
            "",
            "../photo",
            "/photo",
            "a/../b",
            "D:\\secret",
            "a:b",
            "a\0b",
        ] {
            assert!(normalized_relative(Path::new(s)).is_err(), "{s:?}");
        }
        #[cfg(windows)]
        assert_eq!(normalized_relative(Path::new("a\\b")).unwrap(), "a/b");
        #[cfg(not(windows))]
        assert!(normalized_relative(Path::new("a\\b")).is_err());
        assert_eq!(classify(Path::new("x.HeIc")), FileKind::Image);
        assert_eq!(collision_key("E\u{301}.JPG"), collision_key("é.jpg"));
    }
    #[test]
    fn manifest_paths_must_be_normalized() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        std::fs::create_dir(root.join("a")).unwrap();
        let file = root.join("a").join("b");
        std::fs::write(&file, b"synthetic").unwrap();
        assert_eq!(resolve_input(&root, "a/b").unwrap(), file);
        for relative in ["a\\b", "a//b", "./a/b", "a/./b", "a/b/", "a/../a/b"] {
            assert!(resolve_input(&root, relative).is_err(), "{relative:?}");
        }
    }
    #[test]
    fn outputs_cannot_overlap_media() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("photos");
        std::fs::create_dir(&source).unwrap();
        assert!(ensure_separate(&source.join("state/new"), &source).is_err());
        assert!(ensure_separate(dir.path(), &source).is_err());
        assert!(ensure_separate(&dir.path().join("state"), &source).is_ok());
    }
    #[cfg(unix)]
    #[test]
    fn refuses_link_escape() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        std::os::unix::fs::symlink("/etc/passwd", root.join("escape")).unwrap();
        assert!(resolve_input(&root, "escape").is_err());
    }
}
