# Read-only cloud access probe

This research helper is separate from the `iarchive` executable. It reuses pinned
`icloudpd` authentication and streaming downloads, with four explicit scopes:

| Scope | Requests allowed |
|---|---|
| `auth` | Apple sign-in, verification and session trust only |
| `libraries` | The same authentication plus private/shared library zone discovery |
| `album` | Library discovery, album-name lookup, and metadata for one explicitly approved test album |
| `originals` | The same metadata plus the exact two original-resource URLs from a completed test baseline |

The first two scopes cannot list albums or photo records. The album scope checks
both endpoint and query body: asset queries must name the exact approved album
and complete discovered zone identity. The originals scope permits each approved
URL once; previews, other originals and redirects to unapproved URLs are blocked.
No scope modifies Photos records. Unexpected endpoints stop the probe.
This is a bounded access test, not a complete cloud inventory or a removal test.

## Run locally on macOS

From the repository root, prepare the isolated environment:

```sh
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.smoke/cloud-tools/python"
uv sync --locked --project tools/cloud-probe --python 3.12
```

The pinned provider requires Python below 3.14. This command lets uv install a
compatible Python inside the checkout and keeps its virtual environment in
`tools/cloud-probe/.venv`. It does not change the system Python.
The lock selects the official pure-Python wheel so the helper can import the
provider and guard its transport; the platform wheel instead bundles executables.

