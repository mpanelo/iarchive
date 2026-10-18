# Read-only cloud probe

A separate research helper, not an `iarchive` command. It reuses pinned icloudpd
sign-in and streaming downloads. **Agree on the account, album and scope before
any live run.** The `probe.py` scopes below never modify Photos records or enable
removal. The separate [Favorite conflict helper](#favorite-conflict-experiment)
requires its own approval for one metadata update.

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

Type `READ ONLY ALBUM`. Approved fixture counts are 2, 4 or 5.
Unexpected counts stop the run before asset enumeration.

New captures bind the authenticated account ID privately in `album-private.json`
and `resource-report-private.json`; it is never printed in the summary. Zone IDs
retain their full returned structure. Missing owner identity stays unknown.

The probe rejects missing, duplicate or incomplete album matches. It makes two
metadata passes, bounded to 1,000 assets, comparing membership, full identities,
change tags and resource metadata. Membership, asset and master records stay
separate. Captions/locations are not requested; signed URLs and wrapping keys are
removed from output. Provider checksums remain opaque.
Capture and added dates (`assetDate` and `addedDate`) stay private as raw Unix
milliseconds. Missing dates remain unknown; dates never establish asset identity.

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
The Favorite value itself was not requested. The upload test below separately
checked new-upload detection. An atomic guard against later changes remains
unverified.

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

For a separately approved upload test, create one unique synthetic JPEG dated
January 2, 2001. The output's parent directory must already exist:

```sh
uv run --locked --project image-worker python fixtures/dated_upload.py --output .smoke/dated-upload
```

Capture the four-item album and a completed fresh delta **before uploading**.
Then upload the generated JPEG directly into the test album at
[iCloud Photos](https://www.icloud.com/photos). After it shows five items, capture
one new delta and the five-item album. Compare the new full asset ID, capture and
added dates with the baseline; keep the earlier selection unchanged. No media
downloads are needed. This tests observed new-upload detection, not complete
coverage or protection against later changes.

A live five-item test detected one added album member with a capture date of
January 2, 2001 and an added date between the pre/post-upload checkpoints. Its
full asset ID and version tag matched the post-upload delta, which returned seven
metadata records. That ID was absent from the earlier validated feed history.
The original four assets, masters and membership records stayed unchanged, as did
the saved baseline. No media was downloaded. This does not prove remote byte
identity, complete cloud coverage or an atomic removal guard.

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

## Favorite conflict experiment

`conflict.py` is a separate research helper. **Prepare and review it offline
first; live use requires separate approval.** It can attempt one Favorite update
on the synthetic JPEG identified by saved upload evidence. It never removes
media. The ordinary `probe.py` and `run-macos.command` retain their existing scope.

After approval, replace the evidence path, five-item baseline run and album name:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-probe/conflict.py --evidence .smoke/dated-upload/evidence-private.json --baseline cloud-probe-example --album 'Synthetic test album'
```

For a separately approved **read-only diagnostic**, add `--metadata-only` and
type `READ ONLY FAVORITE`. It captures the five-item baseline and one targeted
lookup, then stops without a toggle or update. Diagnostics contain only field
presence, value types and the master-reference match. `metadata_diagnostics_completed`
means diagnostics were collected; missing values remain unknown.

The first live preflight stopped before any toggle or update because the Favorite
value or master binding was unknown.
A read-only diagnostic then confirmed that the master reference matched, while
`isFavorite` was absent from the lookup's fields. It used one targeted lookup and
zero updates. Absence remains unknown; it is not interpreted as `false`.
A second diagnostic after the user favorited the image returned an explicit
integer Favorite value. The target version changed; other captured metadata and
membership stayed unchanged. Both diagnostic runs sent zero updates.

In the revised live trial, Favorite was explicitly `1` before and after the manual
off/on cycle, with different version tags. One `update` carrying the older tag
and only `isFavorite: 0` was accepted. Three targeted lookups and fresh album
captures confirmed Favorite became `0`, the target tag changed, and other
captured album/resource metadata stayed unchanged. No conflict protection was
established for this request. No media was downloaded or removed.

That trial omitted the Photos wrapper's `remapEnums` and `getCurrentSyncToken`
flags. A repeat on October 17, 2026 included both as `True` and had the same
result: one stale-tag update was accepted. Favorite was explicitly `1` at both
pre-write captures and `0` afterward. The target tag changed; other captured
membership, relationships and resource metadata stayed unchanged. The repeat
used three targeted lookups and one update, with no downloads or removals.

An offline integration test compares the helper's prepared query parameters and
common request body with the real pinned provider's request. The flags are saved
as `photo_query_flags` in the intent, outcome and summary. These Favorite trials
do not establish deletion behavior or general concurrency guarantees. No further
live request is automatic.

For the separately approved write trial:

1. Type `FAVORITE CONFLICT TEST` and authenticate locally.
2. Start with the synthetic photo favorited. After the helper captures it, turn
   Favorite off and back on for that photo only. Leave its heart on and type
   `CYCLED`.
3. The helper checks five-item membership, relationships, resource metadata and
   the changed asset tag. Type `TRY ONE STALE FAVORITE` to attempt one update with
   the old tag, attempting to clear Favorite. Both captured Favorite values must
   explicitly be `1`, and the version tags must differ.
4. Leave Photos alone while it checks the result. Restore Favorite manually
   afterward if desired; there is no automatic restoration.

The protection test requires an exact per-record `CONFLICT` plus unchanged
Favorite, version tag and other captured metadata afterward. Acceptance,
unrelated errors, lost replies or failed post-checks are inconclusive. If accepted, Favorite may turn off
on this one photo. A missing post-check field remains unknown. A timeout never
causes a write retry.

The helper binds account, full private zone, album, asset and master IDs to the
saved evidence and baseline hashes. These detect changed inputs, not forged
reports or remote byte identity. Fresh metadata must agree before the trial.
Missing Favorite values remain unknown. Unexpected schema or scope stops it.

Request starts are at least one second apart. Targeted exchanges allow three
lookups and one update, with a 256 KiB response cap, 45-second stream budget and
10/30-second connection/read timeouts. Album metadata uses the existing bounded
two-pass reader with an additional 256 KiB response cap. Server-requested pauses
stop further requests, including the post-check. No redirects or write retries.

Private outputs use fresh `.smoke/cloud-probe-*` directories: the bound evidence,
three album/lookup observations, exclusive file-flushed write intent, a sanitized
outcome and `summary.json`. Interrupted attempts are not resumable. Never reuse
their intent to send again. A new trial requires a fresh capture and confirmation.
Only `stale_update_rejected: true` records the observed conflict result; it does
not prove deletion behavior, master/resource protection or whole-library safety.

## Concurrency decision

The bounded offline review on October 17, 2026 found no additional enforceable
version condition in the pinned provider. Automated archive cleanup remains
unavailable; these findings do not authorize deletion.

| Mechanism | Finding |
| --- | --- |
| `update` with `recordChangeTag` | Already present in the pinned removal request. Both Favorite trials accepted an older tag. Rejection of stale deletion tags remains unverified. |
| `atomic: true` | Groups submitted operations; it does not condition a write on omitted master/resource records or the whole inventory. |
| Change-feed `syncToken` | A read checkpoint. The documented modification request has no whole-zone token condition. `getCurrentSyncToken: true` did not prevent the observed stale Favorite update. |
| `replace` or `delete` | Different mutations, not extra guards for the Photos `isDeleted` update. Replacing can clear omitted fields; deleting a CloudKit record does not establish Photos removal or recovery behavior. |

Sources: Apple's [modification contract](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/ModifyRecords.html),
[record tags](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/Types.html#//apple_ref/doc/uid/TP40015240-CH3-SW1)
and [change checkpoints](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/FetchingRecordZoneChanges%28changeszone%29.html),
plus the pinned [removal helper](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/icloudpd/base.py#L808)
and [Photos wrapper](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/pyicloud_ipd/services/photos.py).
The Apple references describe general CloudKit, not the private Photos endpoint.

An extra preflight read or an upstream allowlist can narrow selection but cannot
close the interval between the final read and the write. Reopening automation
research requires a concrete server-enforced condition and separately approved
evidence for Photos behavior, resource changes and recovery. Manual selection in
Photos remains a user action; local reports cannot certify it as safe removal.

The separate [one-photo trial](../../docs/cloud-cleanup-trial.md#recorded-result)
completed on macOS on October 18, 2026 (UTC). One original download matched the
synthetic source's SHA-256, and one soft-delete update was accepted. The album
changed from five items to four, then back to five after manual restoration.
The other items stayed unchanged; restored identity and resource metadata matched.
Recently Deleted visibility was user-reported. Stale-tag rejection, restored
remote bytes and reclaimed storage remain unverified. Preparation defaults to
offline; each live run still needs separate approval.

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

The pinned removal helper includes the asset's version tag, but no separate
master/resource versions or inventory condition. Apple documents conflict
rejection for that general CloudKit request form; the Photos Favorite trials
accepted stale tags. Stale-tag rejection for Photos deletion remains unverified.
Our offline integration test invokes the real helper with synthetic records and
proves that every probe scope blocks it before transport. See the
[concurrency findings](../cloud-prototype/README.md#what-remains-unproved) for
the distinction between documented conditions and the mock's stronger checks.

## Offline checks

These fake-response tests forbid sockets and need no account:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python -m unittest discover -s tools/cloud-probe -v
```

CI and Windows tests run this separately from image-worker tests. Missing provider
dependencies fail instead of skipping integration coverage.
