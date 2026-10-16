use iarchive_fs::{
    hash_file,
    quarantine::{self, Plan},
};
use iarchive_store::{ReviewAction, ReviewInput, Store};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::SystemTime,
};

struct Fixture {
    dir: tempfile::TempDir,
}
impl Fixture {
    fn new() -> Self {
        let fixture = Self {
            dir: tempfile::Builder::new()
                .prefix("iarchive actions % # 雪 ")
                .tempdir()
                .unwrap(),
        };
        let worker = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../image-worker")
            .canonicalize()
            .unwrap();
        let python = worker.join(if cfg!(windows) {
            ".venv/Scripts/python.exe"
        } else {
            ".venv/bin/python"
        });
        let generator = worker.parent().unwrap().join("fixtures/relationships.py");
        let output = Command::new(python)
            .arg(generator)
            .arg("--output")
            .arg(fixture.path("source"))
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        for name in ["secondary", "primary-reports", "secondary-reports"] {
            fs::create_dir(fixture.path(name)).unwrap();
        }
        fixture.ok(&[
            "import",
            "--cycle",
            "x",
            "--source",
            fixture.path("source").to_str().unwrap(),
        ]);
        let audit = fixture.run(&[
            "audit",
            "--cycle",
            "x",
            "--categories",
            "dark",
            "--worker-project",
            worker.to_str().unwrap(),
        ]);
        assert_eq!(
            audit.status.code(),
            Some(2),
            "{}",
            String::from_utf8_lossy(&audit.stderr)
        );
        let audit: Value = serde_json::from_slice(&audit.stdout).unwrap();
        assert_eq!(audit["decoded_images"], 4);
        assert_eq!(audit["decode_errors"], 0);
        let tool = worker.parent().unwrap().join(if cfg!(windows) {
            ".windows-sandbox/exiftool/exiftool.exe"
        } else {
            ".tools/exiftool/exiftool"
        });
        fixture.ok(&[
            "relationships",
            "--cycle",
            "x",
            "--exiftool",
            tool.to_str().unwrap(),
        ]);
        fixture.replica("replicate");
        fixture.flag("live.heic", ReviewAction::FlagSelected);
        fixture
    }
    fn path(&self, name: &str) -> PathBuf {
        self.dir.path().canonicalize().unwrap().join(name)
    }
    fn store(&self) -> Store {
        Store::open(&self.path("state/manifest.sqlite")).unwrap()
    }
    fn run(&self, args: &[&str]) -> std::process::Output {
        Command::new(env!("CARGO_BIN_EXE_iarchive"))
            .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
            .arg("--state-dir")
            .arg(self.path("state"))
            .arg("--json")
            .args(args)
            .output()
            .unwrap()
    }
    fn ok(&self, args: &[&str]) -> Value {
        let out = self.run(args);
        assert!(
            out.status.success(),
            "stdout: {}\nstderr: {}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
        serde_json::from_slice(&out.stdout).unwrap()
    }
    fn replica(&self, operation: &str) -> Value {
        let mut args = vec![operation, "--cycle", "x", "--secondary"];
        let secondary = self.path("secondary");
        let primary_reports = self.path("primary-reports");
        let secondary_reports = self.path("secondary-reports");
        args.extend([
            secondary.to_str().unwrap(),
            "--primary-artifacts",
            primary_reports.to_str().unwrap(),
            "--secondary-artifacts",
            secondary_reports.to_str().unwrap(),
            "--allow-unverified-devices",
        ]);
        if operation == "verify" {
            args.push("--deep");
        }
        self.ok(&args)
    }
    fn flag(&self, path: &str, action: ReviewAction) {
        let mut store = self.store();
        let view = store.review_snapshot("x").unwrap();
        let group = view
            .groups
            .iter()
            .find(|g| g.kind == "dark" && g.members.iter().any(|m| m.relative_path == path))
            .unwrap();
        let id = group
            .members
            .iter()
            .find(|m| m.relative_path == path)
            .unwrap()
            .id;
        store
            .record_review(
                "x",
                &ReviewInput {
                    fingerprint: group.fingerprint.clone(),
                    selected: if action == ReviewAction::FlagSelected {
                        vec![id]
                    } else {
                        vec![]
                    },
                    action,
                },
            )
            .unwrap();
    }
    fn preview(&self) -> Value {
        self.ok(&[
            "--dry-run",
            "quarantine",
            "--cycle",
            "x",
            "--quarantine-root",
            self.path("quarantine").to_str().unwrap(),
            "--allow-unverified-devices",
        ])
    }
    fn move_files(&self, digest: &str) -> Value {
        self.ok(&[
            "quarantine",
            "--cycle",
            "x",
            "--quarantine-root",
            self.path("quarantine").to_str().unwrap(),
            "--allow-unverified-devices",
            "--confirm",
            digest,
        ])
    }
    fn snapshot(&self, name: &str) -> BTreeMap<String, (String, SystemTime)> {
        fs::read_dir(self.path(name))
            .unwrap()
            .map(|e| {
                let path = e.unwrap().path();
                (
                    path.file_name().unwrap().to_str().unwrap().into(),
                    (
                        hash_file(&path).unwrap(),
                        fs::metadata(path).unwrap().modified().unwrap(),
                    ),
                )
            })
            .collect()
    }
    fn plan(&self, id: &str) -> Plan {
        serde_json::from_value(self.store().local_job(id).unwrap().plan).unwrap()
    }
}

#[test]
fn quarantine_restore_preserves_variants() {
    let fixture = Fixture::new();
    fixture.flag("cropped.jpg", ReviewAction::FlagSelected);
    let before = fixture.snapshot("source");
    let backup = fixture.snapshot("secondary");
    let preview = fixture.preview();
    assert_eq!(preview["flagged_count"], 2);
    assert_eq!(preview["files"].as_array().unwrap().len(), 4);
    assert_eq!(fixture.snapshot("source"), before);
    assert!(!fixture.path("quarantine").exists());
    let moved = fixture.move_files(preview["confirmation"].as_str().unwrap());
    let id = moved["quarantine_id"].as_str().unwrap();
    assert_eq!(fixture.snapshot("source").len(), 1);
    assert_eq!(fixture.snapshot("secondary"), backup);
    let verified = fixture.replica("verify");
    assert_eq!(verified["verified_files"], 5);
    fixture.ok(&[
        "export-manifest",
        "--cycle",
        "x",
        "--output",
        fixture.path("inventory.jsonl").to_str().unwrap(),
    ]);
    let text = fs::read_to_string(fixture.path("inventory.jsonl")).unwrap();
    assert!(text.contains(id));
    assert!(text.contains("adjusted image"));
    let indexed = fixture.run(&["index", "--cycle", "x"]);
    assert_eq!(indexed.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&indexed.stderr).contains("restore quarantined files"));
    let preview = fixture.ok(&["--dry-run", "restore", "--quarantine-id", id]);
    assert_eq!(preview["quarantined_files"], 4);
    fs::rename(fixture.path("secondary"), fixture.path("disconnected")).unwrap();
    fixture.ok(&["restore", "--quarantine-id", id]);
    fixture.ok(&["restore", "--quarantine-id", id]);
    assert_eq!(fixture.snapshot("source"), before);
    assert_eq!(fixture.snapshot("disconnected"), backup);
    assert!(fixture.store().active_jobs("x").unwrap().is_empty());
}

