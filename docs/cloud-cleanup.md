# Cloud cleanup review

These helpers save local review notes. They never remove items from iCloud.

## Review matched archive items

Run [fresh reconciliation](../tools/cloud-prototype/README.md#verify-current-copies)
and save its JSON output as `reconciliation-private.json`. Use the same resource
report and download manifest:

```sh
python3 tools/cloud-cleanup/prepare_review.py --reconciliation reconciliation-private.json --resource-report resource-report-private.json --download-manifest download-manifest-private.json
```

On Windows, use `python` instead of `python3`.

1. Keep Terminal open and open its printed review link.
2. Check items to include in a draft. Everything starts kept; variants stay together.
3. Click **Save draft**. You can also save a draft that keeps everything.
4. Adjust the selection and save again, or press Enter in Terminal to close.

The page shows resource paths, byte counts and verification status. It does not
generate media previews. Saving rehashes both copies and writes identical drafts
under `reviews` in all three verification bundles, outside media. Each save has
a SHA-256 digest; it is not a removal confirmation or an approval signature.
Edits retire the previous draft. Changed evidence, missing or damaged files,
or an incomplete save stop the review and require fresh preparation.

The recorded OS drive check is shown separately. This helper does not refresh
physical-drive topology or cloud metadata. Complete resource relationships,
cloud coverage and removal approval remain unverified. Files may change after
a check. Keep reports and drafts private, and run one writer at a time.

### Reopen a saved draft

After closing a saved review, Terminal prints a **Reopen with** command. Run it
to restore your choices. Any of the three published `draft.json` copies can be
used with `--resume`:

```sh
python3 tools/cloud-cleanup/prepare_review.py --resume /path/to/draft.json
```

Reopening checks all three copies, the original evidence and both media roots.
Edits create a new draft; existing drafts are never overwritten. Superseded,
damaged or incomplete drafts are refused. Older drafts without reopen metadata
need a new review. Reopening never approves cloud removal.

### Rehearse preflight confirmation

For a saved draft containing selected items:

```sh
python3 tools/cloud-cleanup/preflight.py --simulate --draft /path/to/draft.json
```

The helper rehashes both copies and compares an in-memory metadata model before
and after your confirmation. Type the printed phrase with the exact count and
full plan digest within two minutes. A blank answer cancels.

The model starts from the draft's recorded metadata. It does not read iCloud or
prove current cloud coverage, drive independence or server-side race protection.
This rehearses the checks and confirmation; it never approves removal.

Matching plans, confirmation intents and receipts are saved under `preflight`
beside all three draft copies. A receipt requires matching `receipt.json` and
`complete.json` files in every location. Partial writes, changed evidence,
expired confirmations or failed checks stop the rehearsal. Start a new rehearsal
after a failure; previous confirmations cannot be reused.

### Check current iCloud metadata

This optional read-only check requires separate account-access approval. From
the checkout, use the pinned cloud environment and your exact album name:

```sh
uv run --locked --project tools/cloud-probe --python 3.12 python tools/cloud-cleanup/preflight.py --read-only-cloud --draft /path/to/draft.json --album Legoland
```

Confirm the read-only scope in Terminal, then enter credentials there. The helper
checks the draft's account, private zone and album identity before reading photo
metadata. It compares two matching album passes with every saved item, including
kept items, and rehashes both local copies. Confirm the printed count and digest
within two minutes to repeat these checks and save read-only receipts.

The album is limited to 1,000 items and 50 metadata requests per check. Requests
start at least one second apart. Throttling, incomplete results or changed
identities, tags or resources stop the run without retrying. To reuse a previously
authenticated session, add `--reuse-session cloud-probe-RUN_NAME`.

Private metadata and session files stay under `.smoke`; receipts stay beside
the drafts. No media is downloaded or removed. Matching metadata does not prove
complete cloud coverage, independent drives or protection against later edits.
The archive CLI still cannot authenticate or remove cloud items.

## Synthetic demo

This is an **offline demo**, not a cloud deletion command. It creates synthetic
files and two local copies in a fresh temporary directory. It never signs in,
reads your archives or changes iCloud. The archive CLI still has no cloud deletion.

From the checkout, with Python 3.12 or newer:

```sh
python3 tools/cloud-cleanup/cleanup.py --simulate
```

On Windows, use `python` instead of `python3`.

1. Keep Terminal open and open its printed review link.
2. Uncheck items you want to keep in iCloud. Each checkbox includes all variants.
3. Click **Save selection** after making changes, or **Continue to confirmation**.
4. Click **Copy confirmation**, then paste into Terminal. A blank answer cancels.

The page shows the selected and kept counts. **Keep all** clears the selection;
cancel in Terminal to finish without simulating removal. Local copies are always
kept. Previews are synthetic illustrations.

Changing the selection invalidates its previous confirmation immediately when
the helper receives the edit. Saving freshly checks the backups and creates a
new immutable plan. Confirmations from earlier plans are refused; the review
stays open so you can copy the current one.

The helper freshly hashes both copies, rechecks the mock album, and changes only
mock memory. It saves the sealed plan, journal and successful result in
`state`, `replica-a/cleanup` and `replica-b/cleanup`, outside the media folders.
The printed directory is retained for inspection. Saved selections are stored
under `revisions` in those folders. Earlier plans remain for reference.
Omit `--simulate` for a styled, read-only saved preview.

The editor runs only on this computer, uses no external services, and stops when
the Terminal command finishes. It cannot start execution from the page.

Changed or incomplete evidence blocks execution. Lost replies, unexpected
results and failures stop later targets. Started plans cannot be rerun; inspect
the journal instead. Journal files are numbered, identical events at all three
locations. An interrupted write can leave incomplete copies; that is a failure,
not permission to continue.

### Demo limits

The two folders are on one filesystem. This demo does not prove independent
drives, real cloud-to-file relationships, full cloud coverage or recovery.
Its resource identities and media bytes are invented test evidence.

A preflight check cannot prevent an edit between the check and removal. The
mock deliberately accepts that race; a failed post-check stops subsequent
targets but cannot undo an accepted request.

Archive cleanup still needs separate implementation and approval, verified
resource bindings on both drives, and explicit acceptance of this race. The
[one-photo trial](cloud-cleanup-trial.md#recorded-result) records the bounded
soft-delete/recovery evidence and remaining gaps. It does not enable archive
cleanup. No automatic emptying of Recently Deleted is planned.

[Research findings](../tools/cloud-probe/README.md#concurrency-decision)
