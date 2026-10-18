# Offline cloud safety prototype

Tests selection and recovery rules against an in-memory cloud model. There is no
network, account login or real cloud operation. The demo creates synthetic files
in a fresh temporary directory and retains them for inspection.

## Run

Python 3.12 or newer; no extra Python packages. Reconciliation tests also use the
built archive CLI:

```sh
cargo build --locked -p iarchive
python3 -m unittest discover -s tools/cloud-prototype -v
python3 tools/cloud-prototype/demo.py
```

Windows uses `python` instead of `python3`. With the project environment:

```sh
uv run --locked --project image-worker python -m unittest discover -s tools/cloud-prototype -v
uv run --locked --project image-worker python tools/cloud-prototype/demo.py
```

The default demo creates a sealed plan. Add `--simulate` to exercise the mock
operation. Type its printed phrase with the exact count and full plan digest.
Only mock memory changes; fixture bytes remain unchanged. Refused confirmation
or blocked operations exit nonzero.

Repository checks also run the suite:

```sh
uv run --locked --project image-worker python -m unittest discover -s tests -v
```

## What is tested

- Full account, zone, asset, master, owner, resource and version bindings.
- Separate original, edited, sidecar, motion, RAW/JPEG and alternate variants,
  including duplicate filenames.
- Independent SHA-256 reads of both replicas during planning and execution/resume.
- Refusal of unknown relationships, missing resources, unreadable files,
  incomplete inventories and unmapped extras, including outside a selected subset.
- Immutable target/resource plans, digests and exact confirmation. A seal detects
  edits; it is not a digital signature.
- Complete inventory preflight and an atomic generation/resource check in the mock.
  Concurrent changes and newly uploaded old photos stop execution.
- Durable intent before each mock request. Partial failures stop later targets.
  Lost replies remain uncertain until exact active/Recently Deleted inventories
  reconcile them; uncertain requests are never blindly retried.
- Retention in mock Recently Deleted. There is no expunge operation.

Tests forbid sockets. They also cover traversal, links, simulated reparse
attributes, disconnected replicas, stale credentials, scope changes, malformed
responses, interrupted audits and safe resume. Simulated Windows attributes do
not prove native junction behavior.

## Outputs

```text
<fresh temporary directory>/
  state/plan.json
  state/audit.jsonl       # after a simulation attempt
  replica-a/media/       # synthetic bytes
  replica-a/review/      # plan, audit and limits
  replica-b/media/
  replica-b/review/
```

Both review bundles contain matching plans/audits outside media.

## Reconcile saved reports

Compare a sanitized resource report, completed download provenance and an
iarchive **replica** JSONL manifest. An inventory export is insufficient.

```sh
python3 tools/cloud-prototype/reconcile.py --resource-report resource-report-private.json --download-manifest download-manifest-private.json --replica-manifest manifest.jsonl
```

This reads only the three reports and prints private JSON to stdout. It never
opens media, authenticates, makes requests or creates a cloud operation plan.
Inputs are limited to 16 MiB each and 20,000 records. Keep output private: it
contains full identities and relative paths.

Paths bind through download provenance. Defaults strip `copy-a/` and `copy-b/`
before comparison with archive-relative paths. Use `--primary-prefix` and
`--secondary-prefix` for a different recorded layout. Names, dates and equal
hashes never supply missing identities or path mappings.

The report checks account/library scope, asset/master/owner tags, every observed
resource, recorded sizes and response/copy/replica SHA-256 values. Unknown or
missing variants, ambiguous owners or paths, and unmapped files remain findings.
Opaque provider checksums are compared as metadata only.

New probe captures record the account binding privately. Older reports produce
`observation_account_unbound` and need a fresh capture. Zone IDs are compared as
complete objects, including returned owner fields. Missing owner identity produces
`observation_zone_owner_unbound`; no owner is inferred from the account.

Exit 0 means recorded observations match, 2 means findings, and 1 means invalid
input. Even a match leaves fresh local checks, physical independence, cloud
coverage, content relationships and removal approval unverified. Input digests
identify the compared bytes; they do not authenticate the reports.

