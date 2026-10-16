# Safety

- Inventory, analysis and review leave media unchanged. Replication never
  overwrites conflicts or removes extras. Confirmed quarantine only moves primary
  bytes into a journaled directory on the same volume; restore returns those bytes.
- Keep state, previews, exports and bundles outside media. Linked paths, traversal
  and unsafe output destinations are refused.
- Unknown, unreadable, partial, missing or undecodable files remain findings.
  Each file variant stays separate; names alone do not prove relationships.
- Review decisions are notes, never permission to move or remove files.
- Quarantine requires an exact current confirmation, known metadata relationships,
  successful image analysis, and fresh verification of the entire raw backup.
  Unknown or unassigned variants block moves. Restore never overwrites conflicts.
- Cloud authentication/downloads and permanent deletion are unavailable in the
  CLI. There is no telemetry or media upload.

## What verification means

Deep replica verification independently hashes both roots. Cached inventory,
`status` and `report` do not prove current byte equality. New scans and replica
operations invalidate saved completion.
Quarantined files remain accounted for by their immutable journal and original
hashes. Interrupted jobs require resume or restore before reindexing or copying.

Local bytes, physical-drive independence and cloud coverage are separate results.
Two copies on one disk are not independent backups. Nothing here proves completed
iPhone uploads, complete iCloud coverage or offsite protection.

## Browser and privacy

Review listens only on `127.0.0.1`. It requires a session token and checks Host
and Origin. It serves allowlisted cached JPEGs, never originals or arbitrary
paths. Filenames are rendered as text. Requests and preview sizes are bounded;
links, reparse points and paths outside the cache are refused.

Treat app state as trusted local data. These checks do not isolate it from a
hostile local process racing directory replacements. Pause other archive writers.

Reports contain local paths; cloud research outputs may contain account IDs and
session files. Keep them private. Decoder errors use safe categories.

Use synthetic fixtures in development. See [build results](build-report.md) for
what has actually been tested.
