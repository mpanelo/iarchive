# CLI reference

For commands in order, use the [full workflow](workflows.md).
Run from the checkout. Use one state directory and cycle for each workflow.

## Common options

| Option | Purpose |
| --- | --- |
| `--help` | Show command options. |
| `--state-dir PATH` | Choose app state outside media. |
| `--json` | Print machine-readable reports. |
| `--dry-run` | Preview writes without changing files or state. Replica previews still read and hash media. |

## Commands

| Command | Main options |
| --- | --- |
| `doctor` | `--primary PATH`, `--secondary PATH` |
| `init` | `--primary PATH`, optional `--secondary PATH` |
| `import` | `--source PATH --cycle ID` |
| `index` | `--cycle ID`, optional `--deep` to bypass the hash cache |
| `audit` | `--cycle ID`, optional `--worker-project PATH` |
| `report` | `--cycle ID` |
| `export-manifest` | `--cycle ID --output PATH` |
| `replicate` | `--cycle ID --primary-artifacts PATH --secondary-artifacts PATH`, optional `--secondary PATH` |
| `review` | `--cycle ID`, optional `--port NUMBER` |
| `verify` | The replica options above, plus required `--deep` |
| `status` | `--cycle ID` |

Cycle IDs use ASCII letters, digits, `_` or `-`, up to 80 characters.
`import` registers existing files; it does not download them.

Replica paths must exist. Use `--allow-unverified-devices` only to acknowledge
byte-only testing when physical independence cannot be proved. See
[replication](replication.md) for conflicts and recovery.

Audit defaults to `--categories exact,similar,dark`, a 60-second per-file timeout,
and a 1024 MiB thumbnail cache. Change these with `--categories`,
`--timeout-seconds` and `--cache-limit-mib`. Exact-only audit needs no Python
worker and does not claim a complete visual audit.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The requested operation completed. |
| 1 | Invocation, configuration, tool or state error. |
| 2 | The report contains incomplete checks or findings. |

Videos can be copied successfully while their visual audit remains incomplete.
Successful local checks do not prove complete cloud coverage.

## State and worker locations

Default state is `%LOCALAPPDATA%\iarchive\data` on Windows or
`~/Library/Application Support/iarchive` on macOS.

The worker defaults to `image-worker` in the current directory. When running
elsewhere, pass its path with `--worker-project`. `status` and `report` show saved
results; they do not verify current media.

[Windows setup](windows-testing.md) · [Developer checks](development.md) · [Safety](safety.md)
