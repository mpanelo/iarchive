# Offline cloud safety prototype

Tests selection and recovery rules against an in-memory cloud model. There is no
network, account login or real cloud operation. The demo creates synthetic files
in a fresh temporary directory and retains them for inspection.

## Run

Python 3.12 or newer; no extra Python packages. Tests also use the built archive CLI:

```sh
cargo build --locked -p iarchive
uv run --locked --project image-worker python -m unittest discover -s tools/cloud-prototype -v
uv run --locked --project image-worker python tools/cloud-prototype/demo.py
```

The default demo creates a sealed plan. Add `--simulate` to exercise the mock
operation. Type its printed phrase with the exact count and full plan digest.
Only mock memory changes; fixture bytes remain unchanged. Refused confirmation
or blocked operations exit nonzero.

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

Tests forbid sockets and cover path, credential, scope, interruption and response
failures. Simulated Windows attributes do not prove native junction behavior.

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

The mock race test changes an asset, master, resource or inventory generation
after preflight. It also inserts a newly uploaded old-dated asset. Rejection
retains all media, stops later targets and records an uncertain attempt. This
tests the mock's intended guarantees, not Photos server behavior.

See the probe's [recorded observations](../cloud-probe/README.md#recorded-observations)
and [concurrency decision](../cloud-probe/README.md#concurrency-decision) for live
findings, upstream request behavior and sources. The mock's atomic checks are
stronger than anything established for Photos. No automated removal is enabled.

The prototype uses trusted temporary directories. Path checks are not OS isolation
against hostile concurrent replacements. Production durability, device identity,
approval integrity and audit design need further work. Run one writer per state.
