# iarchive

[![Checks](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml)
[MIT license](LICENSE)

A local command-line tool for taking inventory of photo archives and finding exact duplicates, visually similar images, and dark-photo candidates.

> [!WARNING]
> **Active development — experimental software.** Commands, report formats, and the state schema may change. Start with the synthetic demo below. Local inventory and image reports do not prove that an archive is fully backed up or that an iCloud library is completely downloaded.

iarchive works with files already on disk, including existing iCloud Photos downloads. It runs on macOS and Windows, keeps source media unchanged, and processes images locally without uploading them or collecting telemetry.

## Features

- Recursive file inventory with streaming SHA-256 hashes and exact-duplicate detection.
- Image analysis for HEIC, JPEG, PNG, and other supported formats, with orientation-aware previews.
- Perceptual similarity and brightness measurements that identify candidates for human review.
- SQLite state and scan caching to resume interrupted work and reuse unchanged results.
- JSON reports and exportable JSON Lines manifests, with explicit findings for missing, partial, unsupported, and undecodable files.

The current implementation is read-only on source media. Cloud authentication and downloads, archive replication, independent backup verification, a browser gallery, quarantine, and deletion are unavailable. Reports show local replication and cloud coverage as **NOT VERIFIED**. Duplicate and dark-photo candidates are suggestions for review, never deletion decisions.

## Getting started

### Requirements

- Stable Rust and Cargo. Windows builds also require the MSVC build tools.
- [uv](https://docs.astral.sh/uv/) for the local image worker and demo; the worker uses Python 3.12–3.14.
- Network access for the initial dependency installation.

The Rust CLI can inventory files, hash them, and export manifests without the Python worker. Image analysis requires the worker environment. Rust manages state, paths, hashing, and reports; Python handles image decoding and measurements.

### Build from source

```sh
git clone https://github.com/mpanelo/iarchive.git
cd iarchive
uv sync --locked --project image-worker --group dev --python 3.12
cargo build --release --locked
```

Run commands from the repository root so the CLI can find `image-worker`. When running elsewhere, pass `--worker-project` to `audit` with the checkout's worker directory.

### Try the synthetic demo

These commands generate a small library of artificial PNG, JPEG, and HEIC images, then inventory and analyze it. State, previews, and the exported manifest are stored separately from the demo media.

```sh
uv run --locked --project image-worker python fixtures/generate.py --output .smoke/demo-media --clean --with-heic
target/release/iarchive --state-dir .smoke/demo-state import --source .smoke/demo-media --cycle demo
target/release/iarchive --state-dir .smoke/demo-state index --cycle demo
target/release/iarchive --state-dir .smoke/demo-state audit --cycle demo
target/release/iarchive --state-dir .smoke/demo-state report --cycle demo
target/release/iarchive --state-dir .smoke/demo-state export-manifest --cycle demo --output .smoke/demo-manifest.jsonl
```

The example uses macOS shell syntax. See the [Windows PowerShell instructions](docs/CLI.md#windows-11-x64--powershell) for Windows builds and commands.

Windows users, including Windows Home users, can use the [setup and test scripts](docs/windows-testing.md) from the repository root after cloning in a directory of their choice. Run `scripts\windows\setup.ps1`, then `scripts\windows\test.ps1`. Add `-ExternalDrives 'D:', 'E:'` to test generated fixtures in fresh directories on those drives.

`report` displays saved candidate groups, image measurements, errors, and preview paths. Open the preview JPEGs in a local image viewer. The fixture generator and manifest exporter refuse to overwrite existing destinations; use fresh paths when repeating the demo.

## Commands

| Command | Purpose |
| --- | --- |
| `doctor` | Check local tools, drive availability, and free space. |
| `init` | Create local configuration and application state. |
| `import` | Register an existing archive directory. |
| `index` | Inventory files, compute hashes, and find exact duplicates. |
| `audit` | Analyze images and save review candidates and previews. |
| `status` | Show recorded scan and analysis status. |
| `report` | Show saved findings and review candidates. |
| `export-manifest` | Export inventory records to a JSON Lines file. |

Use `--help` for command options, `--json` for compact machine-readable output, and `--dry-run` to preview operations without scanning media. `index --deep` bypasses the hash cache. It still does not verify independent backups.

Keep application state and generated output outside archive roots. See the [CLI reference](docs/CLI.md) for options, exit codes, state locations, and platform-specific examples.

## Documentation

- [Local workflows](docs/workflows.md)
- [Safety and current limits](docs/safety.md)
- [Architecture](docs/architecture.md)
- [Development tooling and checks](docs/development.md)

## Contributing

Bug reports and pull requests are welcome. [Open an issue](https://github.com/mpanelo/iarchive/issues) with reproduction steps, your operating system, and the relevant error output. Use synthetic examples and remove personal paths or account details from shared logs; please do not upload private media.

Before submitting a change, read the [development runbook](AGENTS.md) and follow the [tooling and checks guide](docs/development.md). Development and tests use synthetic fixtures and temporary directories. GitHub Actions runs the Rust and Python suites on macOS and Windows, plus dependency policy checks on Linux.

## License

iarchive is licensed under the [MIT License](LICENSE).
