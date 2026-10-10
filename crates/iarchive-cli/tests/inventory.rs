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
fn value(output: std::process::Output) -> serde_json::Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
#[test]
fn hashes_duplicates_cache_mutation_and_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    std::fs::create_dir_all(source.join("nested")).unwrap();
    std::fs::write(source.join("same.JPG"), b"first").unwrap();
    std::fs::write(source.join("copy 旅行.PNG"), b"first").unwrap();
    std::fs::write(source.join("nested/same.JPG"), b"other").unwrap();
    std::fs::write(source.join("unknown.xyz"), b"keep unknown").unwrap();
    let state = dir.path().join("state");
    value(run(
        &state,
        &[
            "import",
            "--source",
            source.to_str().unwrap(),
            "--cycle",
            "x",
        ],
    ));
    let first = value(run(&state, &["index", "--cycle", "x"]));
    assert_eq!(first["hashes_computed"], 4);
    assert_eq!(first["exact_duplicate_groups"].as_array().unwrap().len(), 1);
    assert_eq!(
        first["exact_duplicate_groups"][0].as_array().unwrap().len(),
        2
    );
    let second = value(run(&state, &["index", "--cycle", "x"]));
    assert_eq!(second["hash_cache_hits"], 4);
    assert_eq!(second["status"]["file_count"], 4);
    std::fs::write(source.join("same.JPG"), b"a different size").unwrap();
    let third = value(run(&state, &["index", "--cycle", "x", "--deep"]));
    assert!(
        third["exact_duplicate_groups"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(third["hash_cache_hits"], 0);
    let output = dir.path().join("manifest.jsonl");
    value(run(
        &state,
        &[
            "export-manifest",
            "--cycle",
            "x",
            "--output",
            output.to_str().unwrap(),
        ],
    ));
    let records: Vec<serde_json::Value> = std::fs::read_to_string(&output)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(records.len(), 5);
    assert!(
        records
            .iter()
            .skip(1)
            .all(|r| r["sha256"].as_str().unwrap().len() == 64)
    );
    assert!(
        !run(
            &state,
            &[
                "export-manifest",
                "--cycle",
                "x",
                "--output",
                source.join("manifest.jsonl").to_str().unwrap()
            ]
        )
        .status
        .success()
    );
    assert!(!source.join("manifest.jsonl").exists());
    assert!(
        !run(
            &state,
            &[
                "export-manifest",
                "--cycle",
                "x",
                "--output",
                output.to_str().unwrap()
            ]
        )
        .status
        .success()
    );
    assert_eq!(
        std::fs::read(source.join("copy 旅行.PNG")).unwrap(),
        b"first"
    );
}
#[test]
fn partial_missing_and_disconnected_sources_never_complete() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    std::fs::create_dir(&source).unwrap();
    std::fs::write(source.join("photo.JPG"), b"photo").unwrap();
    let state = dir.path().join("state");
    value(run(
        &state,
        &[
            "import",
            "--source",
            source.to_str().unwrap(),
            "--cycle",
            "x",
        ],
    ));
    value(run(&state, &["index", "--cycle", "x"]));
    std::fs::remove_file(source.join("photo.JPG")).unwrap();
    std::fs::write(source.join("copy.partial"), b"interrupted").unwrap();
    let output = run(&state, &["index", "--cycle", "x"]);
    assert_eq!(output.status.code(), Some(2));
    let json: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["status"]["missing_files"], 1);
    assert_eq!(json["status"]["inventory_complete"], false);
    std::fs::rename(&source, dir.path().join("disconnected")).unwrap();
    assert_eq!(
        run(&state, &["index", "--cycle", "x"]).status.code(),
        Some(2)
    );
}
#[cfg(unix)]
#[test]
fn links_cannot_pull_external_bytes_into_inventory() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    std::fs::create_dir(&source).unwrap();
    let outside = dir.path().join("outside");
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("secret.jpg"), b"external").unwrap();
    std::os::unix::fs::symlink(&outside, source.join("escape")).unwrap();
    let state = dir.path().join("state");
    value(run(
        &state,
        &[
            "import",
            "--source",
            source.to_str().unwrap(),
            "--cycle",
            "x",
        ],
    ));
    let output = run(&state, &["index", "--cycle", "x"]);
    assert_eq!(output.status.code(), Some(2));
    let json: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["status"]["hashed_files"], 0);
    assert_eq!(json["status"]["file_count"], 1);
}
#[cfg(windows)]
#[test]
fn junction_cannot_escape_root() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    let outside = dir.path().join("outside");
    std::fs::create_dir(&source).unwrap();
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("secret.jpg"), b"external").unwrap();
    let junction = source.join("escape");
    let result = Command::new("cmd")
        .arg("/C")
        .arg("mklink")
        .arg("/J")
        .arg(&junction)
        .arg(&outside)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let state = dir.path().join("state");
    value(run(
        &state,
        &[
            "import",
            "--source",
            source.to_str().unwrap(),
            "--cycle",
            "x",
        ],
    ));
    let output = run(&state, &["index", "--cycle", "x"]);
    assert_eq!(output.status.code(), Some(2));
    let json: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["status"]["hashed_files"], 0);
    assert_eq!(json["status"]["file_count"], 1);
    std::fs::remove_dir(junction).unwrap();
}
#[test]
fn read_only_source_bytes_are_still_indexed() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    std::fs::create_dir(&source).unwrap();
    let file = source.join("original.jpg");
    std::fs::write(&file, b"synthetic").unwrap();
    let original = std::fs::metadata(&file).unwrap().permissions();
    let mut readonly = original.clone();
    readonly.set_readonly(true);
    std::fs::set_permissions(&file, readonly).unwrap();
    let state = dir.path().join("state");
    value(run(
        &state,
        &[
            "import",
            "--source",
            source.to_str().unwrap(),
            "--cycle",
            "x",
        ],
    ));
    assert_eq!(
        value(run(&state, &["index", "--cycle", "x"]))["status"]["hashed_files"],
        1
    );
    assert_eq!(std::fs::read(&file).unwrap(), b"synthetic");
    std::fs::set_permissions(file, original).unwrap();
}
