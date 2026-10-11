use std::{
    path::{Path, PathBuf},
    process::Command,
};

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
fn audit_round_trip() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned();
    let project = root.join("image-worker");
    let python = if cfg!(windows) {
        project.join(".venv/Scripts/python.exe")
    } else {
        project.join(".venv/bin/python")
    };
    let worker_available = python.is_file()
        && Command::new(&python)
            .args(["-B", "-m", "iarchive_worker", "--probe"])
            .current_dir(&project)
            .output()
            .is_ok_and(|o| o.status.success());
    if !worker_available {
        assert!(
            std::env::var_os("IARCHIVE_REQUIRE_WORKER_TESTS").is_none(),
            "CI requires an installed uv worker"
        );
        eprintln!("worker integration requires uv sync --project image-worker --group dev");
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("synthetic media");
    let generated = Command::new(&python)
        .arg(root.join("fixtures/generate.py"))
        .arg("--output")
        .arg(&source)
        .arg("--clean")
        .arg("--with-heic")
        .output()
        .unwrap();
    assert!(
        generated.status.success(),
        "{}",
        String::from_utf8_lossy(&generated.stderr)
    );
    let state = dir.path().join("state");
    let snapshot = |root: &Path| {
        fn collect(root: &Path, files: &mut Vec<(PathBuf, String, std::time::SystemTime)>) {
            for entry in std::fs::read_dir(root).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    collect(&path, files);
                } else {
                    files.push((
                        path.clone(),
                        iarchive_fs::hash_file(&path).unwrap(),
                        std::fs::metadata(path).unwrap().modified().unwrap(),
                    ));
                }
            }
        }
        let mut files = Vec::new();
        collect(root, &mut files);
        files.sort_by(|a, b| a.0.cmp(&b.0));
        files
    };
    let before = snapshot(&source);
    assert!(
        run(
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
    let args = [
        "audit",
        "--cycle",
        "x",
        "--worker-project",
        project.to_str().unwrap(),
    ];
    for round in 0..2 {
        let output = run(&state, &args);
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stderr),
            String::from_utf8_lossy(&output.stdout)
        );
        let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(value["status"]["stage"], "AUDITED");
        let groups = value["candidates"].as_array().unwrap();
        for category in ["exact", "similar", "dark"] {
            assert!(
                groups.iter().any(|g| g["type"] == category),
                "missing category {category}"
            );
        }
        if round == 1 {
            assert!(value["analysis_cache_hits"].as_u64().unwrap() >= 9);
        }
        assert_eq!(snapshot(&source), before);
    }
    std::fs::write(source.join("corrupt.heic"), b"invalid HEIC").unwrap();
    let result = run(&state, &args);
    assert_eq!(result.status.code(), Some(2));
    let value: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(value["decode_errors"], 1);
    assert!(value["candidates"].as_array().unwrap().iter().all(|g| {
        g["members"]
            .as_array()
            .unwrap()
            .iter()
            .all(|m| m["relative_path"] != "corrupt.heic")
    }));
}
