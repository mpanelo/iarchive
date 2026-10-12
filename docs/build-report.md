# Build and test report

## Windows two-drive results — 2026-10-11

The user ran the scripts separately on Windows with PowerShell 5.1.26100.9444.
Both drive tests recorded commit `c3a418ffab1341ffe168989f525372a2b8e167d9`.
The source drive D: uses NTFS; the secondary drive E: uses exFAT. Both tests
reported distinct physical backing disks through Windows topology discovery.

| Check | Result |
| --- | --- |
| Automated checks | `passed: true`, `automated_checks: PASSED`; smoke tests passed on C:, D: and E:. The summary does not retain individual test counts. |
| Copy and repeat run | All 12 files copied and verified; the repeat run copied zero files. |
| Backup recovery | Both drive backups recovered into separate state directories and verified all 12 files. |
| Corruption and conflict | One hash mismatch blocked completion; replication retained the conflicting file and copied nothing. |
| Missing file | Verification rejected one missing file; replication recopied it and verified all 12 files. |
| Unavailable root | Verification rejected the missing root. Restoring the exFAT folder changed its identity; the old cycle rejected it, and a fresh cycle verified all 12 files. |
| Interruption and resume | The 512 MiB copy was stopped after observing a 10 MiB partial. Saved status remained `RUNNING` and `NOT VERIFIED`. Resume copied and verified the file; a separate deep verification passed. |

Both drive-test summaries show `passed: true`; the interruption summary also
shows `interruption.status: PASSED`. Final reports have no issues, missing files
or hash mismatches. The successful scripts also checked independent hashes,
unchanged source bytes and timestamps, and the retained partial's unchanged hash.

Local evidence is retained beneath these checkout-relative directories:

- `.smoke/windows-369ce8fb6d6740a68b9d5c77b5db9ad7/`
- `.smoke/two-drive-b583cc97b7f846c19693aaabd4ca891a/`
- `.smoke/two-drive-interruption-6b4e4d894808465bbd29f354844737cf/`

These results establish Windows runtime behavior with synthetic files on these
two drives. They do not test USB disconnects, graceful Ctrl+C, a large library,
PowerShell 7 on this machine, completed iPhone uploads or iCloud coverage.
Raw reports and generated files remain local and are not tracked in Git.

## Local gallery checks — 2026-10-11

The new `review` command serves saved audit candidates and cached JPEG previews
only on IPv4 loopback. Keep-all, flag-selected, defer and reset append review
notes to SQLite without moving media or changing replica completion. Recorded
scan/group/member fingerprints reject stale selections and retain older history.

All 38 Rust tests passed on macOS with the Python worker required. New coverage
checks persistence across restarts, version 1/2 database migration, stale and
out-of-group selections, exclusive state locking, Host/Origin/session checks,
bounded bodies, allowlisted previews, traversal and linked thumbnail refusal.
Formatting and workspace Clippy passed with warnings treated as errors. All 28
image-worker, three repository and 62 offline cloud-probe tests passed, and the
JavaScript syntax check passed. cargo-deny passed with a reviewed crate-specific
BSD-3-Clause allowance for Axum's `matchit` router; existing `syn`/`winnow`
duplicate warnings remain.

Firefox on macOS rendered the generated JPEG/HEIC previews side by side. Saving
a selected-file flag updated the group and Reviewed filter; the failure filter
showed a corrupt HEIC and unknown file explicitly. All 11 synthetic source file
hashes and timestamps remained unchanged. The synthetic review server stopped
cleanly with Ctrl+C. Raw test data stays outside tracked files.

The earlier Windows replication results do not verify this new gallery.
Windows gallery runtime, large-library gallery performance and minimum-compiler
compatibility remain untested. The browser pages 12 groups at a time, while the
API materializes saved groups/findings. See [browser review](review.md) for usage,
state locking and the limits of review notes.

## Windows acceptance script checks — 2026-10-11

Replica database backups now retain their writable creation handle until SQLite
finishes, then flush that handle. The previous read-only reopen cannot satisfy
Windows' flush access requirements and is the likely cause of the two failed
replication tests in the published Windows run. Failed exit-code assertions now
include stdout JSON as well as stderr. At the time of these earlier checks,
a Windows rerun was still required to confirm the reported CI failure was
resolved. The later results above confirm local Windows backup recovery;
they do not establish the outcome of that CI run.

