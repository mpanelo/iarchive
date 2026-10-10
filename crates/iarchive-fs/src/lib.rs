use anyhow::{Context, Result, ensure};
use iarchive_core::{
    FileFact, FileKind, StorageProvider, classify, collision_key, is_link, normalized_relative,
};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{BufReader, Read},
    path::Path,
};

pub struct LocalStorage;

impl StorageProvider for LocalStorage {
    fn visit(&self, root: &Path, visitor: &mut dyn FnMut(FileFact) -> Result<()>) -> Result<()> {
        let mut walker = walkdir::WalkDir::new(root)
            .follow_links(false)
            .follow_root_links(false)
            .into_iter();
        while let Some(entry) = walker.next() {
            let entry = entry?;
            if entry.depth() == 0 {
                continue;
            }
            let metadata = std::fs::symlink_metadata(entry.path());
            if metadata.as_ref().is_ok_and(|m| is_link(m) && m.is_dir()) {
                walker.skip_current_dir();
            }
            if metadata.as_ref().is_ok_and(|m| m.is_dir() && !is_link(m)) {
                continue;
            }
            // Do not silently lose non-portable/non-Unicode entries through lossy path conversion.
            let relative = normalized_relative(entry.path().strip_prefix(root)?)
                .with_context(|| format!("Cannot inventory path {}", entry.path().display()))?;
            let mut warnings = portable_warnings(&relative);
            let (kind, size, time, error) = match metadata {
                Ok(m) if is_link(&m) => (
                    FileKind::ExcludedLink,
                    0,
                    String::new(),
                    Some(
                        "Symlink/reparse point excluded; bytes outside root are never read".into(),
                    ),
                ),
                Ok(m) if m.is_file() => {
                    let kind = classify(entry.path());
                    let error = if kind == FileKind::Partial {
                        Some("Incomplete/temp file; cannot count as a completed original".into())
                    } else {
                        None
                    };
                    if m.len() == 0 {
                        warnings.push("Zero-byte file; content validity is unknown".into());
                    }
                    (
                        kind,
                        m.len(),
                        mtime_ns(&m)
                            .with_context(|| format!("Timestamp unavailable for {relative}"))?,
                        error,
                    )
                }
                Ok(_) => (
                    FileKind::Unknown,
                    0,
                    String::new(),
                    Some("Non-regular filesystem entry; not read".into()),
                ),
                Err(e) => (
                    classify(entry.path()),
                    0,
                    String::new(),
                    Some(format!("Metadata unreadable: {e}")),
                ),
            };
            visitor(FileFact {
                path_key: collision_key(&relative),
                relative_path: relative,
                kind,
                byte_count: size,
                mtime_ns: time,
                sha256: None,
                error,
                warnings,
            })?;
        }
        Ok(())
    }
}

fn portable_warnings(relative: &str) -> Vec<String> {
    let mut warnings = Vec::new();
    if relative.chars().count() > 240 {
        warnings.push("Long path: Windows long-path support may be required".into());
    }
    for part in relative.split('/') {
        let stem = part.split('.').next().unwrap_or("").to_uppercase();
        let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.as_bytes()[3].is_ascii_digit()
                && stem.as_bytes()[3] != b'0');
        if reserved
            || part.ends_with([' ', '.'])
            || part.contains(['<', '>', '"', '|', '?', '*'])
            || part.chars().any(char::is_control)
        {
            warnings.push(format!("Non-portable Windows filename: {part}"));
        }
    }
    warnings
}

pub fn mtime_ns(metadata: &std::fs::Metadata) -> Result<String> {
    let time = metadata.modified()?;
    Ok(match time.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_nanos().to_string(),
        Err(e) => format!("-{}", e.duration().as_nanos()),
    })
}

pub fn hash_file(path: &Path) -> Result<String> {
    let before = std::fs::symlink_metadata(path)?;
    ensure!(
        before.is_file() && !is_link(&before),
        "hash input must be a regular, non-linked file"
    );
    let file = File::open(path)?;
    let opened = file.metadata()?;
    ensure!(
        before.len() == opened.len() && mtime_ns(&before)? == mtime_ns(&opened)?,
        "file changed while opening"
    );
    let mut reader = BufReader::with_capacity(1024 * 1024, file);
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let n = reader.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    let after = std::fs::symlink_metadata(path)?;
    ensure!(
        !is_link(&after) && before.len() == after.len() && mtime_ns(&before)? == mtime_ns(&after)?,
        "file changed while hashing"
    );
    Ok(format!("{:x}", hash.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hash_is_read_only_and_streaming() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("space 旅行.MOV");
        std::fs::write(&path, b"abc").unwrap();
        let before = std::fs::metadata(&path).unwrap().modified().unwrap();
        assert_eq!(
            hash_file(&path).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            std::fs::metadata(&path).unwrap().modified().unwrap(),
            before
        );
        assert_eq!(std::fs::read(&path).unwrap(), b"abc");
    }
}
