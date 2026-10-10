# Safety contract

Media input is read-only. App state and thumbnails must be outside media roots, and exports must use a new filename outside media/app state. The app never reorganizes an existing archive. Unknown extensions are retained in inventory; unreadable files, temporary files, links, and collisions are findings. A corrupted decode produces no pHash, brightness, or dark candidate.

No cloud download/auth implementation, cloud removal, raw copying, two-drive verification, review decision, quarantine, restore, or permanent purge exists in this build. Destructive commands fail at CLI parsing. There is no network listener, telemetry, photo upload, or cloud-delete provider method. Installing dependencies can use package registries; inventory and image audit themselves use local processes and files.

All reports keep `local_replica` and `cloud_coverage` at **NOT VERIFIED**, and `cloud_asset_count` is null. Inventory counts are file counts. Byte hashes, visual similarity, and a successful decoder do not prove a complete iCloud library or safe removal. Neither filename stems nor truncated identifiers imply remote IDs or associated variants.

`status` and `report` describe saved work. The size/mtime scan cache is explicitly stale-capable. Images are rehashed around analysis, but even a deep index is only a single-root scan. A verified-archive claim requires independent reads of both replicas and stable drive identities; this build provides neither. Nothing here can approve cloud removal.

Privacy errors from the decoder use safe categories rather than raw tracebacks or arbitrary exception strings. Local reports intentionally contain local filenames and paths; do not share them without inspecting/redacting them. No support export is implemented.

Use synthetic fixtures for development. See [the build report](build-report.md) for completed checks and limits before using the app on an archive. Passing synthetic tests does not prove library completeness, independent backups, or safe removal.
