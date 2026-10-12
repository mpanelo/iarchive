# Recorded checks

These are historical results. Consult the [CI workflow](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml)
for a particular commit's result. Development used synthetic media only.

## Windows two-drive run — 2026-10-11

PowerShell 5.1.26100.9444, commit `c3a418ffab1341ffe168989f525372a2b8e167d9`.
D: was NTFS; E: was exFAT. Windows reported distinct physical disks.

| Check | Result |
| --- | --- |
| Automated tests | Passed; smoke checks on C:, D: and E:. Individual counts were not retained. |
| Copy and rerun | 12 files verified; rerun copied zero. |
| Recovery | Both drive backups restored into separate state and verified all 12 files. |
| Corruption | One mismatch blocked completion; no conflict was overwritten. |
| Missing file | Rejected, then recopied and verified. |
| Unavailable root | Rejected. exFAT folder identity changed after restoration; the old cycle refused it and a fresh cycle verified all files. |
| Interruption | A 512 MiB copy stopped with a 10 MiB partial. Status stayed RUNNING/NOT VERIFIED. Resume and deep verification passed. |

Both drive summaries recorded `passed: true`; interruption also recorded
`interruption.status: PASSED`. Final reports had no issues, missing files or
mismatches. Source bytes/timestamps and retained partial hashes stayed unchanged.

Local evidence directories:

```text
.smoke/windows-369ce8fb6d6740a68b9d5c77b5db9ad7/
.smoke/two-drive-b583cc97b7f846c19693aaabd4ca891a/
.smoke/two-drive-interruption-6b4e4d894808465bbd29f354844737cf/
```

This did not test USB disconnects, graceful Ctrl+C, PowerShell 7 on that machine,
large libraries, completed iPhone uploads or cloud coverage. Raw reports remain
local and untracked.

## macOS gallery checks — 2026-10-11

| Check | Result |
| --- | --- |
| Rust | 38 passed, with the image worker required. |
| Image worker | 28 passed. |
| Repository Python checks | 3 passed. |
| Offline cloud probe | 62 passed. |
| Formatting, Clippy, JavaScript syntax | Passed. |
| cargo-deny | Passed; existing duplicate syn/winnow warnings. matchit has a reviewed BSD-3-Clause allowance. |

New coverage included restart persistence, schema 1/2 migration, stale/invalid
selections, state locking, session/Host/Origin guards, bounded bodies and safe
preview paths.

Firefox displayed synthetic JPEG/HEIC previews side by side. A saved flag updated
the Reviewed filter; corrupt HEIC and unknown files remained findings. All 11
source hashes/timestamps stayed unchanged. Ctrl+C stopped the server cleanly.

Windows browser acceptance, large-library gallery performance and the declared
minimum compiler version were not tested. See [browser review](review.md).

## Earlier replication and script checks — 2026-10-11

macOS passed 33 Rust tests, 28 image-worker tests, 3 repository checks and 62
offline cloud-probe tests, plus formatting, Clippy and cargo-deny. Coverage included
interruption/retry, root replacement, capacity, conflicts, changed snapshots,
failed publication, schema migration and recovery from either database backup.

The Windows filesystem crate and test targets passed cross-target compilation
and Clippy. This was not Windows runtime verification.

Three shared PowerShell cases passed under portable PowerShell 7.5.4 on macOS:
workflow acceptance, same-volume refusal, and stopping only an owned child,
including failed-observation cleanup. All scripts parsed and CI YAML was checked.
A separate 512 MiB copy was stopped, resumed and verified without changing source
bytes/timestamps or the retained partial.

SQLite backup flushing was fixed to retain a writable handle, as Windows requires.
Local Windows recovery later passed. These records do not establish the outcome
of the originally failed CI run.

## Earlier inventory checks — 2026-10-10

macOS Apple Silicon; Rust/Cargo 1.96.0, uv 0.12.3, Python 3.14.7.

- 21 Rust, 28 image-worker and 2 SQLite query/schema tests passed.
- Formatting, Clippy and cargo-deny passed; syn/winnow duplicates were warnings.
- Coverage included required worker integration, checked SQLite byte counts,
  manifest paths and all EXIF rotation/mirroring orientations in JPEG/HEIC.
- Lockfiles retained resolved dependencies. No security advisories were ignored.

Checked conversions fixed SQLite byte counts. Serializing HEIC EXIF metadata
fixed orientation fixtures. Path checks reject non-normalized manifest input.

## Still untested

Physical USB disconnects, private archives, complete iCloud coverage, a 200 GiB
library and the declared Rust minimum version. Review notes and local verification
never approve cloud removal. See [safety](safety.md).
