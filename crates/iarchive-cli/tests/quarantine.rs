use iarchive_core::{ALGORITHM_VERSION, ImageFeatures};
use iarchive_store::{ReviewAction, ReviewInput, Store};
use serde_json::Value;
use std::{collections::BTreeMap, fs, path::PathBuf, process::Command, time::SystemTime};

struct Fixture {
    dir: tempfile::TempDir,
    file: i64,
}

impl Fixture {
    fn new() -> Self {
        let mut fixture = Self {
            dir: tempfile::Builder::new()
                .prefix("iarchive preview % # ")
                .tempdir()
                .unwrap(),
            file: 0,
        };
        for name in [
            "source",
            "secondary",
            "primary-reports",
            "secondary-reports",
            "other",
        ] {
            fs::create_dir(fixture.path(name)).unwrap();
        }
        fs::write(fixture.path("source/photo.jpg"), b"synthetic photo").unwrap();
        fs::write(fixture.path("source/motion.mov"), b"synthetic motion").unwrap();
        fixture.ok(&[
            "import",
            "--cycle",
            "x",
            "--source",
            fixture.path("source").to_str().unwrap(),
        ]);
        fixture.ok(&["index", "--cycle", "x", "--deep"]);
        fixture.ok(&[
            "replicate",
            "--cycle",
            "x",
            "--secondary",
            fixture.path("secondary").to_str().unwrap(),
            "--primary-artifacts",
            fixture.path("primary-reports").to_str().unwrap(),
            "--secondary-artifacts",
            fixture.path("secondary-reports").to_str().unwrap(),
            "--allow-unverified-devices",
        ]);
        let mut store = fixture.store();
        let (id, fact) = store.image_files("x").unwrap().remove(0);
        fixture.file = id;
        let features = ImageFeatures {
            algorithm_version: ALGORITHM_VERSION.into(),
            phash: "0".repeat(16),
            width: 10,
            height: 10,
            p50: 0.,
            p95: 0.,
            near_black_share: 1.,
            edge_energy: 0.,
            sharpness: 0.,
            dark_candidate_score: 1.,
            dark_reason: "synthetic analysis fixture".into(),
            thumbnail: "unused.jpg".into(),
        };
        store
            .save_analysis(
                id,
                fact.sha256.as_deref().unwrap(),
                &Ok(features),
                ALGORITHM_VERSION,
            )
            .unwrap();
        for kind in ["dark", "similar"] {
            store
                .candidate("x", kind, "synthetic candidate", &[id])
                .unwrap();
        }
        drop(store);
        fixture.decide("x", 0, ReviewAction::FlagSelected);
        fixture.decide("x", 1, ReviewAction::FlagSelected);
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
            .arg("--state-dir")
            .arg(self.path("state"))
            .arg("--json")
            .args(args)
            .output()
            .unwrap()
    }
    fn ok(&self, args: &[&str]) {
        let output = self.run(args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    fn decide(&self, cycle: &str, group: usize, action: ReviewAction) {
        let mut store = self.store();
        let selected = if action == ReviewAction::FlagSelected {
            vec![self.file]
        } else {
            vec![]
        };
        store
            .record_review(
                cycle,
                &ReviewInput {
                    fingerprint: store.review_snapshot(cycle).unwrap().groups[group]
                        .fingerprint
                        .clone(),
                    action,
                    selected,
                },
            )
            .unwrap();
    }
    fn preview(&self) -> Value {
        let output = self.run(&["--dry-run", "quarantine", "--cycle", "x"]);
        assert_eq!(
            output.status.code(),
            Some(2),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }
    fn snapshot(&self) -> BTreeMap<PathBuf, (String, SystemTime)> {
        fn visit(path: PathBuf, files: &mut BTreeMap<PathBuf, (String, SystemTime)>) {
            for entry in fs::read_dir(path).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    visit(path, files);
                } else {
                    files.insert(
                        path.clone(),
                        (
                            iarchive_fs::hash_file(&path).unwrap(),
                            fs::metadata(path).unwrap().modified().unwrap(),
                        ),
                    );
                }
            }
        }
        let mut files = BTreeMap::new();
        visit(self.path(""), &mut files);
        files
    }
}

fn contains(value: &Value, text: &str) -> bool {
    value
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v.as_str().unwrap().contains(text))
}

