# Build and test report

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

Synthetic checks do not prove behavior with real Windows external drives, distinct physical-device identity, a complete iCloud library, or a 200 GiB library. The declared Rust minimum version has not been independently tested. No cloud authentication, deletion, quarantine, replication, or verified-backup claim is available. Reports retain **NOT VERIFIED** for local replication and cloud coverage.
