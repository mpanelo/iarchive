use serde_json::Value;
use std::{fs, path::Path, process::Command};

struct Fixture {
    dir: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        let fixture = Self {
            dir: tempfile::tempdir().unwrap(),
        };
        for name in [
            "source",
            "secondary",
            "primary-reports",
            "secondary-reports",
        ] {
            fs::create_dir(fixture.path(name)).unwrap();
        }
        fs::create_dir(fixture.path("source/nested")).unwrap();
        for (name, bytes) in [
            ("photo.heic", b"synthetic photo".as_slice()),
            ("nested/motion.MOV", b"synthetic motion"),
            ("edit.aae", b"edit"),
            ("unknown.xyz", b"keep unknown"),
            ("empty.bin", b""),
        ] {
            fs::write(fixture.path(&format!("source/{name}")), bytes).unwrap();
        }
        fixture.ok(&[
            "import",
            "--source",
            fixture.path("source").to_str().unwrap(),
            "--cycle",
            "x",
        ]);
        fixture
    }

    fn path(&self, name: &str) -> std::path::PathBuf {
        self.dir.path().canonicalize().unwrap().join(name)
    }

    fn run(&self, args: &[&str]) -> std::process::Output {
        Command::new(env!("CARGO_BIN_EXE_iarchive"))
            .arg("--state-dir")
            .arg(self.path("state"))
            .arg("--json")
            .args(args)
            .output()
            .unwrap()
    }

    fn ok(&self, args: &[&str]) -> Value {
        let output = self.run(args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }

    fn replica(&self, command: &str, extra: &[&str]) -> std::process::Output {
        let secondary = self.path("secondary");
        let primary_reports = self.path("primary-reports");
        let secondary_reports = self.path("secondary-reports");
        let mut args = vec![
            command,
            "--cycle",
            "x",
            "--secondary",
            secondary.to_str().unwrap(),
            "--primary-artifacts",
            primary_reports.to_str().unwrap(),
            "--secondary-artifacts",
            secondary_reports.to_str().unwrap(),
            "--allow-unverified-devices",
        ];
        let mut index = 0;
        while index < extra.len() {
            if let Some(position) = args.iter().position(|arg| *arg == extra[index]) {
                args[position + 1] = extra[index + 1];
                index += 2;
            } else {
                args.push(extra[index]);
                index += 1;
            }
        }
        self.run(&args)
    }
}

