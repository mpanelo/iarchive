# Disposable media recovery trials

These research helpers are separate from the archive CLI and cleanup demo.
The JPEG helper accepts only the synthetic photo bound to the upload experiment.
Its macOS trial completed; see the [recorded result](#recorded-result).
Each live run requires separate approval.

## Prepare offline

Use the evidence path and five-item baseline supplied for the approved test.
For example, from the checkout:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-cleanup/trial.py --evidence .smoke/dated-upload/evidence-private.json --baseline cloud-probe-example --album 'Synthetic test album'
```

Default behavior is offline. The helper verifies the original's SHA-256 against
the upload evidence and saves a recovery copy. It prints a fresh private output
directory under `.smoke`. No authentication or cloud requests occur.

## After separate live approval

Only after agreeing on the main account, album and exact disposable item, add
`--live` to that same command. The trial:

1. Asks for `ONE DISPOSABLE PHOTO TRIAL` before local authentication.
2. Checks the five-item album and exact account, zone, asset and master IDs.
3. Downloads that original once and requires its SHA-256 to match the saved JPEG.
4. Rechecks metadata and local originals, then prints a one-item confirmation
   containing the full plan digest. Confirming permits one removal request.
5. Checks the soft-delete flag and four remaining album items.
6. Asks you to find the blue image in Recently Deleted and type
   `SEEN IN RECENTLY DELETED`.
7. Asks you to restore it manually and type `RESTORED ONE PHOTO`, then checks
   its identity, album membership and resource metadata again.

Keep the selected image unchanged during the trial. Removal affects synced
devices. A preflight check cannot prevent a change immediately before the write.
The helper never purges, restores automatically or retries a write. On an
uncertain result, inspect Recently Deleted and restore the blue image if present.

Request starts are at least one second apart. There are at most three paired
asset/master lookups, one original GET capped at 1 MiB, and one soft-delete
update. Media and targeted metadata reads have 45-second stream budgets and
10/30-second connection/read timeouts. Server-requested pauses stop the trial,
including post-checks. There are no redirects or automatic write retries.

## Evidence and limits

Run only one probe at a time. The private run retains recovery/downloaded
originals, sanitized metadata, the removal plan and digest, flushed intent and
outcome, and `summary.json`.
Keep everything under `.smoke` private. Interrupted attempts cannot be resumed.
Digests detect edits; they are not approval signatures.

The helper separates the per-record reply from observed removal metadata and
your reported UI confirmation. A missing flag stays unknown. The original trial
checks restored metadata without downloading restored bytes. Relation ownership
and complete resource coverage remain unverified.

Observe iCloud storage usage manually before and after; the helper does not query
quota or claim reclaimed space. Never empty Recently Deleted for this trial.
Both local files are on one filesystem, so this does not establish independent
backups or authorize cleanup of real archives.

## Check restored JPEG bytes

Prepare a read-only follow-up from the completed trial run:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-cleanup/verify_restored.py --trial cloud-probe-example --album 'Synthetic test album'
```

This checks saved evidence and preserves another local copy without signing in.
After separate live approval, add `--live` and type `READ ONE RESTORED JPEG`.
The helper requires the same account and unchanged five-item album. It allows
one asset/master lookup and one original GET, capped at 1 MiB, then rechecks album
metadata. Requests start at least one second apart; throttling, redirects or
changed evidence stop the check without retries. No cloud modifications are allowed.

Require `restored_original_sha256_verified: true` in the new summary. That result
covers this JPEG only; the original trial and other verification limits stay unchanged.

## Live Photo and video trials

Prepare from a completed five-item metadata baseline and the earlier resource
download run. Both saved original copies must still match their hashes:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-cleanup/live_photo_trial.py --baseline cloud-probe-baseline --downloads cloud-probe-downloads --album 'Synthetic test album'
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-cleanup/video_trial.py --baseline cloud-probe-baseline --downloads cloud-probe-downloads --album 'Synthetic test album'
```

Choose one command. Preparation stays offline and preserves the selected originals.
Missing, ambiguous, edited or additional original resources block it.

After separate live approval, add `--live` and follow the Terminal prompts.
The helper downloads the originals and checks their saved hashes before offering
one exact removal confirmation. Removal confirmation expires after two minutes.
After removal, confirm Recently Deleted, restore that item manually, then check
playback (and video audio, if present). Restored originals are downloaded and
compared separately; playback is recorded as **USER REPORTED**.

| Trial | Originals | Media GETs total | Limit per GET |
| --- | --- | --- | --- |
| Live Photo | HEIC still and MOV motion | 4 | 4 MiB |
| Video | MOV original | 2 | 8 MiB |

Each trial allows three asset/master lookups and one soft-delete request.
Requests start at least one second apart; media streams
have 45-second budgets and 10/30-second connection/read timeouts. Throttling,
changed evidence or uncertain outcomes stop the trial. Writes are never retried;
lost replies require exact metadata reconciliation before continuing.
No purge or automatic restore is available. Both local copies remain on one
filesystem; archive cleanup and physical backup independence stay unverified.

## Recorded result

### JPEG

One approved macOS trial completed on October 18, 2026 (UTC):

- One original download, three targeted asset/master lookups and one accepted
  soft-delete update. The saved plan and durable intent matched that one item.
- Album count changed **5 → 4 → 5** after manual restoration. The other four items
  stayed unchanged; the restored target's identity and resource metadata matched.
- The source, recovery copy and downloaded original had matching SHA-256 hashes
  before removal and after restoration.

Recently Deleted visibility and manual restoration were user-reported. The
helper verified the soft-delete flag, then restored membership and metadata.
An offline evidence check confirmed the saved plan, observations and local hashes
without making further cloud requests.

A read-only follow-up on October 23, 2026 (UTC) downloaded the restored 29,928-byte
original once. Its SHA-256 matched the synthetic source and preserved pre-removal
copies. One asset/master lookup and before/after album captures matched the saved
restoration metadata; zero cloud modifications occurred. An independent offline
check confirmed the saved bindings, report digests and local hashes.

### Live Photo

One approved macOS trial completed on October 23, 2026 (UTC): one accepted
soft-delete, three asset/master lookups and four media GETs. The album changed
**5 → 4 → 5** after manual restoration; the other four items stayed unchanged.
The restored 428,844-byte HEIC still and 3,752,093-byte MOV motion file each
matched their preserved pre-removal SHA-256 hashes. Recently Deleted visibility
and restored Live Photo playback were user-reported. An independent offline
check verified the plan/intent, input digests, album records and both file hashes.

An earlier attempt expired at confirmation after two media GETs and zero cloud
modifications. It was not retried automatically.

### Video

One approved macOS trial completed on October 28, 2026 (UTC): one accepted
soft-delete, three asset/master lookups and two media GETs. The album changed
**5 → 4 → 5** after manual restoration; the other four items stayed unchanged.
The restored 5,423,087-byte MOV matched its preserved pre-removal SHA-256.
Recently Deleted visibility and restored playback were user-reported. An
independent offline check verified the plan/intent, input digests, album records
and original hashes.

Restored bytes are verified for the tested JPEG, Live Photo and video only. Concurrency
protection, complete cloud/resource coverage, relationship ownership, independent
physical backups and reclaimed space remain **NOT VERIFIED**. These results do
not enable archive cleanup.
