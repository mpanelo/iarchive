use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    path::Path,
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::Duration,
};

fn command(state: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_iarchive"));
    command.arg("--state-dir").arg(state).arg("--json");
    command
}
struct Server {
    child: Child,
    address: SocketAddr,
    token: String,
}
impl Server {
    fn start(state: &Path, error: &Path) -> Self {
        let mut child = command(state)
            .args(["review", "--cycle", "x"])
            .stdout(Stdio::piped())
            .stderr(std::fs::File::create(error).unwrap())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _ = sender.send(result);
        });
        let line = match receiver.recv_timeout(Duration::from_secs(10)) {
            Ok(Ok(line)) => line,
            _ => {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!(
                    "review did not start: {}",
                    std::fs::read_to_string(error).unwrap()
                );
            }
        };
        assert!(
            !line.is_empty(),
            "review failed: {}",
            std::fs::read_to_string(error).unwrap()
        );
        let value: Value = serde_json::from_str(&line).unwrap();
        let (authority, token) = value["review_url"]
            .as_str()
            .unwrap()
            .strip_prefix("http://")
            .unwrap()
            .split_once("/#")
            .unwrap();
        let address: SocketAddr = authority.parse().unwrap();
        assert_eq!(address.ip(), Ipv4Addr::LOCALHOST);
        assert_eq!(token.len(), 64);
        Self {
            child,
            address,
            token: token.into(),
        }
    }
    fn request(&self, method: &str, path: &str, body: &str) -> (u16, Value) {
        let mut stream = TcpStream::connect_timeout(&self.address, Duration::from_secs(5)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        write!(stream,"{method} {path} HTTP/1.1\r\nHost: {}\r\nOrigin: http://{}\r\nAuthorization: Bearer {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",self.address,self.address,self.token,body.len()).unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).unwrap();
        let (headers, body) = response.split_once("\r\n\r\n").unwrap();
        let code = headers.split_whitespace().nth(1).unwrap().parse().unwrap();
        (code, serde_json::from_str(body).unwrap())
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn review_preserves_media_and_locking() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let source = root.join("media");
    let state = root.join("state");
    std::fs::create_dir(&source).unwrap();
    for name in ["first.jpg", "second.jpg"] {
        std::fs::write(source.join(name), b"synthetic equal bytes").unwrap();
    }
    let snapshot = || {
        ["first.jpg", "second.jpg"].map(|name| {
            let path = source.join(name);
            (
                iarchive_fs::hash_file(&path).unwrap(),
                std::fs::metadata(path).unwrap().modified().unwrap(),
            )
        })
    };
    let before = snapshot();
    for args in [
        vec![
            "import",
            "--cycle",
            "x",
            "--source",
            source.to_str().unwrap(),
        ],
        vec!["audit", "--cycle", "x", "--categories", "exact"],
    ] {
        let output = command(&state).args(args).output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let preview = command(&state)
        .args(["--dry-run", "review", "--cycle", "x"])
        .output()
        .unwrap();
    assert!(preview.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&preview.stdout).unwrap()["will_write_media"],
        false
    );
    let server = Server::start(&state, &root.join("review.stderr"));
    let (code, view) = server.request("GET", "/api/review", "");
    assert_eq!(code, 200);
    assert_eq!(view["groups"][0]["kind"], "exact");
    let input = serde_json::json!({"fingerprint":view["groups"][0]["fingerprint"],"action":"keep_all","selected":[]}).to_string();
    assert_eq!(server.request("POST", "/api/decision", &input).0, 200);
    let blocked = command(&state)
        .args(["index", "--cycle", "x"])
        .output()
        .unwrap();
    assert_eq!(blocked.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&blocked.stderr)
            .contains("another iarchive operation holds this state directory")
    );
    drop(server);
    let server = Server::start(&state, &root.join("review-restart.stderr"));
    let (_, view) = server.request("GET", "/api/review", "");
    assert_eq!(view["groups"][0]["decision"]["action"], "keep_all");
    assert_eq!(view["status"]["stage"], "AUDIT_INCOMPLETE");
    drop(server);
    assert_eq!(snapshot(), before);
    assert_eq!(std::fs::read_dir(&source).unwrap().count(), 2);
    #[cfg(unix)]
    {
        let linked = root.join("linked-state");
        std::os::unix::fs::symlink(&state, &linked).unwrap();
        let output = command(&linked)
            .args(["review", "--cycle", "x"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&output.stderr).contains("linked review path refused"));
    }
}
