# Cloud cleanup preview

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

## Limits

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
