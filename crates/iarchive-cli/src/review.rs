use crate::{Cli, emit};
use anyhow::{Context, Result, ensure};
use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, Path as RoutePath, Request, State},
    http::{StatusCode, Uri, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use iarchive_core::{is_link, resolve_input};
use iarchive_store::{ReviewInput, ReviewSnapshot, Store};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Read,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

const MAX_PREVIEW_BYTES: u64 = 8 * 1024 * 1024;

struct AppState {
    store: Mutex<Store>,
    cycle: String,
    cache: PathBuf,
    authority: String,
    token: String,
    previews: BTreeMap<i64, String>,
}

type Shared = Arc<AppState>;
type ApiError = (StatusCode, &'static str);

pub fn run(cli: &Cli, store: Store, state: &Path, cycle: &str, port: u16) -> Result<bool> {
    reject_links(cli.state_dir.as_deref().unwrap_or(state))?;
    let snapshot = store.review_snapshot(cycle)?;
    let mut previews = BTreeMap::new();
    for cycle in store.cycles()? {
        let view = store.review_snapshot(&cycle.cycle)?;
        previews.extend(
            view.groups
                .iter()
                .flat_map(|g| &g.members)
                .chain(&view.findings)
                .filter_map(|f| f.thumbnail.as_ref().map(|p| (f.id, p.clone()))),
        );
    }
    let _ = snapshot;
    tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {
        // No configurable host: review content can only be served on IPv4 loopback.
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
        let authority = listener.local_addr()?.to_string();
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).map_err(|_| anyhow::anyhow!("cannot create review session token"))?;
        let token = random.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
        let url = format!("http://{authority}/#{token}");
        let state = Arc::new(AppState {
            store: Mutex::new(store), cycle: cycle.to_owned(), cache: state.join("thumbnails"), authority, token, previews,
        });
        emit(cli, &serde_json::json!({
            "review_url":url,"cycle":cycle,"media_read_only":true,
            "note":"Open this private URL in your browser. Ctrl+C stops the server. Review decisions are saved in app state; no files are moved.",
        }))?;
        axum::serve(listener, router(state)).with_graceful_shutdown(async {
            if tokio::signal::ctrl_c().await.is_err() {
                tracing::error!("could not install review shutdown handler");
            }
        }).await.context("review server stopped unexpectedly")?;
        Ok(true)
    })
}

fn router(state: Shared) -> Router {
    Router::new()
        .route(
            "/",
            get(|| async {
                (
                    [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
                    include_str!("review/index.html"),
                )
            }),
        )
        .route(
            "/review.js",
            get(|| async {
                (
                    [(header::CONTENT_TYPE, "text/javascript; charset=utf-8")],
                    include_str!("review/review.js"),
                )
            }),
        )
        .route(
            "/review.css",
            get(|| async {
                (
                    [(header::CONTENT_TYPE, "text/css; charset=utf-8")],
                    include_str!("review/review.css"),
                )
            }),
        )
        .route("/api/review", get(snapshot))
        .route("/api/cycles", get(cycles))
        .route("/api/decision", post(decide))
        .route("/api/preview/{id}", get(preview))
        .layer(DefaultBodyLimit::max(16 * 1024))
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state)
}

async fn guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    let headers = request.headers();
    let origin = format!("http://{}", state.authority);
    let allowed = headers.get(header::HOST).and_then(|h| h.to_str().ok())
        == Some(state.authority.as_str())
        && headers
            .get(header::ORIGIN)
            .is_none_or(|h| h.to_str().ok() == Some(origin.as_str()))
        && headers
            .get("sec-fetch-site")
            .is_none_or(|h| h != "cross-site");
    let protected = request.uri().path().starts_with("/api/");
    let bearer = format!("Bearer {}", state.token);
    let authenticated = !protected
        || headers
            .get(header::AUTHORIZATION)
            .and_then(|h| h.to_str().ok())
            == Some(bearer.as_str());
    let write_origin = request.method() != axum::http::Method::POST
        || headers.get(header::ORIGIN).and_then(|h| h.to_str().ok()) == Some(origin.as_str());
    let mut response = if allowed && authenticated && write_origin {
        next.run(request).await
    } else {
        (
            StatusCode::FORBIDDEN,
            "This request is outside the review session.",
        )
            .into_response()
    };
    for (name, value) in [
        ("cache-control", "no-store"),
        ("referrer-policy", "no-referrer"),
        ("x-content-type-options", "nosniff"),
        (
            "content-security-policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        ),
    ] {
        response.headers_mut().insert(
            axum::http::HeaderName::from_static(name),
            axum::http::HeaderValue::from_static(value),
        );
    }
    response
}

