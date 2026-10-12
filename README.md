# iarchive

[![Checks](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml)
[MIT license](LICENSE)

Inventory, copy and verify photo archives. Review duplicate and dark-photo
candidates in a local browser gallery.

> [!WARNING]
> **Active development — experimental software.** Commands and data formats may
> change. Start with the [synthetic walkthrough](docs/workflows.md).

iarchive works with files already on disk, including iCloud Photos downloads.
Source files stay unchanged. Images are processed locally, with no uploads or
telemetry. Review decisions are saved notes; they do not move or delete files.

## Features

- File inventory, SHA-256 hashes and exact duplicates.
- HEIC/JPEG/PNG previews, similar-image groups and dark-photo candidates.
- A local gallery with keep, flag and defer decisions.
- Resumable scans and JSONL inventory exports.
- Raw copying without overwriting conflicts, deep verification and recovery bundles.

Cloud downloads, quarantine, restore and deletion are unavailable in the CLI.
Local byte verification does not prove complete iCloud coverage.

## Get started

Clone in a writable folder outside your photo archive:

```sh
git clone https://github.com/mpanelo/iarchive.git
cd iarchive
```

Follow the **[full workflow](docs/workflows.md)** for Windows or macOS:

```text
doctor
init
import
index
audit
report
export-manifest
replicate --dry-run
replicate
review
Ctrl+C to stop review
verify --deep
status
```

Windows setup installs missing prerequisites and builds the app. macOS builds
need Rust/Cargo and [uv](https://docs.astral.sh/uv/). Image analysis uses managed
Python 3.12–3.14; raw inventory and copying work without it. Initial setup needs
internet access. See [Windows setup](docs/windows-testing.md) for installer details.

## Commands

| Command | Purpose |
| --- | --- |
| `doctor` | Check tools, drives and free space. |
| `init` | Configure archive paths. |
| `import` | Register a local archive. |
| `index` | Inventory and hash files. |
| `audit` | Analyze images and save candidates. |
| `report` | Show saved candidates and findings. |
| `export-manifest` | Export the inventory. |
| `replicate` | Copy raw files and verify both roots. |
| `review` | Browse previews and save review notes. |
| `verify --deep` | Rehash both roots and refresh backup bundles. |
| `status` | Show saved state. |

Use `--help` for options. See the [CLI reference](docs/CLI.md) for exit codes.

## Documentation

- [Full workflow](docs/workflows.md)
- [Windows setup and tests](docs/windows-testing.md)
- [Two-drive tests](docs/windows-two-drive-test.md)
- [Browser review](docs/review.md)
- [Replication and recovery](docs/replication.md)
- [Safety](docs/safety.md)
- [Architecture](docs/architecture.md)
- [Build results](docs/build-report.md)

## Contributing

[Open an issue](https://github.com/mpanelo/iarchive/issues) with steps to reproduce,
your operating system and the error. Use synthetic examples and remove private
paths or account details from logs.

Read [AGENTS.md](AGENTS.md) and the [development guide](docs/development.md).
CI runs Rust/Python checks on macOS and Windows, plus dependency checks on Linux.

## License

[MIT](LICENSE).