The two-drive guide now uses `scripts/windows/test-two-drives.ps1` for synthetic
copying, independent hashes, bundle recovery and failure handling. Three shared
PowerShell regression cases passed locally with a temporary portable PowerShell
7.5.4 runtime on macOS: the acceptance workflow, same-volume topology refusal,
and interruption of only an owned child, including cleanup after a failed
observation. All Windows script files parsed successfully, and the CI workflow
YAML was checked. CI is configured to run the cases under both Windows shells.

After the backup fix, all 33 Rust tests passed with the Python worker required,
along with formatting, workspace Clippy, 28 image-worker tests, three repository
tests and 62 offline cloud-probe tests. The three PowerShell cases passed again
against the updated executable. cargo-deny passed after refreshing its public
advisory database; the existing duplicate `syn`/`winnow` warnings remain.

A separate generated 512 MiB file was interrupted during copying, resumed and
deeply verified. Source bytes/timestamps and the retained partial's digest stayed
unchanged. These local checks explicitly acknowledged same-volume byte testing;
they do not prove Windows runtime behavior, physical-drive independence or USB
disconnect/reconnect handling.

## Replication checks — 2026-10-11

The new raw-copy and deep-verification commands were exercised on macOS using
only synthetic files and temporary directories. All 33 Rust tests passed with
the Python worker required, including interruption/retry, root replacement,
capacity preflight, conflicts, changed source snapshots, incomplete publication,
schema migration and recovery from either drive's standalone database backup.
Formatting and workspace Clippy passed with warnings treated as errors.

All 28 image-worker tests, three repository tests (including the offline cloud
policy suite), and 62 offline cloud-probe tests passed. cargo-deny passed after
refreshing the public advisory database; the existing duplicate `syn`/`winnow`
warnings remain. Cargo.lock records the filesystem crate's added dependencies;
SQLite's backup feature is enabled without changing its resolved version.

The Windows filesystem crate and its test targets passed a cross-target compile
and Clippy check. This is **not Windows runtime verification**. PowerShell smoke
changes were prepared for both CI shells but had not run on that macOS host.
The later Windows results above cover synthetic two-drive behavior. Physical USB
disconnect/reconnect and a large-library benchmark remain outstanding.
The [Windows two-drive acceptance plan](windows-two-drive-test.md)
documents the manual checks now completed in the Windows results above. See
[replication and recovery](replication.md) for command behavior and limits.

## Earlier inventory and image-audit checks

Date: 2026-10-10. Local host: macOS, Apple Silicon; Rust/Cargo 1.96.0, uv 0.12.3, Python 3.14.7. Development uses only synthetic media and temporary directories. No private archives or cloud accounts were accessed.

## Completed local checks

- Rust formatting and Clippy across all workspace targets/features passed with warnings treated as errors.
- All 21 Rust tests passed, including the required Python worker integration test, byte-count overflow/negative-value regression coverage, and normalized manifest-path restrictions.
- All 28 Python tests passed, including HEIC, every EXIF rotation/mirroring orientation in JPEG and HEIC, decoder failures, path restrictions, and unchanged source bytes/mtime.
- Both Python SQLite schema/query tests passed.
- cargo-deny passed advisory, license, dependency-version, and source checks. Duplicate transitive versions of `syn` and `winnow` produce warnings; no security advisories are ignored.
- Cargo.lock and image-worker/uv.lock contain the resolved dependency versions used by these checks.

The SQLite compiler failure was fixed with checked conversions between public unsigned byte counts and SQLite's signed integers. HEIC fixture metadata is serialized to EXIF bytes so the encoder writes the intended orientation property. Path expectations distinguish native Windows separators from forward-slash manifest paths; non-normalized manifest input is rejected on every platform.

## Continuous integration

The [checks workflow](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml) runs formatting, Clippy, Rust tests, and Python/HEIC tests on macOS and Windows, with the worker required. A separate Linux job runs cargo-deny. CI uses the committed locks without dependency bootstrap or lockfile replacement. Consult the workflow run for a specific commit's platform results.

## Remaining limits

Windows checks now cover synthetic files on two physical drives. USB disconnects, private archives, a complete iCloud library and a 200 GiB library remain untested. The declared Rust minimum version has not been independently tested. The archive CLI has no cloud authentication, deletion or quarantine. Replica commands now distinguish independently checked local bytes from OS-reported physical independence and unverified cloud coverage; saved status does not reverify files.