## What remains unproved

**Passing this mock does not establish safe iCloud removal.** Its inventories,
stable generations, authoritative relationships and atomic conditional operation
have not been established for the live provider. Synthetic checksums must never
be substituted for opaque provider checksums.

Source inspection of icloudpd 1.32.3 found:

- `PhotoAsset.id` identifies a master; removal targets a separate asset record.
- The helper sends `update` with that asset's `recordChangeTag` and `atomic: true`.
  It sends no master/resource versions or inventory generation condition.
- The removal helper does not inspect per-record responses.
- Rank-based enumeration can miss concurrent changes.
- The version map omits some resources.

See the pinned [photo service](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/pyicloud_ipd/services/photos.py)
and [operation helper](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/icloudpd/base.py#L808).

Apple documents conflict rejection for `update` with a record change tag.
`atomic` groups the submitted operations in a custom zone; it does not provide
a condition on records omitted from the request. Successful HTTP transport also
does not establish per-record success. See [CloudKit modifications](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/ModifyRecords.html)
and [record tags](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/Types.html#//apple_ref/doc/uid/TP40015240-CH3-SW1).
These are general CloudKit contracts, not documentation of the private Photos API.

The mock race test changes an asset, master, resource or inventory generation
after preflight. It also inserts a newly uploaded old-dated asset. Rejection
retains all media, stops later targets and records an uncertain attempt. This
tests the mock's intended guarantees, not Photos server behavior.

Still unverified: whether the upstream removal request rejects stale asset tags
and whether master/resource changes always invalidate the asset tag. A separate
Favorite experiment accepted a stale-tag update, as recorded below. The request
contains no whole-inventory condition. The ordinary read-only probe blocks the
real pinned removal helper before transport in every scope.

Existing filenames cannot supply full identities or relationships. Future download
manifests need those bindings; existing media needs content reconciliation.
Complete library coverage, server mutation semantics, account compatibility,
remote checksum meaning and recovery behavior remain open.

The prototype uses trusted temporary directories. Path checks are not OS isolation
against hostile concurrent replacements. Production durability, device identity,
approval integrity and audit design need further work. Run one writer per state.

Read-only observations and offline tests cannot establish server mutation
semantics. Any live conflict experiment needs its own bounded plan and explicit
approval. Removal remains unavailable while these guarantees are unproved.

A separately guarded [Favorite conflict experiment](../cloud-probe/README.md#favorite-conflict-experiment)
is prepared with offline tests. It allows one stale-tag metadata update on the
synthetic upload, then checks the exact record outcome. Its first live preflight
stopped on unknown Favorite/master metadata before any update. A read-only check
then found a matching master reference but no `isFavorite` field. After a manual
Favorite change, a second diagnostic returned an explicit integer and a new
version tag. Omission stays unknown.

The revised live trial observed Favorite on at both captures around a manual
off/on cycle, with different version tags. A single stale-tag Favorite-clear
request was accepted; a fresh lookup and album capture confirmed Favorite off
and a new target tag. Other captured metadata stayed unchanged. This is negative
evidence for the tested request's conflict protection, not proof of deletion
semantics. No media was downloaded or removed.

That experiment omitted the pinned Photos wrapper's `remapEnums` and
`getCurrentSyncToken` flags. The helper now adds them to targeted requests and
records their values with the request evidence. Offline comparison confirms
the prepared common query parameters and body match the real pinned request;
the written field and return selection intentionally differ. A live repeat on
October 17, 2026 with both flags enabled also accepted one stale-tag update:
Favorite changed from `1` to `0`, with a new target tag and unchanged other
captured metadata. No media was downloaded or removed.

The bounded offline review found no additional enforceable condition in the
pinned provider or the documented modification request. See the
[concurrency decision](../cloud-probe/README.md#concurrency-decision) for the
alternatives and limits. Master/resource protection and removal semantics remain
unproved. No automated removal is enabled; the mock remains an intended policy,
not evidence that Photos enforces it.