fn json(output: std::process::Output, code: i32) -> Value {
    assert_eq!(
        output.status.code(),
        Some(code),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

fn files(root: &Path) -> Vec<std::path::PathBuf> {
    fs::read_dir(root)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect()
}

#[test]
fn replicate_preserves_all_variants() {
    let fixture = Fixture::new();
    let first = json(fixture.replica("replicate", &[]), 0);
    assert_eq!(first["copied_files"], 5);
    assert_eq!(first["verified_files"], 5);
    assert_eq!(first["local_replica"], "LOCAL BYTES VERIFIED");
    assert_eq!(first["independent_physical_drives"], "NOT VERIFIED");
    assert_eq!(first["cloud_coverage"], "NOT VERIFIED");
    for name in [
        "photo.heic",
        "nested/motion.MOV",
        "edit.aae",
        "unknown.xyz",
        "empty.bin",
    ] {
        let source = fixture.path(&format!("source/{name}"));
        let target = fixture.path(&format!("secondary/{name}"));
        assert_eq!(fs::read(&source).unwrap(), fs::read(&target).unwrap());
        assert_eq!(
            fs::metadata(source).unwrap().modified().unwrap(),
            fs::metadata(target).unwrap().modified().unwrap()
        );
    }
    let bundles = first["artifact_bundles"].as_array().unwrap();
    assert_eq!(bundles.len(), 3);
    for bundle in bundles {
        let root = Path::new(bundle.as_str().unwrap());
        let manifest = fs::read(root.join("manifest.jsonl")).unwrap();
        assert_eq!(
            manifest,
            fs::read(Path::new(bundles[0].as_str().unwrap()).join("manifest.jsonl")).unwrap()
        );
        assert_eq!(String::from_utf8(manifest).unwrap().lines().count(), 6);
        let backup = iarchive_store::Store::open_readonly(&root.join("manifest.sqlite")).unwrap();
        assert_eq!(
            backup.root("x").unwrap(),
            fixture.path("source").canonicalize().unwrap()
        );
        assert_eq!(backup.status("x").unwrap().stage, "PUBLISHING");
        assert_eq!(
            iarchive_fs::hash_file(&root.join("manifest.sqlite")).unwrap(),
            first["database_backup_sha256"].as_str().unwrap()
        );
        assert!(root.join("summary.json").is_file());
    }
    let second = json(fixture.replica("replicate", &[]), 0);
    assert_eq!(second["copied_files"], 0);
    assert_eq!(second["existing_files"], 5);
    let verified = json(fixture.replica("verify", &["--deep"]), 0);
    assert_eq!(verified["verified_files"], 5);
    assert_eq!(verified["copied_files"], 0);
    let status = fixture.ok(&["status", "--cycle", "x"]);
    assert_eq!(status["replica_state"], "VERIFIED");
    assert_eq!(status["local_replica"], "NOT VERIFIED"); // saved state is not a live check
    // Either drive's standalone backup restores enough state to independently verify again.
    for (number, bundle) in bundles.iter().skip(1).enumerate() {
        let recovered = fixture.path(&format!("recovered-{number}"));
        fs::create_dir(&recovered).unwrap();
        fs::copy(
            Path::new(bundle.as_str().unwrap()).join("manifest.sqlite"),
            recovered.join("manifest.sqlite"),
        )
        .unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_iarchive"))
            .arg("--state-dir")
            .arg(&recovered)
            .arg("--json")
            .args([
                "verify",
                "--deep",
                "--cycle",
                "x",
                "--allow-unverified-devices",
            ])
            .arg("--secondary")
            .arg(fixture.path("secondary"))
            .arg("--primary-artifacts")
            .arg(fixture.path("primary-reports"))
            .arg("--secondary-artifacts")
            .arg(fixture.path("secondary-reports"))
            .output()
            .unwrap();
        assert_eq!(json(output, 0)["verified_files"], 5);
    }
}

#[test]
fn mismatches_invalidate_saved_verification() {
    for change in [
        "corrupt",
        "missing",
        "extra",
        "source",
        "partial",
        "unavailable",
        "both",
    ] {
        let fixture = Fixture::new();
        json(fixture.replica("replicate", &[]), 0);
        match change {
            "corrupt" => {
                fs::write(fixture.path("secondary/photo.heic"), b"synthetic fault").unwrap()
            }
            "missing" => fs::remove_file(fixture.path("secondary/photo.heic")).unwrap(),
            "extra" => fs::write(fixture.path("secondary/extra.bin"), b"extra").unwrap(),
            "source" => fs::write(fixture.path("source/photo.heic"), b"changed source").unwrap(),
            "partial" => fs::write(fixture.path("source/download.partial"), b"unfinished").unwrap(),
            "unavailable" => {
                fs::rename(fixture.path("secondary"), fixture.path("disconnected")).unwrap()
            }
            "both" => {
                for root in ["source", "secondary"] {
                    fs::write(
                        fixture.path(&format!("{root}/photo.heic")),
                        b"same changed bytes",
                    )
                    .unwrap();
                }
            }
            _ => unreachable!(),
        }
        let report = json(fixture.replica("verify", &["--deep"]), 2);
        assert_eq!(report["complete"], false, "{change}");
        assert_eq!(report["local_replica"], "NOT VERIFIED", "{change}");
        assert!(!report["issues"].as_array().unwrap().is_empty(), "{change}");
        assert_eq!(
            fixture.ok(&["status", "--cycle", "x"])["replica_state"],
            "INCOMPLETE"
        );
    }
}

#[test]
fn failed_publication_invalidates_completion() {
    let fixture = Fixture::new();
    fs::write(
        fixture.path("state/replica"),
        b"synthetic artifact obstruction",
    )
    .unwrap();
    let report = json(fixture.replica("replicate", &[]), 2);
    assert_eq!(report["copied_files"], 5);
    assert_eq!(report["complete"], false);
    assert!(report["issues"].as_array().unwrap().iter().any(|issue| {
        issue
            .as_str()
            .unwrap()
            .contains("Artifact publication failed")
    }));
    assert_eq!(
        fixture.ok(&["status", "--cycle", "x"])["replica_state"],
        "INCOMPLETE"
    );
    assert_eq!(
        fs::read(fixture.path("state/replica")).unwrap(),
        b"synthetic artifact obstruction"
    );
}

#[test]
fn conflicts_never_overwrite_destination() {
    let fixture = Fixture::new();
    fs::write(fixture.path("secondary/photo.heic"), b"existing conflict").unwrap();
    let report = json(fixture.replica("replicate", &[]), 2);
    assert_eq!(report["copied_files"], 0);
    assert_eq!(report["hash_mismatches"], 1);
    assert_eq!(
        fs::read(fixture.path("secondary/photo.heic")).unwrap(),
        b"existing conflict"
    );
    assert_eq!(files(&fixture.path("secondary")).len(), 1);
}

#[test]
fn dry_run_does_not_write() {
    let fixture = Fixture::new();
    let state_before = fs::read(fixture.path("state/manifest.sqlite")).unwrap();
    let report = json(fixture.replica("replicate", &["--dry-run"]), 0);
    assert_eq!(report["planned_files"], 5);
    assert_eq!(report["complete"], false);
    assert_eq!(files(&fixture.path("secondary")).len(), 0);
    assert_eq!(files(&fixture.path("primary-reports")).len(), 0);
    assert_eq!(files(&fixture.path("secondary-reports")).len(), 0);
    assert_eq!(
        fs::read(fixture.path("state/manifest.sqlite")).unwrap(),
        state_before
    );
    assert!(!fixture.replica("verify", &[]).status.success());
}

#[test]
fn roots_and_artifacts_cannot_overlap() {
    let fixture = Fixture::new();
    for (flag, path) in [
        ("--secondary", fixture.path("source/nested")),
        ("--primary-artifacts", fixture.path("source")),
        ("--secondary-artifacts", fixture.path("secondary")),
        ("--secondary", fixture.path("state")),
    ] {
        assert!(
            !fixture
                .replica("replicate", &[flag, path.to_str().unwrap()])
                .status
                .success()
        );
    }
    let result = fixture.run(&[
        "replicate",
        "--cycle",
        "x",
        "--secondary",
        fixture.path("secondary").to_str().unwrap(),
        "--primary-artifacts",
        fixture.path("primary-reports").to_str().unwrap(),
        "--secondary-artifacts",
        fixture.path("secondary-reports").to_str().unwrap(),
    ]);
    let report = json(result, 2);
    assert!(report["issues"].as_array().unwrap().iter().any(|item| {
        item.as_str()
            .unwrap()
            .contains("--allow-unverified-devices")
    }));
    assert_eq!(files(&fixture.path("secondary")).len(), 0);
}

#[cfg(any(unix, windows))]
#[test]
fn replica_refuses_linked_paths() {
    let fixture = Fixture::new();
    let outside = fixture.path("outside");
    fs::create_dir(&outside).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&outside, fixture.path("secondary/nested")).unwrap();
    #[cfg(windows)]
    {
        let output = Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(fixture.path("secondary/nested"))
            .arg(&outside)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let report = json(fixture.replica("replicate", &[]), 2);
    assert_eq!(report["complete"], false);
    assert_eq!(files(&outside).len(), 0);
    #[cfg(unix)]
    assert_eq!(
        fs::read_link(fixture.path("secondary/nested")).unwrap(),
        outside
    );
    #[cfg(windows)]
    fs::remove_dir(fixture.path("secondary/nested")).unwrap();
}
