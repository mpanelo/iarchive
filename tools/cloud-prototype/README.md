# Offline cloud safety prototype

This experiment tests conservative selection and recovery rules against an
in-memory cloud model. It is separate from the `iarchive` CLI and image worker.
There is no network transport, account login, or real cloud operation. The demo
accepts no archive paths or credentials; it creates every byte in a new temporary
directory. It leaves those synthetic fixtures and review bundles for inspection.

Python 3.12 or newer is sufficient; no extra packages are required.

```sh
python3 -m unittest discover -s tools/cloud-prototype -v
python3 tools/cloud-prototype/demo.py
```

On Windows, use `python` in place of `python3`. With the existing project
environment, the equivalent is:

```sh
uv run --locked --project image-worker python -m unittest discover -s tools/cloud-prototype -v
uv run --locked --project image-worker python tools/cloud-prototype/demo.py
```

The normal demo creates a sealed review plan without simulating any operation.
To exercise the in-memory operation, add `--simulate`. It prints a confirmation
phrase containing the exact count and full plan digest; type that phrase at the
prompt. This changes only the mock's memory and writes an audit. Fixture media
bytes remain unchanged. A refused confirmation or blocked operation exits with
a nonzero status.

The suite also runs through the repository's existing command:

```sh
uv run --locked --project image-worker python -m unittest discover -s tests -v
```

## Evidence exercised

- Account, database, zone and owner, full asset and master record IDs, both change
  tags, and every declared resource identity/version are explicit inputs.
- Fixtures include original, adjusted, sidecar, Live Photo motion, RAW/JPEG and
  alternative resources, with repeated basenames. Each variant stays separate.
- Full bytes are independently read and SHA-256 hashed in both replica
  directories during planning and again before each simulated operation or
  resume. Equal filenames, sizes and mtimes do not prove content equality.
- Unknown relationships, missing resources, unreadable files, unrecognized
  variants, incomplete inventories and unmapped extra files block planning.
  This prototype conservatively requires all inventoried fixture assets to have
  complete mappings, including assets outside a selected subset.
- Canonical plans bind the complete baseline inventory, exact target set,
  resource manifest and digest, creation time, count, byte total, scope and paths.
  Frozen payloads and SHA-256 seals detect edits; confirmation binds the exact
  seal. A seal is an integrity check, not a digital signature.
- Every operation checks the current complete inventory, then the mock checks
  its generation and exact asset/resources atomically when applying the change.
  New old-dated uploads and changes between preflight and application stop it.
- An fsynced append-only audit records intent before a mock request. Partial
  failures stop later targets. Lost replies and interruptions remain uncertain
  until the exact remaining and mock Recently Deleted inventories prove the
  expected outcome; uncertain requests are never blindly retried.
- The mock retains all simulated removals in Recently Deleted. There is no
  expunge operation. The demo exports identical plans and audits outside each
  replica's media root.

The generated layout is:

```text
<new system temporary directory>/
  state/plan.json
  state/audit.jsonl          # present only after an attempted simulation
  replica-a/media/           # generated synthetic bytes
  replica-a/review/          # plan, optional audit, limitations
  replica-b/media/
  replica-b/review/
```

Tests forbid socket creation and cover missing variants, binding ambiguity,
corrupt or disconnected replicas, path traversal, symlinks, simulated Windows
reparse attributes, scope changes, stale credentials, new uploads, resource
changes, inventory drift, conditional-operation races, per-record errors,
unverified success responses, interrupted audits and safe resume. Native Windows
junction handling and physical external drives require separate Windows runs;
a simulated attribute test does not establish that runtime behavior.

## Feasibility boundary

**Passing this experiment does not establish safe automated iCloud removal.**
Two temporary directories do not prove independent physical drives. The report
therefore says `NOT VERIFIED` for physical-drive independence and live cloud
coverage. No iPhone upload, real resource decoding, real recovery or Apple server
behavior is tested.

The mock deliberately supplies evidence not established for the real provider:
complete inventories with a stable generation, authoritative resource
relationships, synthetic SHA-256 checksums, and an atomic conditional operation
over that generation and all resource metadata. These are requirements exposed
by the experiment, not an adapter contract proven to exist upstream.

Source inspection of `icloudpd` 1.32.3 found that the exposed `PhotoAsset.id`
identifies the master, while its removal helper targets a separate asset record.
The helper does not inspect per-record responses; rank-based enumeration can
miss assets during concurrent changes, and its version map omits some requested
resources. See the pinned [photo service source](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/pyicloud_ipd/services/photos.py)
and [operation helper](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/icloudpd/base.py#L808).
The remote checksum's equivalence to SHA-256 has not been established.
The prototype's `synthetic_sha256` field must never be populated by guessing the
algorithm of an opaque upstream checksum.

Existing archive filenames cannot supply these missing full identities or
resource relationships. A future download manifest would need to capture those
facts, and existing downloads would need separately demonstrated content
reconciliation. Complete Personal/Shared/Hidden inventory coverage, server
mutation semantics, account compatibility and recovery behavior remain open.

The experiment uses owned, trusted temporary directories. Static link/reparse
checks and leaf no-follow opens are useful regression checks, not production
filesystem isolation against concurrent directory replacement. Portable durable
storage, device identity, immutable archive handling, signed approval and audit
integrity also need production design. Use one simulation runner per state
directory; concurrent execution against a shared journal is unsupported.

The useful conclusion is that a conservative policy can be tested offline.
The next feasibility question is whether a read-only adapter can actually
obtain the evidence the mock supplies. Live authentication or sacrificial cloud
tests require separate explicit authorization; this prototype provides neither.
