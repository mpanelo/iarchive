# Browser review and saved decisions

`iarchive review` opens a local gallery of saved audit results. It shows exact
byte duplicates, visually similar images, dark-photo candidates, and explicit
failures or unprocessed files. Source media stays unchanged. Decisions are notes
in app state; they do not move, quarantine or delete files.

## Start a review session

For the complete sequence, follow the [end-to-end workflow](workflows.md).
After its audit and replication steps, use the same executable, state and cycle
variables from that guide:

**macOS:**

```sh
"$exe" --state-dir "$state" review --cycle "$cycle"
```

**Windows:**

```powershell
& $exe --state-dir $state review --cycle $cycle
```

Open the **complete** `review_url` printed by the command, including everything
after `#`. It is private to this running session. The server listens only on
`127.0.0.1`, picks an available port, and uses no external scripts, fonts or media
services. Optional `--port 8765` selects a fixed port without changing the host.
Do not share the URL or bookmark it as a permanent link: each restart has a new
session token. No browser opens automatically.

Audit first to generate visual candidates and thumbnails. Exact-only audits work
without Python, but show no image previews unless matching analysis already
exists. Missing or invalid cached previews are shown as unavailable; stop review
and rerun audit to regenerate them. An incomplete audit remains browsable, with
its errors shown explicitly.

## Record a decision

Filter by exact duplicates, similar images, dark candidates, failures/unprocessed
files, or reviewed groups. Candidate explanations and saved image measurements
appear beside each file's relative path, cycle and byte count. Group pages contain
up to 12 groups. Decode failures are findings, never dark or duplicate removal
suggestions; unsupported files remain visible without a visual-analysis claim.

- **Keep all:** record that this group should be kept.
- **Flag selected for later:** select one or more member checkboxes, then save a
  review note for those files. This is not approval for quarantine or removal.
- **Decide later:** record a deferred decision.
- **Clear decision:** clear the current group's note, retaining its earlier history.

Notes save immediately to the same SQLite database used by inventory and audit.
Reopening the same recorded snapshot retains them. A new index or audit requires
fresh decisions; earlier records remain historical and are not silently applied
to regenerated groups. Related originals, edits, sidecars and Live Photo parts
remain separate until metadata proves a relationship. Notes in overlapping groups
are independent observations, not a consolidated file-action policy.

## Stop and retain state

Press **Ctrl+C** in the terminal to stop the server. It holds the state directory's
writer lock while running, so stop it before indexing, auditing, replicating or
running another command that writes the same state. A dry run opens no server:

```sh
"$exe" --state-dir "$state" --dry-run review --cycle "$cycle"
```

Keep app state outside media roots. Review notes do not automatically update
previously exported backup bundles; future whole-database backups include the
notes present at that time. The gallery serves cached JPEG previews only, never
original media or arbitrary local paths. Treat app state as trusted local data.

These are saved observations, not a fresh integrity check. Cloud coverage remains
**NOT VERIFIED**. No keep/flag/defer decision establishes independent backups,
completed iPhone uploads, or permission to remove local or cloud media.