After approving the intended scope, run **directly in your own interactive
Terminal**, with either `auth` or `libraries`:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-probe/probe.py libraries
```

On macOS, `tools/cloud-probe/run-macos.command` opens in Terminal when
double-clicked and applies those checkout-local environment settings. Its
default scope is `libraries`; from a shell it forwards all supplied arguments.

Type `READ ONLY LIBRARIES` at the scope prompt. Then enter your account email
and password locally. The upstream two-factor flow explicitly requests a push
notification to your trusted devices. If no code arrives, enter the letter beside
one of the displayed masked phone numbers to request an SMS, then enter that
code. Account/password/code inputs are hidden. Do not send credentials or
verification codes in chat. This helper uses
upstream authentication rather than implementing its own login protocol, and
does not use a keyring password provider. Upstream documents its console
authentication and account compatibility in the [authentication guide](https://icloud-photos-downloader.github.io/icloud_photos_downloader/authentication.html).

Authentication has not been verified for every account configuration. The probe
does not change security settings or attempt to work around unsupported ones.
Apple may issue its normal sign-in notifications or verification prompts.

## One approved test album

After agreeing on a test album and metadata-only scope, run:

```sh
tools/cloud-probe/run-macos.command album --album 'Synthetic test album'
```

Type `READ ONLY ALBUM` at the local prompt. The probe locates that exact album
name and rejects absent, duplicate or incomplete matches. It makes two bounded
metadata passes (at most 1,000 assets), checks membership counts before and after
each pass, and compares full record identities, change tags and resource metadata.
Album-membership relation records are preserved separately from assets and masters.
It requests neither captions nor locations. Signed media URLs and wrapping keys
are removed before output; only URL availability is recorded. Provider checksums
are opaque values, not assumed to be SHA-256.

To reuse a previous authenticated session, append
`--reuse-session cloud-probe-<run-name>` using the basename printed by that run.
Only a run under this checkout's `.smoke` with a matching provider and successful
authentication summary is accepted. Upstream revalidates the session and may
still request credentials or verification. It updates that previous run's sensitive
cookie cache; the new run gets its own output directory. Run only one probe at a time.

## Resource observations

Album metadata runs also create `resource-report-private.json`. Each observed
resource remains a separate entry with its full owner-record identity and change
tag, field name, declared size, opaque checksum, file type, and URL availability.
The report retains the album and zone identities. Asset entries reference those
resources through the returned `masterRef`; duplicate names or checksums do not
merge entries. Signed URLs and wrapping keys are removed before persistence.

The labels describe returned fields rather than proving their media contents:

| Returned resource fields | Report label |
|---|---|
| `resOriginalRes` | Original |
| `resOriginalAltRes` | Alternate original |
| `resOriginalVidComplRes` | Original video companion |
| `resJPEGFullRes`, `resVidFullRes` | Full image or video |
| `resSidecarRes` | Sidecar |
| Other requested JPEG/video sizes | Preview image or video |
| Unexpected returned resource fields | Unknown |

These field interpretations follow the [pinned provider source](https://github.com/icloud-photos-downloader/icloud_photos_downloader/blob/879c561240d993d748ddb4546f935090502b16d3/src/pyicloud_ipd/services/photos.py).
The helper inventories the requested raw fields rather than relying on the
provider's version lookup, which does not include every requested field.
It makes no extra metadata queries to generate this report.

Findings include an absent original, metadata without a corresponding resource,
the same resource field on both asset and master, shared or unreferenced masters,
unknown fields, empty resources, and missing file types or URLs. Both owners'
entries remain present when ownership is ambiguous. Pair confidence stays
`unknown`: a record association alone does not prove a Live Photo or RAW/JPEG
relationship within the media bytes. Printed summaries contain only role and
finding counts; full identities and unexpected field labels remain private.

The offline fixture suite covers Live Photo still/motion, video, original/full-size
render, sidecar, and RAW/JPEG examples, including either RAW/JPEG resource order.
`resources.build_resource_report()` can also compare a sanitized observation with
an explicit mapping from full asset IDs to exact expected resource-field lists.
It reports missing and unexpected fields and how many assets had expectations.
Those expectations describe the caller's disposable fixtures; they are not
inferred from filenames, opaque subtype flags, or checksums. Live runs currently
report `expectation_status: NOT PROVIDED`.

Even `MATCHED OBSERVED FIELDS` proves only that the supplied field expectations
matched the observed metadata without resource findings. No media content is
verified by this report. The query requests a known projection of fields, so
unreturned or unknown variants may remain undiscovered. Complete resource
inventory, content mapping, and the provider checksum algorithm remain
`NOT VERIFIED`. A live variant test needs a separately agreed disposable album
and read/download scope.

## Two approved originals

This separate scope requires explicit download approval and a successful metadata
run containing exactly two assets and two masters. Replace `cloud-probe-example`
below with the basename of that metadata run:

```sh
tools/cloud-probe/run-macos.command originals --album 'Synthetic test album' --baseline cloud-probe-example
```

Append `--reuse-session cloud-probe-<authenticated-run>` to reuse a session, as
above. Type `DOWNLOAD TWO ORIGINALS` at the local prompt. The probe repeats the
album metadata passes and requires the full identities, change tags and resource
metadata to match the baseline. It fetches only `resOriginalRes`, retaining signed
URLs in memory only. Ambiguous ownership, unsafe URLs, extra assets and changed
metadata stop the test. Each original is limited to 256 MiB and 180 seconds, plus
the provider's 30-second per-request timeout. No automatic retry or partial-file
resume is attempted; partial bytes and per-file progress remain available.

The pinned provider opens each streamed download. Byte counts must match the
declared resource size, partial HTTP ranges must cover the full object, and known
error-document content types are rejected. Fresh files use generated names, with
exclusive creation and link/reparse checks. Each download is copied into a second
directory, then both files are reopened and independently SHA-256 hashed. A third
metadata pass checks for changes during the test.

Matching hashes show that the two local files contain the same bytes received
from the selected original-resource request. They do not establish the algorithm
of the provider's opaque checksum or an independent remote SHA-256 match. Both
directories are on the checkout's filesystem: disk failure can lose both copies.
Physical backup independence requires separately verified drives. This is a
bounded download/mapping test; it does not establish complete cloud backup or
enable removal.

## Outputs and interpretation

Each run creates a new ignored directory beneath `.smoke/cloud-probe-*`:

- `summary.json`: authentication status, discovery status, request counts,
  private/shared library counts, missing owner-identity indications and findings.
- `zones-private.json`: complete returned zone identities, only for the
  `libraries`, `album` and `originals` scopes. Keep this local; it is unnecessary to share it to report
  whether the probe succeeded.
- `cookies/`: sensitive upstream session and cookie files. Keep these local
  and never commit, upload or paste them into chat. On macOS the run directory
  is created with mode `0700` and files with a restrictive process umask.
- `album-private.json`: full album, asset and master IDs, change tags and sanitized
  resource metadata from both passes, plus the resource report for the second
  pass. Keep local; it contains account-specific IDs.
- `resource-report-private.json`: separate resource entries, full owner bindings,
  asset/master associations, and findings for the observed resource metadata.
  `metadata_passes_match` records whether the two observations agreed. Keep local.
- `album-pass-*-page-*.json`: sanitized page progress, retained even if interrupted.
  These partial pages are explicitly incomplete; use the summary to check completion.
- `copy-a/` and `copy-b/`: the two downloaded originals and their local copies,
  only for `originals`. These are private test media; never commit or upload them.
- `download-plan-private.json` and per-file progress/verification files: full
  account/database/zone/asset/master/resource bindings and local hash results.
- `download-manifest-private.json`: completed local copy verification. Overall
  success additionally requires `original_download_completed` in the summary,
  which includes the post-download cloud metadata comparison.
- `post-download-private.json`: sanitized metadata after the download test.

Only the summary is printed. Upstream response bodies, signed URLs, exception
messages and debug logs are suppressed. A blocked or incomplete run exits with
a nonzero status and keeps an explicit finding. An absent owner identity is
reported as absent, rather than invented from the current account.

Successful discovery proves only that the selected endpoints responded to this
account. It does not prove complete Personal/Shared/Hidden media coverage,
resource identity, checksum equivalence, stable snapshots, physical backups or
safe removal. Those statuses remain `NOT VERIFIED`.

Matching metadata passes do not prove an atomic snapshot or a complete resource
inventory. The metadata scopes read no media bytes. The originals scope records
byte associations for the two selected resources and checks local copies; broader
resource/content mapping and independent physical backup verification remain open.
Removal tests require their own explicit authorization. Do not use private archives
as development fixtures.

macOS success establishes behavior on macOS. The same pinned package and scripts
can subsequently be exercised on Windows; terminal handling, authentication,
paths and filesystem protections still require Windows runtime verification.

## Offline checks

These tests use fake responses and forbid socket creation. They require no cloud
package installation or account access:

```sh
python3 -m unittest discover -s tools/cloud-probe -v
```

They also run through the repository's existing Python unittest check.
