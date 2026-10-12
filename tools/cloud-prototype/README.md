# Offline cloud safety prototype

Tests selection and recovery rules against an in-memory cloud model. There is no
network, account login or real cloud operation. The demo creates synthetic files
in a fresh temporary directory and retains them for inspection.

## Run

Python 3.12 or newer; no extra packages:

```sh
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

## What remains unproved

**Passing this mock does not establish safe iCloud removal.** Its inventories,
stable generations, authoritative relationships and atomic conditional operation
have not been established for the live provider. Synthetic checksums must never
be substituted for opaque provider checksums.

Source inspection of icloudpd 1.32.3 found:

- `PhotoAsset.id` identifies a master; removal targets a separate asset record.
- The removal helper does not inspect per-record responses.
- Rank-based enumeration can miss concurrent changes.
- The version map omits some resources.

See the pinned [photo service](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/pyicloud_ipd/services/photos.py)
and [operation helper](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/icloudpd/base.py#L808).

Existing filenames cannot supply full identities or relationships. Future download
manifests need those bindings; existing media needs content reconciliation.
Complete library coverage, server mutation semantics, account compatibility,
remote checksum meaning and recovery behavior remain open.

The prototype uses trusted temporary directories. Path checks are not OS isolation
against hostile concurrent replacements. Production durability, device identity,
approval integrity and audit design need further work. Run one writer per state.

Next: determine whether a read-only adapter can obtain the mock's required
evidence. Live authentication and disposable cloud tests require separate approval.