#[test]
fn preview_preserves_files_and_decisions() {
    let fixture = Fixture::new();
    let before = fixture.snapshot();
    let preview = fixture.preview();
    assert_eq!(preview["flagged_count"], 1);
    assert_eq!(
        preview["flags"][0]["review_groups"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        preview["verification"]["local_replica"],
        "LOCAL BYTES VERIFIED"
    );
    assert_eq!(preview["verification"]["verified_files"], 2);
    assert_eq!(
        preview["verification"]["independent_physical_drives"],
        "NOT VERIFIED"
    );
    assert_eq!(preview["verification"]["cloud_coverage"], "NOT VERIFIED");
    assert_eq!(preview["will_write_media"], false);
    assert_eq!(preview["will_write_state"], false);
    assert!(contains(
        &preview["flags"][0]["blockers"],
        "relationships are unknown"
    ));
    assert_eq!(fixture.snapshot(), before);
    let output = fixture.run(&["quarantine", "--cycle", "x"]);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("quarantine requires --confirm"));
    assert_eq!(fixture.snapshot(), before);

    fixture.decide("x", 1, ReviewAction::KeepAll);
    assert!(contains(
        &fixture.preview()["flags"][0]["blockers"],
        "Conflicting Keep"
    ));
    fixture.decide("x", 1, ReviewAction::Reset);
    assert!(!contains(
        &fixture.preview()["flags"][0]["blockers"],
        "Conflicting Keep"
    ));

    fixture.ok(&[
        "import",
        "--cycle",
        "other",
        "--source",
        fixture.path("other").to_str().unwrap(),
    ]);
    fixture
        .store()
        .candidate("other", "exact", "cross-cycle member", &[fixture.file])
        .unwrap();
    fixture.decide("other", 0, ReviewAction::KeepAll);
    assert!(contains(
        &fixture.preview()["flags"][0]["blockers"],
        "Conflicting Keep"
    ));
    fixture.decide("other", 0, ReviewAction::Reset);
    fixture.decide("x", 0, ReviewAction::Defer);
    assert_eq!(fixture.preview()["flagged_count"], 0);
    fixture.decide("x", 0, ReviewAction::FlagSelected);
    fixture.ok(&["index", "--cycle", "x", "--deep"]);
    assert_eq!(
        fixture.preview()["flagged_count"],
        0,
        "stale flags must not survive a new scan"
    );
}

#[test]
fn preview_rechecks_entire_backup() {
    for (scenario, counter, issue) in [
        (
            "source",
            "hash_mismatches",
            "Source facts or replica bytes changed",
        ),
        (
            "secondary",
            "hash_mismatches",
            "Source facts or replica bytes changed",
        ),
        ("missing", "missing_files", "Missing source or secondary"),
        ("extra", "extra_files", "Extra secondary entry"),
        ("partial", "file_errors", "Incomplete/temp file"),
        (
            "disconnected",
            "verified_files",
            "bound secondary root is unavailable",
        ),
        (
            "rebound",
            "verified_files",
            "bound secondary root/volume changed",
        ),
    ] {
        let fixture = Fixture::new();
        match scenario {
            "source" | "secondary" => {
                let path = fixture.path(&format!("{scenario}/motion.mov"));
                let modified = fs::metadata(&path).unwrap().modified().unwrap();
                fs::write(&path, b"different motion").unwrap();
                fs::File::options()
                    .write(true)
                    .open(path)
                    .unwrap()
                    .set_modified(modified)
                    .unwrap();
            }
            "missing" => fs::remove_file(fixture.path("secondary/motion.mov")).unwrap(),
            "extra" => fs::write(fixture.path("secondary/extra.jpg"), b"extra").unwrap(),
            "partial" => {
                fs::write(fixture.path("secondary/incomplete.partial"), b"partial").unwrap()
            }
            "disconnected" => {
                fs::rename(fixture.path("secondary"), fixture.path("unavailable")).unwrap()
            }
            "rebound" => {
                fs::rename(fixture.path("secondary"), fixture.path("old-secondary")).unwrap();
                fs::create_dir(fixture.path("secondary")).unwrap();
            }
            _ => unreachable!(),
        }
        let before = fixture.snapshot();
        let preview = fixture.preview();
        assert_eq!(
            preview["verification"]["local_replica"], "NOT VERIFIED",
            "{scenario}"
        );
        assert!(!preview["verification"]["complete"].as_bool().unwrap());
        if matches!(scenario, "disconnected" | "rebound") {
            assert_eq!(preview["verification"][counter], 0);
            assert!(
                contains(&preview["verification"]["issues"], issue),
                "{scenario}"
            );
        } else {
            assert!(
                preview["verification"][counter].as_u64().unwrap() > 0,
                "{scenario}"
            );
            assert!(
                contains(&preview["verification"]["issues"], issue),
                "{scenario}"
            );
        }
        assert_eq!(fixture.snapshot(), before, "{scenario}");
    }
}

