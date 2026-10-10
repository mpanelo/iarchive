# iarchive

Read-only local inventory and image audit for an existing iCloud Photos download. Rust owns archive state, paths, hashes, and reports; a uv-managed Python worker creates local previews and image features. The executable is `iarchive`.

**Implementation draft:** dependency versions are locked, and CI runs Rust quality checks and Rust/Python tests. See [the build report](docs/build-report.md) for results and limits. Do not treat this as an archive safety gate.

Commands currently provided: `doctor`, `init`, `import`, `status`, `index`, `export-manifest`, `audit`, and `report`. They never modify source media. No cloud authentication, replication, verification, gallery server, quarantine, or deletion command exists. Local replication and cloud coverage always report **NOT VERIFIED**.

From the repository root, on a machine with dependency access:

```sh
uv sync --locked --project image-worker --group dev
cargo build --release --locked
```

Generate a small synthetic library and inspect the results:

```sh
python3 fixtures/generate.py --output .smoke/clean-media --clean
target/release/iarchive --state-dir .smoke/state import --source .smoke/clean-media --cycle demo
target/release/iarchive --state-dir .smoke/state index --cycle demo
target/release/iarchive --state-dir .smoke/state audit --cycle demo
target/release/iarchive --state-dir .smoke/state report --cycle demo
target/release/iarchive --state-dir .smoke/state export-manifest --cycle demo --output .smoke/manifest.jsonl
```

The generator refuses an existing output directory; use a fresh name for another fixture. Add `--json` to any command for machine-readable output. Add `--dry-run` to describe changes without starting a media scan. `index --deep` bypasses the hash cache; it still does **not** verify two independent backups.

See [CLI and Windows instructions](docs/CLI.md), [development tooling](docs/development.md), [architecture and decisions](docs/architecture.md), [safety rules](docs/safety.md), [local workflows](docs/workflows.md), and [developer runbook](AGENTS.md).
