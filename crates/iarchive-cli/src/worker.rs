use anyhow::{Context, Result, ensure};
use iarchive_core::{ALGORITHM_VERSION, ImageAnalyzer, ImageFeatures, ensure_separate};
use serde::Deserialize;
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

/// Direct Python execution in a uv-managed venv lets a timeout kill the actual decoder on Windows.
pub struct UvAnalyzer {
    pub project: PathBuf,
    pub timeout: Duration,
}

pub fn default_project() -> PathBuf {
    std::env::current_dir()
        .unwrap_or_default()
        .join("image-worker")
}

pub fn python(project: &Path) -> PathBuf {
    if cfg!(windows) {
        project.join(".venv/Scripts/python.exe")
    } else {
        project.join(".venv/bin/python")
    }
}

/// Bounded output files avoid deadlocking pipes or retaining an unbounded decoder traceback.
pub fn run_bounded(
    command: &mut Command,
    input: Option<&[u8]>,
    timeout: Duration,
) -> Result<Vec<u8>> {
    ensure!(!timeout.is_zero(), "timeout must be positive");
    let mut output = tempfile::tempfile()?;
    let mut child = command
        .stdout(Stdio::from(output.try_clone()?))
        .stderr(Stdio::null())
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .spawn()
        .context("cannot start capability/worker process")?;
    if let Some(input) = input {
        let result = child
            .stdin
            .take()
            .context("worker stdin missing")?
            .write_all(input);
        if let Err(error) = result {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error.into());
        }
    }
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if start.elapsed() >= timeout || output.metadata()?.len() > 65536 {
            let _ = child.kill();
            let _ = child.wait();
            anyhow::bail!("worker/capability process timed out or exceeded output limit");
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    ensure!(
        status.success(),
        "worker/capability process failed (stderr suppressed for privacy)"
    );
    use std::io::{Seek, SeekFrom};
    output.seek(SeekFrom::Start(0))?;
    ensure!(
        output.metadata()?.len() <= 65536,
        "worker output exceeds protocol limit"
    );
    let mut bytes = Vec::new();
    output.take(65537).read_to_end(&mut bytes)?;
    Ok(bytes)
}

#[derive(Deserialize)]
struct Response {
    protocol_version: u32,
    request_id: String,
    status: String,
    features: Option<ImageFeatures>,
    error: Option<String>,
}

impl ImageAnalyzer for UvAnalyzer {
    fn analyze(
        &mut self,
        file_id: i64,
        input: &Path,
        root: &Path,
        cache: &Path,
    ) -> Result<ImageFeatures> {
        ensure_separate(cache, root)?;
        ensure_separate(&self.project, root)?;
        ensure!(
            !std::env::temp_dir().canonicalize()?.starts_with(root),
            "system temp directory must be outside the archive root"
        );
        let python = python(&self.project);
        ensure!(
            python.is_file(),
            "worker environment unavailable; run uv sync --project image-worker --group dev first"
        );
        let request_id = file_id.to_string();
        let mut request = serde_json::to_vec(
            &serde_json::json!({"protocol_version":1,"request_id":request_id,"file_id":file_id,"input_path":input,"archive_root":root,"cache_root":cache,"max_preview_pixels":512,"algorithms":["phash","dark","sharpness","thumbnail"]}),
        )?;
        request.push(b'\n');
        let bytes = run_bounded(
            Command::new(python)
                .args(["-B", "-m", "iarchive_worker"])
                .arg("--archive-root")
                .arg(root)
                .arg("--cache-root")
                .arg(cache)
                .current_dir(&self.project)
                .env_remove("PYTHONPATH")
                .env("PYTHONDONTWRITEBYTECODE", "1"),
            Some(&request),
            self.timeout,
        )?;
        let response: Response =
            serde_json::from_slice(&bytes).context("invalid worker JSONL response")?;
        ensure!(
            response.protocol_version == 1 && response.request_id == request_id,
            "worker response version/ID mismatch"
        );
        ensure!(
            response.status == "ok",
            "{}",
            response
                .error
                .unwrap_or_else(|| "worker analysis failed".into())
        );
        let features = response
            .features
            .context("worker success has no features")?;
        validate_features(&features, cache)?;
        Ok(features)
    }
}

pub fn validate_features(f: &ImageFeatures, cache: &Path) -> Result<()> {
    ensure!(
        f.algorithm_version == ALGORITHM_VERSION,
        "worker algorithm version mismatch"
    );
    ensure!(
        f.phash.len() == 16 && f.phash.chars().all(|c| c.is_ascii_hexdigit()),
        "worker pHash must be 64 bits"
    );
    ensure!(f.width > 0 && f.height > 0, "invalid worker dimensions");
    for metric in [
        f.p50,
        f.p95,
        f.near_black_share,
        f.edge_energy,
        f.sharpness,
        f.dark_candidate_score,
    ] {
        ensure!(
            metric.is_finite() && metric >= 0.0,
            "invalid worker metrics"
        );
    }
    ensure!(
        f.p50 <= f.p95
            && f.p95 <= 255.0
            && f.near_black_share <= 1.0
            && f.dark_candidate_score <= 1.0,
        "out-of-range worker metrics"
    );
    let preview = Path::new(&f.thumbnail).canonicalize()?;
    ensure!(
        preview.starts_with(cache.canonicalize()?) && preview.is_file(),
        "worker preview escapes cache"
    );
    Ok(())
}
