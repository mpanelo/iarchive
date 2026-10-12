# iarchive

[![Checks](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml)
[MIT license](LICENSE)

Inventory, copy and verify local photo archives, then review duplicate and
dark-photo candidates in a local browser gallery.

> [!WARNING]
> **Active development — experimental software.** Commands, report formats and
> the state schema may change. Start with the [synthetic walkthrough](docs/workflows.md).
> Local verification does not prove complete iCloud coverage or authorize removal.

iarchive works with files already on disk, including existing iCloud Photos
downloads. It keeps source media unchanged, preserves distinct file variants,
and processes images locally without uploads or telemetry. Review decisions are
saved notes; they do not move or delete files.

## Features

- Recursive inventory, streaming SHA-256 and exact-duplicate detection.
- Local image analysis, including HEIC/JPEG/PNG, orientation-aware previews,
  perceptual similarity and dark-photo candidates for human review.
- A loopback browser gallery with side-by-side previews and persistent
  keep, flag and defer decisions.
- Resumable SQLite state, scan caching, explicit file/decode findings and
  exportable JSONL inventories.
- Raw replication without overwriting conflicts or pruning extras, independent
  byte verification of both roots, and recoverable database/manifest bundles.

Cloud authentication/downloads in the archive CLI, quarantine, restore and
permanent deletion are unavailable. Replication reports distinguish local byte
equality from physical-drive independence; cloud coverage remains **NOT VERIFIED**.

## Getting started

Clone in a writable directory of your choice, outside photo archives and synced
folders:

```sh
git clone https://github.com/mpanelo/iarchive.git
cd iarchive
```

Continue with the **[end-to-end workflow](docs/workflows.md)**. It includes
Windows setup, macOS build commands, a fresh synthetic archive, and every
available command in execution order:

```text
doctor → init → import → index → audit → report → export-manifest
       → replication preview → replicate → review → stop review
       → verify --deep → status
```

The guide uses one state directory and cycle throughout, explains expected
results and failures, and shows how to adapt the paths for existing archives.
Stop the gallery before running another command against its state. Final deep
verification refreshes the backup bundles with saved review notes.

### Requirements and platforms

- Rust/Cargo for source builds; Windows also needs MSVC C++ tools and a Windows SDK.
- [uv](https://docs.astral.sh/uv/) and its managed Python 3.12–3.14 environment for
  image analysis. Inventory, replication and verification work without the worker.
- Internet access for initial dependency installation.

macOS and Windows 11 x64, including Windows Home, are supported development
platforms. The [Windows setup script](docs/windows-testing.md) detects compatible
prerequisites, installs missing ones and builds the executable. Project tools
and caches stay in the checkout; prerequisite installers can change Windows
outside it. Directory scoping does not provide OS isolation.

Synthetic two-drive replication and interruption/recovery checks have passed on
Windows. The new gallery has been exercised in Firefox on macOS; Windows gallery
runtime, physical USB disconnects and large-library performance remain untested.
See the [build report](docs/build-report.md) for recorded checks and limits.

## Commands

| Command | Purpose |
| --- | --- |
| `doctor` | Check local tools, drive availability and free space. |
| `init` | Configure existing primary and optional secondary roots. |
| `import` | Register an existing local archive under a cycle ID. |
| `index` | Inventory files, hash them and find exact duplicates. |
| `audit` | Analyze images and save candidates, findings and previews. |
| `report` | Display saved image measurements and candidate groups. |
| `export-manifest` | Export a versioned JSONL inventory outside media and state. |
| `replicate` | Copy raw files without overwriting, then verify both roots. |
| `review` | Browse saved previews on loopback and record review notes. |
| `verify --deep` | Rehash both roots and publish fresh backup bundles. |
| `status` | Show recorded state; no current media verification. |

Use `--help` for options, `--json` for machine-readable reports, and `--dry-run`
to preview writes. Replica previews read and hash media. `index --deep` bypasses
the inventory cache; it does not replace two-root verification.

## Documentation

- **[End-to-end workflow](docs/workflows.md)** — start here for the full command chain.
- [CLI reference](docs/CLI.md) — options, exit codes and developer commands.
- [Browser review](docs/review.md) — decisions, previews and session behavior.
- [Replication and recovery](docs/replication.md) — conflicts, drive identity and backups.
- [Windows setup and testing](docs/windows-testing.md)
- [Windows two-drive acceptance checks](docs/windows-two-drive-test.md)
- [Safety and limits](docs/safety.md)
- [Architecture](docs/architecture.md)
- [Development tooling](docs/development.md)

## Contributing

Bug reports and pull requests are welcome. [Open an issue](https://github.com/mpanelo/iarchive/issues)
with reproduction steps, your operating system and relevant error output. Use
synthetic examples and remove personal paths or account details from shared logs;
please do not upload private media.

Read the [development runbook](AGENTS.md) and [tooling guide](docs/development.md)
before making changes. Tests use generated fixtures and temporary directories.
GitHub Actions runs the Rust/Python suites on macOS and Windows and dependency
policy checks on Linux. Keep dependency changes and their lockfiles together.

## License

iarchive is licensed under the [MIT License](LICENSE).