#[test]
fn preview_requires_idle_bound_state() {
    let fixture = Fixture::new();
    let lock = fs::File::options()
        .read(true)
        .write(true)
        .open(fixture.path("state/writer.lock"))
        .unwrap();
    fs2::FileExt::try_lock_exclusive(&lock).unwrap();
    let output = fixture.run(&["--dry-run", "quarantine", "--cycle", "x"]);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("another iarchive operation"));
    drop(lock);
    for suffix in ["-wal", "-journal"] {
        let journal = fixture.path(&format!("state/manifest.sqlite{suffix}"));
        fs::write(&journal, b"pending journal fixture").unwrap();
        let before = fixture.snapshot();
        let output = fixture.run(&["--dry-run", "quarantine", "--cycle", "x"]);
        assert_eq!(output.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&output.stderr).contains("pending SQLite journal"));
        assert_eq!(fixture.snapshot(), before);
        fs::remove_file(journal).unwrap();
    }
    fixture
        .store()
        .connection
        .execute("DELETE FROM replica_binding WHERE cycle_id='x'", [])
        .unwrap();
    assert!(contains(
        &fixture.preview()["blockers"],
        "No frozen secondary replica"
    ));
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(fixture.path("state"), fixture.path("linked-state")).unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_iarchive"))
            .arg("--state-dir")
            .arg(fixture.path("linked-state"))
            .args(["--dry-run", "quarantine", "--cycle", "x"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("directory is unavailable or linked")
        );
    }
}

#[test]
fn unsafe_review_members_remain_blocked() {
    for scenario in ["missing", "decode", "unknown", "facts", "foreign"] {
        let mut fixture = Fixture::new();
        if scenario == "foreign" {
            fs::write(
                fixture.path("other/foreign.jpg"),
                b"synthetic foreign photo",
            )
            .unwrap();
            fixture.ok(&[
                "import",
                "--cycle",
                "other",
                "--source",
                fixture.path("other").to_str().unwrap(),
            ]);
            fixture.ok(&["index", "--cycle", "other", "--deep"]);
            fixture.file = fixture.store().image_files("other").unwrap()[0].0;
            fixture
                .store()
                .candidate("x", "dark", "foreign member", &[fixture.file])
                .unwrap();
            fixture.decide("x", 1, ReviewAction::FlagSelected);
        } else {
            let store = fixture.store();
            let sql = match scenario {
                "missing" => "UPDATE file_item SET present=0 WHERE id=?1",
                "decode" => {
                    "UPDATE analysis SET error='synthetic decode failure',features_json=NULL WHERE file_id=?1"
                }
                "unknown" => "UPDATE file_item SET kind='unknown' WHERE id=?1",
                "facts" => "UPDATE file_item SET byte_count=byte_count+1 WHERE id=?1",
                _ => unreachable!(),
            };
            store.connection.execute(sql, [fixture.file]).unwrap();
            drop(store);
            fixture.decide("x", 0, ReviewAction::FlagSelected);
        }
        let before = fixture.snapshot();
        let preview = fixture.preview();
        // Current bytes still match; the reviewed record itself is unsafe.
        assert_eq!(
            preview["verification"]["local_replica"],
            "LOCAL BYTES VERIFIED"
        );
        let flag = preview["flags"]
            .as_array()
            .unwrap()
            .iter()
            .find(|f| f["file_id"] == fixture.file)
            .unwrap();
        let reason = match scenario {
            "foreign" => "belongs to another cycle",
            "facts" => "do not match the frozen backup",
            _ => "missing, unreadable, unsupported",
        };
        assert!(contains(&flag["blockers"], reason), "{scenario}");
        if scenario == "missing" {
            assert!(contains(
                &preview["blockers"],
                "Recorded inventory is incomplete"
            ));
        }
        assert_eq!(fixture.snapshot(), before, "{scenario}");
    }
}
