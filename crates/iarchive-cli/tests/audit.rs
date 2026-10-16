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

    let previous = || {
        let store = iarchive_store::Store::open_readonly(&state.join("manifest.sqlite")).unwrap();
        serde_json::to_value(store.review_snapshot("x").unwrap()).unwrap()
    };
    let saved = previous();
    let next = dir.path().join("next media");
    std::fs::create_dir(&next).unwrap();
    std::fs::copy(source.join("recompressed.png"), next.join("new.png")).unwrap();
    std::fs::copy(source.join("scene.jpg"), next.join("incoming.jpg.partial")).unwrap();
    let offline = dir.path().join("offline media");
    std::fs::rename(&source, &offline).unwrap();
    assert!(
        run(
            &state,
            &[
                "import",
                "--source",
                next.to_str().unwrap(),
                "--cycle",
                "next"
            ]
        )
        .status
        .success()
    );
    let next_args = [
        "audit",
        "--cycle",
        "next",
        "--categories",
        "similar",
        "--worker-project",
        project.to_str().unwrap(),
    ];
    let output = run(&state, &next_args);
    assert_eq!(
        output.status.code(),
        Some(2),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["decoded_images"], 1);
    assert_eq!(report["status"]["inventory_complete"], false);
    assert_eq!(report["status"]["file_errors"], 1);
    assert_eq!(report["status"]["cloud_coverage"], "NOT VERIFIED");
    let groups = report["candidates"].as_array().unwrap();
    assert_eq!(groups.len(), 1);
    assert_eq!(groups[0]["type"], "similar");
    let members = groups[0]["members"].as_array().unwrap();
    assert_eq!(members.len(), 2);
    assert!(members.iter().any(|m| m["cycle"] == "next"));
    let earlier = members.iter().find(|m| m["cycle"] == "x").unwrap();
    assert_ne!(
        iarchive_fs::hash_file(&offline.join(earlier["relative_path"].as_str().unwrap())).unwrap(),
        iarchive_fs::hash_file(&next.join("new.png")).unwrap(),
        "similarity must find a match with different bytes"
    );
    std::fs::rename(next.join("incoming.jpg.partial"), next.join("incoming.jpg")).unwrap();
    for (decoded, hits) in [(1, 1), (0, 2)] {
        let output = run(&state, &next_args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        for line in String::from_utf8_lossy(&output.stderr).lines() {
            serde_json::from_str::<serde_json::Value>(line)
                .expect("structured logs stay on stderr");
        }
        let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["decoded_images"], decoded);
        assert_eq!(report["analysis_cache_hits"], hits);
        assert_eq!(report["hashes_computed"], decoded);
        assert_eq!(report["hash_cache_hits"], hits);
        assert_eq!(report["status"]["inventory_complete"], true);
        assert_eq!(report["status"]["missing_files"], 0);
        assert_eq!(report["status"]["pending_images"], 0);
        assert_eq!(report["status"]["image_successes"], 2);
        assert_eq!(report["status"]["cloud_coverage"], "NOT VERIFIED");
        assert!(report["findings"].as_array().unwrap().is_empty());
    }
    let store = iarchive_store::Store::open_readonly(&state.join("manifest.sqlite")).unwrap();
    assert!(store.review_snapshot("next").unwrap().findings.is_empty());
    drop(store);
    let human = Command::new(env!("CARGO_BIN_EXE_iarchive"))
        .arg("--state-dir")
        .arg(&state)
        .args(next_args)
        .output()
        .unwrap();
    assert!(human.status.success());
    let summary = String::from_utf8(human.stdout).unwrap();
    assert!(
        summary.contains("Analysis: 0 decoded, 2 cached, 0 decode errors"),
        "{summary}"
    );
    assert!(
        summary.contains("Cloud coverage: NOT VERIFIED"),
        "{summary}"
    );
    assert!(summary.contains("Review suggestions with iarchive review --cycle next"));
    assert_eq!(previous(), saved, "earlier review state changed");
}
