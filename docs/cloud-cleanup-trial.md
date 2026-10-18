# One disposable photo trial

This research helper is separate from the archive CLI and the cleanup demo.
It accepts only the synthetic JPEG bound to the saved upload experiment.
**The live trial has not been run.** Preparing it does not authorize a live run.

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
your reported UI confirmation. A missing flag stays unknown. Restored membership
and resource metadata do not prove restored remote bytes; no second download is
made. Relation ownership and complete resource coverage remain unverified.

Observe iCloud storage usage manually before and after; the helper does not query
quota or claim reclaimed space. Never empty Recently Deleted for this trial.
Both local files are on one filesystem, so this does not establish independent
backups or authorize cleanup of real archives.