fn selected_cycle<'a>(state: &'a AppState, uri: &'a axum::http::Uri) -> Result<&'a str, ApiError> {
    let cycle = if let Some(query) = uri.query() {
        query
            .strip_prefix("cycle=")
            .filter(|value| !value.contains('&'))
            .ok_or((StatusCode::BAD_REQUEST, "Invalid cycle query."))?
    } else {
        &state.cycle
    };
    iarchive_core::validate_cycle(cycle)
        .map_err(|_| (StatusCode::BAD_REQUEST, "Invalid archive cycle."))?;
    Ok(cycle)
}

fn load_snapshot_cycle(state: &AppState, cycle: &str) -> Result<ReviewSnapshot, ApiError> {
    let store = state.store.lock().map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Review state is unavailable.",
        )
    })?;
    let mut snapshot = store.review_snapshot(cycle).map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Cannot read the saved audit.",
        )
    })?;
    for file in snapshot
        .groups
        .iter_mut()
        .flat_map(|g| g.members.iter_mut())
        .chain(snapshot.findings.iter_mut())
    {
        file.preview_available = file
            .thumbnail
            .as_deref()
            .is_some_and(|path| preview_path(&state.cache, path).is_ok());
    }
    Ok(snapshot)
}

async fn snapshot(State(state): State<Shared>, uri: Uri) -> Result<Json<ReviewSnapshot>, ApiError> {
    Ok(Json(load_snapshot_cycle(
        &state,
        selected_cycle(&state, &uri)?,
    )?))
}

async fn cycles(State(state): State<Shared>) -> Result<Json<serde_json::Value>, ApiError> {
    let store = state.store.lock().map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Review state unavailable.",
        )
    })?;
    Ok(Json(
        serde_json::json!({"cycles":store.cycles().map_err(|_|(StatusCode::INTERNAL_SERVER_ERROR,"Cannot read cycles."))?}),
    ))
}

async fn decide(
    State(state): State<Shared>,
    uri: Uri,
    Json(input): Json<ReviewInput>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let mut store = state.store.lock().map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Review state is unavailable.",
        )
    })?;
    store
        .record_review(selected_cycle(&state, &uri)?, &input)
        .map_err(|_| {
            (
                StatusCode::CONFLICT,
                "Decision rejected. Reload the gallery and check the selection.",
            )
        })?;
    Ok(Json(
        serde_json::json!({"saved":true,"media_changed":false}),
    ))
}

async fn preview(
    State(state): State<Shared>,
    uri: Uri,
    RoutePath(id): RoutePath<i64>,
) -> Result<Response, ApiError> {
    let view = load_snapshot_cycle(&state, selected_cycle(&state, &uri)?)?;
    if !view
        .groups
        .iter()
        .flat_map(|g| &g.members)
        .chain(&view.findings)
        .any(|f| f.id == id)
    {
        return Err((StatusCode::NOT_FOUND, "No preview in the selected cycle."));
    }
    let bytes = state
        .previews
        .get(&id)
        .map(String::as_str)
        .ok_or((StatusCode::NOT_FOUND, "No preview was saved."))
        .and_then(|path| {
            read_preview(&state.cache, path).map_err(|_| {
                (
                    StatusCode::NOT_FOUND,
                    "Preview is unavailable; rerun audit to regenerate it.",
                )
            })
        })?;
    Ok(([(header::CONTENT_TYPE, "image/jpeg")], Body::from(bytes)).into_response())
}

// Check every existing ancestor before canonicalizing, including the cache root.
pub(crate) fn reject_links(path: &Path) -> Result<()> {
    let path = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    for ancestor in path.ancestors() {
        if let Ok(metadata) = fs::symlink_metadata(ancestor) {
            ensure!(!is_link(&metadata), "linked review path refused");
        }
    }
    Ok(())
}

