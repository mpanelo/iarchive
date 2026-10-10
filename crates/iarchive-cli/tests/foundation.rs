use std::{path::Path, process::Command};

fn run(state: &Path, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_iarchive"))
        .arg("--state-dir")
        .arg(state)
        .arg("--json")
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn import_is_read_only_idempotent_and_cloud_unverified() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("photos 旅行😀");
    std::fs::create_dir(&source).unwrap();
    let photo = source.join("IMAGE.JPG");
    std::fs::write(&photo, b"synthetic bytes").unwrap();
    let mtime = std::fs::metadata(&photo).unwrap().modified().unwrap();
    let state = dir.path().join("state");
    for _ in 0..2 {
        let result = run(
            &state,
            &[
                "import",
                "--source",
                source.to_str().unwrap(),
                "--cycle",
                "fixture",
            ],
        );
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let json: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(json["cloud_coverage"], "NOT VERIFIED");
        assert_eq!(json["cloud_asset_count"], serde_json::Value::Null);
    }
    assert_eq!(std::fs::read(&photo).unwrap(), b"synthetic bytes");
    assert_eq!(
        std::fs::metadata(&photo).unwrap().modified().unwrap(),
        mtime
    );
    assert_eq!(std::fs::read_dir(&source).unwrap().count(), 1);
}
#[test]
fn no_state_inside_source_and_dry_run_does_not_write() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    std::fs::create_dir(&source).unwrap();
    let invalid = source.join("state");
    assert!(
        !run(
            &invalid,
            &[
                "import",
                "--source",
                source.to_str().unwrap(),
                "--cycle",
                "x"
            ]
        )
        .status
        .success()
    );
    assert!(!invalid.exists());
    let state = dir.path().join("dry-state");
    assert!(
        run(
            &state,
            &[
                "--dry-run",
                "import",
                "--source",
                source.to_str().unwrap(),
                "--cycle",
                "x"
            ]
        )
        .status
        .success()
    );
    assert!(!state.exists());
}
#[test]
fn dismounted_secondary_is_reported_without_creating_it() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    std::fs::create_dir(&source).unwrap();
    let secondary = dir.path().join("dismounted");
    let result = run(
        &dir.path().join("state"),
        &[
            "doctor",
            "--primary",
            source.to_str().unwrap(),
            "--secondary",
            secondary.to_str().unwrap(),
        ],
    );
    assert_eq!(result.status.code(), Some(2));
    assert!(!secondary.exists());
    assert!(!dir.path().join("state").exists());
}
#[test]
fn destructive_commands_are_unavailable() {
    let dir = tempfile::tempdir().unwrap();
    for command in [vec!["cloud", "delete"], vec!["quarantine"], vec!["purge"]] {
        assert!(!run(dir.path(), &command).status.success());
    }
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}

#[cfg(unix)]
#[test]
fn generated_state_artifacts_cannot_link_to_media() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    let state = dir.path().join("state");
    std::fs::create_dir(&source).unwrap();
    std::fs::create_dir(&state).unwrap();
    let file = source.join("raw.sqlite");
    std::fs::write(&file, b"original bytes").unwrap();
    std::os::unix::fs::symlink(&file, state.join("manifest.sqlite")).unwrap();
    assert!(
        !run(
            &state,
            &[
                "import",
                "--source",
                source.to_str().unwrap(),
                "--cycle",
                "x"
            ]
        )
        .status
        .success()
    );
    assert_eq!(std::fs::read(file).unwrap(), b"original bytes");
}
