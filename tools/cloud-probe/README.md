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

## Download two originals

Requires separate download approval and a completed two-asset/two-master baseline.
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
