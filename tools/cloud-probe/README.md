# Read-only cloud probe

A separate research helper, not an `iarchive` command. It reuses pinned icloudpd
sign-in and streaming downloads. **Agree on the account, album and scope before
any live run.** Nothing modifies Photos records or enables removal.

| Scope | Access |
| --- | --- |
| `auth` | Sign-in, verification and session trust. |
| `libraries` | Authentication plus private/shared library zones. |
| `album` | One approved album's metadata. |
| `originals` | Exactly two approved originals from a completed baseline. |
| `variants` | Four originals, one motion companion and one full-image rendition from a four-asset baseline. |
| `changes` | Identity/version changes across one separately approved private zone. |

Unexpected endpoints, queries, redirects and resource selections are blocked.
This is a bounded experiment, not a complete cloud inventory.

## Prepare on macOS

From the checkout:

```sh
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.smoke/cloud-tools/python"
uv sync --locked --project tools/cloud-probe --python 3.12
```

The provider needs Python below 3.14. Managed Python stays in the checkout; the
venv is `tools/cloud-probe/.venv`. The lock uses the official pure-Python provider
wheel so the helper can guard its transport.

## Authenticate and discover libraries

After approval, run in your own interactive Terminal:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-probe/probe.py libraries
```

Or double-click `run-macos.command`, which defaults to `libraries` and sets the
local environment. From a shell it forwards supplied arguments.

Type `READ ONLY LIBRARIES`. Enter account credentials locally; inputs are hidden.
Upstream requests a push code. If it does not arrive, choose a displayed masked
phone number for SMS. Never send passwords or codes in chat.

The helper uses upstream login, not its own protocol or a keyring provider. Some
account configurations remain unsupported. It does not change security settings.
See [upstream authentication](https://icloud-photos-downloader.github.io/icloud_photos_downloader/authentication.html).

## Read one test album

After agreeing on the album and metadata-only scope:

```sh
tools/cloud-probe/run-macos.command album --album 'Synthetic test album' --expected-assets 4
```

Type `READ ONLY ALBUM`. Use `--expected-assets 2` for the two-item fixture.
Unexpected counts stop the run before asset enumeration.

New captures bind the authenticated account ID privately in `album-private.json`
and `resource-report-private.json`; it is never printed in the summary. Zone IDs
retain their full returned structure. Missing owner identity stays unknown.

The probe rejects missing, duplicate or incomplete album matches. It makes two
metadata passes, bounded to 1,000 assets, comparing membership, full identities,
change tags and resource metadata. Membership, asset and master records stay
separate. Captions/locations are not requested; signed URLs and wrapping keys are
removed from output. Provider checksums remain opaque.

For session reuse, add `--reuse-session cloud-probe-<run-name>`. Only a successful
matching run beneath this checkout's `.smoke` is accepted. Upstream revalidates
it and may ask for credentials. Reuse updates that run's sensitive cookie cache;
new outputs get their own directory. Run one probe at a time.

## Resource report

`resource-report-private.json` keeps each resource's full owner identity/tag,
field, size, type, opaque checksum and URL availability. Assets reference masters
through returned `masterRef`; equal names/checksums do not merge resources.

| Provider field | Label |
| --- | --- |
| `resOriginalRes` | Original |
| `resOriginalAltRes` | Alternate original |
| `resOriginalVidComplRes` | Original video companion |
| `resJPEGFullRes`, `resVidFullRes` | Full image/video |
| `resSidecarRes` | Sidecar |
| Other requested JPEG/video sizes | Preview |
| Unexpected fields | Unknown |

Labels follow the [pinned provider source](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/pyicloud_ipd/services/photos.py).
The report uses raw requested fields because the provider's version lookup omits
some. It makes no extra queries.

Missing originals, types or URLs, empty resources, ambiguous owners, shared or
unreferenced masters and unknown fields remain findings. Ambiguous resources
stay separate. Pair confidence remains `unknown`; record links do not prove
media content relationships.

Offline fixtures cover originals, renders, motion, video, sidecars and RAW/JPEG.
`resources.build_resource_report()` can compare full asset IDs with explicit
expected field lists. Live runs currently use `expectation_status: NOT PROVIDED`.
Even `MATCHED OBSERVED FIELDS` proves metadata agreement only. Unrequested or
unknown variants may remain undiscovered.

## Investigate a change feed

A live traversal completed in two linked batches: 113 initial pages containing
10,912 metadata records, then one empty delta page. An earlier 10-page experiment
stopped at its cap. After a Favorite toggle in a four-item test album, exactly
one asset changed version tag. A one-page delta returned that same full asset ID
and the tag seen in a fresh album capture, plus three unrelated metadata records.
Membership, master records and other requested asset fields stayed unchanged.
The Favorite value itself was not requested. New-upload detection and an atomic
guard against later changes remain unverified.

This scope reads beyond an album. Agree on the account and entire private zone
first; prefer a disposable account. Earlier album approval does not cover this scope.

After separate approval, use the exact name and owner returned by library
discovery. Replace both example values:

```sh
tools/cloud-probe/run-macos.command changes --zone-name 'approved-zone' --zone-owner 'returned-owner'
```

Type `READ ONLY CHANGES`. Authentication and private/shared zone discovery happen
first; record requests are restricted to the selected private zone's full identity.
Missing owners, deleted zones, ambiguous matches and the default zone are refused.
There is no album filter. Keep zone identities and outputs private.

The probe requests only record IDs, types and version tags, across all record
types. It drains an initial feed and one follow-up delta. Changes and tombstones
remain separate observations; unknown types are preserved. Other returned record
fields, including captions, locations, URLs and keys, are discarded.

Both phases share limits: 100 pages, 10,000 records and 8 MiB of response bytes,
with at most 100 records and 2 MiB per page. A 120-second budget is checked between
reads; connection/read timeouts are 10/30 seconds. Redirects, transport retries,
expired tokens, non-progressing tokens and incomplete responses stop the run.
There is no automatic restart or chaining of batches.

After separate approval, continue one batch from a run stopped by its page,
record or time budget. Replace the example run name:

```sh
tools/cloud-probe/run-macos.command changes --continue-from cloud-probe-example
```

The checkpoint supplies the zone. The probe validates all linked reports and
tokens, then requires the same authenticated account and full discovered zone
before record requests. Each batch uses fresh output and the same per-run limits.
Once the initial feed ends, it starts or continues the follow-up delta.

Keep every linked run. Missing, edited, unsafe or inconsistent history blocks
continuation, as do throttled or otherwise failed source runs. History is bounded
to 20 runs including the new batch and 64 MiB of input reports. Expired tokens
never trigger a restart. Report hashes detect changes to earlier linked bytes;
they do not authenticate the reports.

After separate approval, request one new delta from a **completed** capture:

```sh
tools/cloud-probe/run-macos.command changes --since-run cloud-probe-completed
```

This validates the completed reports, final token and linked history, then uses
the same account, full zone, pacing and limits. It never repeats the initial
traversal or polls automatically. Budget-stopped runs use `--continue-from`.

A controlled edit test uses fresh metadata for the disposable test album. After
the baseline is confirmed, toggle Favorite on one item and run one approved delta.
Compare exact asset IDs and version tags with a fresh album capture. An observed
tag change demonstrates visibility only; it does not prove Favorite semantics,
complete coverage or an atomic guard against later changes.

Change-feed request starts are at least one second apart. Slow responses count
toward that interval. HTTP 429/503, CloudKit throttling errors or a server retry
delay stop the run without another request. The summary reports
`retry_after_seconds` when supplied; wait at least that long before any separately
approved retry. Missing or invalid delays stay unknown. When both the HTTP header
and JSON body supply delays, the longer delay applies. See Apple's
[throttling guidance](https://developer.apple.com/documentation/technotes/tn3162-understanding-cloudkit-throttles).

Tokens and sanitized pages stay in private files. Each page is saved before the
next request. Only `summary.json` with `change_feed_completed: true` establishes
that both phases finished across the validated chain. Partial pages never
establish completion. Per-run counts and cumulative observation counts are
separate; neither is a count of unique photos.

The request shape follows Apple's [CloudKit change-feed documentation](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/FetchingRecordZoneChanges%28changeszone%29.html).
Photos accepted the initial paginated requests. Reaching the end of a feed does not prove
complete library/resource coverage, an atomic snapshot, completed iPhone uploads
or protection against changes after the final read. These remain unverified;
there is no cloud modification or removal command.

## Download two originals

Requires separate download approval and a completed two-asset/two-master baseline.
Older baselines without an account binding need a fresh metadata capture.
An account mismatch stops the run before library or album queries.

Replace `cloud-probe-example` with that run's basename:

```sh
tools/cloud-probe/run-macos.command originals --album 'Synthetic test album' --baseline cloud-probe-example
```

Type `DOWNLOAD TWO ORIGINALS`. Full identities, tags and metadata must still match.
Only `resOriginalRes` is fetched. Limits: 256 MiB and 180 seconds per original,
plus a 30-second provider request timeout. Ambiguity, unsafe URLs or drift block
it. There is no automatic retry or partial-byte resume.

## Download six resources

Requires separate approval and a four-asset/four-master metadata baseline:

```sh
tools/cloud-probe/run-macos.command variants --album 'Synthetic test album' --baseline cloud-probe-example
```

Type `DOWNLOAD SIX RESOURCES`. Selection is exactly four `resOriginalRes`, one
`resOriginalVidComplRes` and one `resJPEGFullRes`. Limits: 32 MiB per resource,
64 MiB total and 180 seconds per resource, plus the provider timeout.

Owner IDs/tags, fields, types, sizes and checksums are bound separately. Missing
or ambiguous selected owners, drift, unexpected counts or normalized URL
collisions block the run. Unselected preview/sidecar/alternate metadata remains
in the report; it is not permission to download those resources.

## Download checks

Both download scopes use single-use approved URLs held only in memory. They
stream through the pinned provider, check declared byte counts and full ranges,
and reject error-document content types. Fresh files use exclusive creation,
generated names and link/reparse checks. Bytes are never converted.

Each file is copied into a second directory and both copies are independently
SHA-256 hashed. Partial files and progress are retained after failure. A final
metadata pass checks drift. Success requires `resource_download_completed` in
`summary.json`; the older originals scope also records `original_download_completed`.

Equal local hashes do not prove a remote SHA-256 match or the provider checksum
algorithm. Both copies are on one filesystem. Playback, decoding, Live Photo
pairing and edit matching need separate content checks. None of this proves
complete archival coverage or independent backups.

## Keep outputs private

Every run creates `.smoke/cloud-probe-<id>`:

| Output | Contents |
| --- | --- |
| `summary.json` | Status, counts and findings; the only printed output. |
| `cookies/` | Sensitive upstream sessions. Never commit or share. |
| `zones-private.json` | Returned zone identities from discovery. |
| `album-private.json` | Album/asset/master identities and sanitized passes. |
| `resource-report-private.json` | Resource ownership and metadata findings. |
| `album-pass-*-page-*.json` | Partial progress; incomplete until the summary says otherwise. |
| `copy-a/`, `copy-b/` | Private downloaded test media, for download scopes only. |
| `download-plan-private.json`, progress files | Approved bindings and per-file progress. |
| `download-manifest-private.json` | Completed local copy checks. |
| `post-download-private.json` | Sanitized final metadata. |
| `changes-context-private.json` | Account and full zone binding for a change-feed attempt. |
| `changes-page-*-private.json` | Sanitized change events and tokens, including partial progress. |
| `changes-private.json` | Final token and phase counts after a completed change-feed capture. |

Do not upload or commit private reports, media or cookies. macOS run directories
use mode `0700` and a restrictive umask. Raw responses, signed URLs, wrapping
keys, exceptions and debug logs are suppressed. Failures retain explicit findings
and exit nonzero.

Matching passes do not prove an atomic snapshot or complete Personal/Shared/Hidden
coverage. Missing owner identity stays absent, never invented. macOS results do
not establish Windows behavior. Removal tests need their own approval; private
archives are never development fixtures.

## Offline checks

These fake-response tests forbid sockets and need no account:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python -m unittest discover -s tools/cloud-probe -v
```

CI and Windows tests run this separately from image-worker tests. Missing provider
dependencies fail instead of skipping integration coverage.