fn preview_path(cache: &Path, path: &str) -> Result<PathBuf> {
    reject_links(cache)?;
    let supplied = Path::new(path);
    ensure!(
        supplied.is_absolute()
            && !supplied
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir)),
        "invalid preview path"
    );
    reject_links(supplied)?;
    let name = supplied
        .file_name()
        .and_then(|n| n.to_str())
        .context("invalid preview name")?;
    let resolved = resolve_input(&cache.canonicalize()?, name)?;
    ensure!(
        supplied.canonicalize()? == resolved && supplied.extension().is_some_and(|e| e == "jpg"),
        "preview escapes thumbnail cache"
    );
    ensure!(
        fs::metadata(&resolved)?.len() <= MAX_PREVIEW_BYTES,
        "preview is too large"
    );
    Ok(resolved)
}

fn read_preview(cache: &Path, path: &str) -> Result<Vec<u8>> {
    let resolved = preview_path(cache, path)?;
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000); // FILE_FLAG_OPEN_REPARSE_POINT
    }
    let file = options.open(resolved)?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file() && !is_link(&metadata),
        "linked preview refused"
    );
    let mut bytes = Vec::new();
    file.take(MAX_PREVIEW_BYTES + 1).read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 <= MAX_PREVIEW_BYTES
            && bytes.starts_with(&[0xff, 0xd8, 0xff])
            && bytes.ends_with(&[0xff, 0xd9]),
        "invalid JPEG preview"
    );
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::to_bytes,
        http::{Method, Request as HttpRequest},
    };
    use iarchive_core::{ALGORITHM_VERSION, FileFact, FileKind, ImageFeatures};
    use tower::ServiceExt;

    struct Fixture {
        dir: tempfile::TempDir,
        state: Shared,
    }
    impl Fixture {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let cache = dir.path().canonicalize().unwrap().join("thumbnails");
            fs::create_dir(&cache).unwrap();
            let image = cache.join("synthetic.jpg");
            fs::write(&image, [0xff, 0xd8, 0xff, 0xd9]).unwrap();
            let mut store = Store::open(&dir.path().join("state.sqlite")).unwrap();
            store.import("x", &dir.path().join("media")).unwrap();
            let scan = store.begin_scan("x").unwrap();
            for (name, error) in [
                ("<img src=x onerror=alert(1)>.jpg", None),
                ("broken.heic", Some("Cannot decode synthetic image")),
            ] {
                store
                    .save_file(
                        "x",
                        scan,
                        &FileFact {
                            relative_path: name.into(),
                            path_key: name.into(),
                            kind: FileKind::Image,
                            byte_count: 4,
                            mtime_ns: "1".into(),
                            sha256: Some("a".repeat(64)),
                            error: None,
                            warnings: vec![],
                        },
                    )
                    .unwrap();
                let id = if error.is_some() { 2 } else { 1 };
                let features = if let Some(error) = error {
                    Err(anyhow::anyhow!(error))
                } else {
                    Ok(ImageFeatures {
                        algorithm_version: ALGORITHM_VERSION.into(),
                        phash: "0".repeat(16),
                        width: 1,
                        height: 1,
                        p50: 0.0,
                        p95: 0.0,
                        near_black_share: 1.0,
                        edge_energy: 0.0,
                        sharpness: 0.0,
                        dark_candidate_score: 1.0,
                        dark_reason: "Synthetic dark frame".into(),
                        thumbnail: image.to_str().unwrap().into(),
                    })
                };
                store
                    .save_analysis(id, &"a".repeat(64), &features, ALGORITHM_VERSION)
                    .unwrap();
            }
            store.finish_scan("x", scan, true).unwrap();
            store
                .candidate("x", "dark", "Synthetic frame", &[1])
                .unwrap();
            Self {
                dir,
                state: Arc::new(AppState {
                    store: Mutex::new(store),
                    cycle: "x".into(),
                    cache,
                    authority: "127.0.0.1:4321".into(),
                    token: "test-session".into(),
                    previews: BTreeMap::from([(1, image.to_str().unwrap().into())]),
                }),
            }
        }
        async fn request(&self, method: Method, path: &str, body: &str) -> Response {
            router(self.state.clone())
                .oneshot(
                    HttpRequest::builder()
                        .method(method)
                        .uri(path)
                        .header("host", &self.state.authority)
                        .header("origin", format!("http://{}", self.state.authority))
                        .header("authorization", format!("Bearer {}", self.state.token))
                        .header("content-type", "application/json")
                        .body(Body::from(body.to_owned()))
                        .unwrap(),
                )
                .await
                .unwrap()
        }
    }
    async fn json(response: Response) -> serde_json::Value {
        serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap()).unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn gallery_saves_decisions_and_findings() {
        let fixture = Fixture::new();
        let response = fixture.request(Method::GET, "/api/review", "").await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()["cache-control"], "no-store");
        let view = json(response).await;
        assert_eq!(
            view["groups"][0]["members"][0]["relative_path"],
            "<img src=x onerror=alert(1)>.jpg"
        );
        assert_eq!(view["groups"][0]["members"][0]["preview_available"], true);
        assert!(
            view["groups"][0]["members"][0]["features"]
                .get("thumbnail")
                .is_none()
        );
        assert_eq!(
            view["findings"][0]["decode_error"],
            "Cannot decode synthetic image"
        );
        assert_eq!(
            fixture
                .request(Method::GET, "/api/preview/1", "")
                .await
                .status(),
            StatusCode::OK
        );
        for id in [2, 999] {
            assert_eq!(
                fixture
                    .request(Method::GET, &format!("/api/preview/{id}"), "")
                    .await
                    .status(),
                StatusCode::NOT_FOUND
            );
        }
        let input = serde_json::json!({"fingerprint":view["groups"][0]["fingerprint"],"action":"flag_selected","selected":[1]}).to_string();
        assert_eq!(
            fixture
                .request(Method::POST, "/api/decision", &input)
                .await
                .status(),
            StatusCode::OK
        );
        let saved = json(fixture.request(Method::GET, "/api/review", "").await).await;
        assert_eq!(saved["groups"][0]["decision"]["action"], "flag_selected");
        assert_eq!(saved["status"]["stage"], "INDEXED");
        for input in [
            serde_json::json!({"fingerprint":"stale","action":"keep_all","selected":[]}),
            serde_json::json!({"fingerprint":view["groups"][0]["fingerprint"],"action":"flag_selected","selected":[999]}),
        ] {
            assert_eq!(
                fixture
                    .request(Method::POST, "/api/decision", &input.to_string())
                    .await
                    .status(),
                StatusCode::CONFLICT
            );
        }
        assert_eq!(
            fixture
                .request(Method::POST, "/api/decision", r#"{"action":"quarantine"}"#)
                .await
                .status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(
            fixture
                .request(Method::POST, "/api/decision", &" ".repeat(17000))
                .await
                .status(),
            StatusCode::PAYLOAD_TOO_LARGE
        );
        assert_eq!(
            fixture
                .request(Method::GET, "/api/preview/..%2f..%2fsecret", "")
                .await
                .status(),
            StatusCode::BAD_REQUEST
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn browser_requests_require_session_authority() {
        let fixture = Fixture::new();
        for (host, origin, token, method) in [
            (
                "evil.example",
                Some("http://127.0.0.1:4321"),
                "Bearer test-session",
                Method::GET,
            ),
            (
                "127.0.0.1:4321",
                Some("https://evil.example"),
                "Bearer test-session",
                Method::POST,
            ),
            (
                "127.0.0.1:4321",
                Some("http://127.0.0.1:4321"),
                "wrong",
                Method::GET,
            ),
            ("127.0.0.1:4321", None, "Bearer test-session", Method::POST),
        ] {
            let response = router(fixture.state.clone())
                .oneshot(
                    HttpRequest::builder()
                        .method(method)
                        .uri("/api/decision")
                        .header("host", host)
                        .header("authorization", token)
                        .body(Body::empty())
                        .map(|mut req| {
                            if let Some(origin) = origin {
                                req.headers_mut().insert("origin", origin.parse().unwrap());
                            }
                            req
                        })
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::FORBIDDEN);
            assert!(
                response
                    .headers()
                    .get("access-control-allow-origin")
                    .is_none()
            );
        }
        let html = fixture.request(Method::GET, "/", "").await;
        assert_eq!(html.status(), StatusCode::OK);
        assert!(
            html.headers()["content-security-policy"]
                .to_str()
                .unwrap()
                .contains("frame-ancestors 'none'")
        );
        let bytes = to_bytes(html.into_body(), 100000).await.unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("<img src=x onerror"));
        assert_eq!(
            fixture
                .state
                .store
                .lock()
                .unwrap()
                .review_snapshot("x")
                .unwrap()
                .decision_history_count,
            0
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn cycle_selection_binds_review_requests() {
        let fixture = Fixture::new();
        {
            let mut store = fixture.state.store.lock().unwrap();
            store
                .import("next", &fixture.dir.path().join("other-media"))
                .unwrap();
        }
        let data = json(fixture.request(Method::GET, "/api/cycles", "").await).await;
        assert_eq!(data["cycles"].as_array().unwrap().len(), 2);
        let next = json(
            fixture
                .request(Method::GET, "/api/review?cycle=next", "")
                .await,
        )
        .await;
        assert_eq!(next["status"]["cycle"], "next");
        assert!(next["groups"].as_array().unwrap().is_empty());
        let view = json(
            fixture
                .request(Method::GET, "/api/review?cycle=x", "")
                .await,
        )
        .await;
        let input=serde_json::json!({"fingerprint":view["groups"][0]["fingerprint"],"action":"keep_all","selected":[]}).to_string();
        assert_eq!(
            fixture
                .request(Method::POST, "/api/decision?cycle=next", &input)
                .await
                .status(),
            StatusCode::CONFLICT
        );
        assert_eq!(
            fixture
                .request(Method::GET, "/api/preview/1?cycle=next", "")
                .await
                .status(),
            StatusCode::NOT_FOUND
        );
        for query in ["cycle=../x", "cycle=x&cycle=next", "cycle=%2ex"] {
            assert_eq!(
                fixture
                    .request(Method::GET, &format!("/api/review?{query}"), "")
                    .await
                    .status(),
                StatusCode::BAD_REQUEST
            );
        }
        assert_eq!(
            fixture
                .request(Method::POST, "/api/decision?cycle=x", &input)
                .await
                .status(),
            StatusCode::OK
        );
        let view = json(
            fixture
                .request(Method::GET, "/api/review?cycle=x", "")
                .await,
        )
        .await;
        assert_eq!(view["groups"][0]["decision"]["action"], "keep_all");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn preview_paths_stay_in_cache() {
        let fixture = Fixture::new();
        // Both files exist under the same leaf name; only containment should reject it.
        let outside = fixture
            .dir
            .path()
            .canonicalize()
            .unwrap()
            .join("synthetic.jpg");
        fs::write(&outside, [0xff, 0xd8, 0xff, 0xd9]).unwrap();
        assert!(read_preview(&fixture.state.cache, outside.to_str().unwrap()).is_err());
        let preview = fixture.state.cache.join("synthetic.jpg");
        // Joining onto a Windows verbatim path normalizes away parent components.
        // Append raw text so the fixture actually exercises the traversal guard.
        let mut traversal = fixture.state.cache.as_os_str().to_os_string();
        for component in ["..", "thumbnails", "synthetic.jpg"] {
            traversal.push(std::path::MAIN_SEPARATOR_STR);
            traversal.push(component);
        }
        let traversal = PathBuf::from(traversal);
        assert!(
            traversal
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
        );
        assert_eq!(
            read_preview(&fixture.state.cache, traversal.to_str().unwrap())
                .unwrap_err()
                .to_string(),
            "invalid preview path"
        );
        fs::write(&preview, b"not a JPEG").unwrap();
        assert_eq!(
            fixture
                .request(Method::GET, "/api/preview/1", "")
                .await
                .status(),
            StatusCode::NOT_FOUND
        );
        let file = fs::File::create(&preview).unwrap();
        file.set_len(MAX_PREVIEW_BYTES + 1).unwrap();
        drop(file);
        assert!(read_preview(&fixture.state.cache, preview.to_str().unwrap()).is_err());
        fs::remove_file(&preview).unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, &preview).unwrap();
        }
        #[cfg(windows)]
        {
            // Directory junctions work without developer mode or elevated symlink rights.
            fs::remove_dir(&fixture.state.cache).unwrap();
            let output = std::process::Command::new("cmd")
                .args(["/C", "mklink", "/J"])
                .arg(fixture.dir.path().join("thumbnails"))
                .arg(fixture.dir.path())
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        assert!(read_preview(&fixture.state.cache, preview.to_str().unwrap()).is_err());
        #[cfg(windows)]
        fs::remove_dir(&fixture.state.cache).unwrap();
    }
}
