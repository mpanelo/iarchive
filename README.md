# iarchive

[![Checks](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml)
[MIT license](LICENSE)

Inventory, copy and verify photo archives. Review duplicate and dark-photo
candidates in a local browser gallery.

> [!WARNING]
> **Active development — experimental software.** Commands and data formats may
> change. Start with the [synthetic walkthrough](docs/workflows.md).

iarchive works with files already on disk, including iCloud Photos downloads.
Inventory, analysis and browser review leave media unchanged. Images are
processed locally, with no uploads or telemetry.

## Features

- File inventory, SHA-256 hashes and exact duplicates.
- HEIC/JPEG/PNG previews, similar-image groups and dark-photo candidates.
- A local gallery with keep, flag and defer decisions.
- Resumable scans and JSONL inventory exports.
- Raw copying without overwriting conflicts, deep verification and recovery bundles.

Confirmed quarantine and restore preserve primary bytes and leave secondary media
untouched. Unknown relationships block moves. Cloud downloads and deletion are
unavailable in the CLI.
Local byte verification does not prove complete iCloud coverage.

## Get started

Clone in a writable folder outside your photo archive:

```sh
git clone https://github.com/mpanelo/iarchive.git
cd iarchive
```

Follow the **[guided walkthrough](docs/workflows.md)** for Windows or macOS.
The demo creates synthetic images and walks through scanning, copying, browser
review and verification.

Windows setup installs missing prerequisites. macOS needs Rust/Cargo and
[uv](https://docs.astral.sh/uv/). Initial setup needs internet access.

## Documentation

- [Workflow](docs/workflows.md) and [CLI reference](docs/CLI.md)
- [Windows setup and tests](docs/windows-testing.md)
- [Replication and recovery](docs/replication.md)
- [Safety](docs/safety.md)

## Contributing

[Open an issue](https://github.com/mpanelo/iarchive/issues) with steps to reproduce,
your operating system and the error. Use synthetic examples and remove private
paths or account details from logs.

Read [AGENTS.md](AGENTS.md) and the [development guide](docs/development.md).
See [architecture](docs/architecture.md) for implementation details.

## License

[MIT](LICENSE).