#[test]
fn associated_keeps_and_drift_block() {
    let fixture = Fixture::new();
    fixture.flag("cropped.jpg", ReviewAction::FlagSelected);
    let before = fixture.snapshot("source");
    let preview = fixture.preview();
    fixture.flag("original.jpg", ReviewAction::KeepAll);
    let refused = fixture.run(&[
        "--dry-run",
        "quarantine",
        "--cycle",
        "x",
        "--allow-unverified-devices",
    ]);
    assert_eq!(refused.status.code(), Some(2));
    let view: Value = serde_json::from_slice(&refused.stdout).unwrap();
    assert!(view["flags"].as_array().unwrap().iter().any(|f| {
        f["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b.as_str().unwrap().contains("associated member"))
    }));
    let refused = fixture.run(&[
        "quarantine",
        "--cycle",
        "x",
        "--allow-unverified-devices",
        "--confirm",
        preview["confirmation"].as_str().unwrap(),
    ]);
    assert_eq!(refused.status.code(), Some(1));
    assert!(!fixture.path("quarantine").exists());
    assert_eq!(fixture.snapshot("source"), before);
    fixture.flag("original.jpg", ReviewAction::Reset);
    let old = fixture.preview();
    fixture.flag("cropped.jpg", ReviewAction::Defer);
    let refused = fixture.run(&[
        "quarantine",
        "--cycle",
        "x",
        "--quarantine-root",
        fixture.path("quarantine").to_str().unwrap(),
        "--allow-unverified-devices",
        "--confirm",
        old["confirmation"].as_str().unwrap(),
    ]);
    assert_eq!(refused.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("confirmation differs"));
    let path = fixture.path("secondary/portrait.jpg");
    let bytes = fs::read(&path).unwrap();
    let time = fs::metadata(&path).unwrap().modified().unwrap();
    fs::write(&path, vec![0; bytes.len()]).unwrap();
    fs::File::options()
        .write(true)
        .open(&path)
        .unwrap()
        .set_modified(time)
        .unwrap();
    let refused = fixture.run(&[
        "--dry-run",
        "quarantine",
        "--cycle",
        "x",
        "--allow-unverified-devices",
    ]);
    assert_eq!(refused.status.code(), Some(2));
    let view: Value = serde_json::from_slice(&refused.stdout).unwrap();
    assert_eq!(view["verification"]["local_replica"], "NOT VERIFIED");
    assert_eq!(fixture.snapshot("source"), before);
}

#[test]
fn interrupted_moves_reconcile_without_overwrites() {
    let fixture = Fixture::new();
    let before = fixture.snapshot("source");
    let backup = fixture.snapshot("secondary");
    let preview = fixture.preview();
    let digest = preview["confirmation"].as_str().unwrap();
    let result = fixture.move_files(digest);
    let id = result["quarantine_id"].as_str().unwrap();
    let plan = fixture.plan(id);
    // Interrupt after the rename but before the progress journal update.
    assert!(
        quarantine::execute(&plan, true, &mut |_| anyhow::bail!("synthetic power loss")).is_err()
    );
    fixture
        .store()
        .set_job_state(id, "INCOMPLETE", Some("synthetic interruption"))
        .unwrap();
    let review = fixture.store().review_snapshot("x").unwrap();
    let affected: Vec<_> = review
        .groups
        .iter()
        .flat_map(|group| &group.members)
        .filter(|member| {
            plan.files
                .iter()
                .any(|file| file.relative_path == member.relative_path)
        })
        .collect();
    assert!(!affected.is_empty());
    assert!(
        affected
            .iter()
            .all(|member| member.location == "needs journal reconciliation")
    );
    assert_eq!(
        quarantine::positions(&plan)
            .unwrap()
            .iter()
            .filter(|q| **q)
            .count(),
        1
    );
    fixture.ok(&[
        "quarantine",
        "--cycle",
        "x",
        "--resume",
        id,
        "--allow-unverified-devices",
        "--confirm",
        digest,
    ]);
    assert!(quarantine::positions(&plan).unwrap().iter().all(|q| *q));
    assert!(
        quarantine::execute(&plan, true, &mut |_| anyhow::bail!("synthetic power loss")).is_err()
    );
    fixture
        .store()
        .set_job_state(id, "INCOMPLETE", Some("synthetic restore interruption"))
        .unwrap();
    let remaining = plan
        .files
        .iter()
        .zip(quarantine::positions(&plan).unwrap())
        .find(|(_, q)| *q)
        .unwrap()
        .0;
    let conflict = fixture.path("source").join(&remaining.relative_path);
    fs::write(&conflict, b"retain this conflicting file").unwrap();
    let refused = fixture.run(&["restore", "--quarantine-id", id]);
    assert_eq!(refused.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("both locations"));
    assert_eq!(
        fs::read(&conflict).unwrap(),
        b"retain this conflicting file"
    );
    fs::remove_file(conflict).unwrap();
    fixture.ok(&["restore", "--quarantine-id", id]);
    assert_eq!(fixture.snapshot("source"), before);
    assert_eq!(fixture.snapshot("secondary"), backup);
}

#[test]
fn later_cycles_preserve_previous_archives() {
    let fixture = Fixture::new();
    let before = fixture.snapshot("source");
    let backup = fixture.snapshot("secondary");
    let old = fixture.store().review_snapshot("x").unwrap();
    let old_fingerprints: Vec<_> = old.groups.iter().map(|g| g.fingerprint.clone()).collect();
    fs::create_dir(fixture.path("next-media")).unwrap();
    fs::copy(
        fixture.path("source/portrait.jpg"),
        fixture.path("next-media/portrait-copy.jpg"),
    )
    .unwrap();
    fixture.ok(&[
        "import",
        "--cycle",
        "next",
        "--source",
        fixture.path("next-media").to_str().unwrap(),
    ]);
    fixture.ok(&["audit", "--cycle", "next", "--categories", "exact"]);
    let exact = fixture.store().review_snapshot("next").unwrap();
    assert!(exact.groups.iter().any(|g| g.kind == "exact"
        && g.members.iter().any(|m| m.cycle == "x")
        && g.members.iter().any(|m| m.cycle == "next")));
    fixture.ok(&["audit", "--cycle", "next", "--categories", "dark"]);
    let repeated = fixture.ok(&["audit", "--cycle", "next", "--categories", "dark"]);
    assert_eq!(repeated["analysis_cache_hits"], 1);
    assert_eq!(repeated["decoded_images"], 0);
    assert_eq!(repeated["analyzed_images"], 1);
    let old = fixture.store().review_snapshot("x").unwrap();
    assert_eq!(
        old.groups
            .iter()
            .map(|g| g.fingerprint.clone())
            .collect::<Vec<_>>(),
        old_fingerprints
    );
    assert!(old.groups.iter().any(|g| {
        g.decision
            .as_ref()
            .is_some_and(|d| d.action == ReviewAction::FlagSelected)
    }));
    assert_eq!(fixture.snapshot("source"), before);
    assert_eq!(fixture.snapshot("secondary"), backup);
    let cycles = fixture.ok(&["cycles"]);
    assert_eq!(cycles["cycles"].as_array().unwrap().len(), 2);
    let overlap = fixture.run(&[
        "import",
        "--cycle",
        "overlap",
        "--source",
        fixture.path("source").to_str().unwrap(),
    ]);
    assert_eq!(overlap.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&overlap.stderr).contains("non-overlapping media"));
}
