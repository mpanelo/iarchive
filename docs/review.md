# Browser review

Start with the [full workflow](workflows.md). Use its executable, state and cycle.

**Windows:**

```powershell
& $exe --state-dir $state review --cycle $cycle
```

**macOS:**

```sh
"$exe" --state-dir "$state" review --cycle "$cycle"
```

Open the full printed URL, including everything after `#`. It works only for
this session. The gallery runs on `127.0.0.1`; keep its URL private.

## Save a note

Filter by exact duplicates, similar images, dark candidates, failures or reviewed
groups. Compare previews, paths and measurements.

| Button | What it saves |
| --- | --- |
| Keep all | Keep this group. |
| Flag selected for later | A note for the checked files. |
| Decide later | A deferred decision. |
| Clear decision | Clear the current note; keep its history. |

Notes save immediately to SQLite. Files stay unchanged. Reopening the same audit
keeps its notes. A new index or audit leaves earlier notes as history.

## Stop or refresh

Press **Ctrl+C** in the terminal before running another command against this
state. Review holds its writer lock.

Missing preview? Stop review and rerun audit. Exact-only audits do not generate
previews. Decode failures stay visible as findings.

After review, run `verify --deep` to put the notes in fresh backup bundles.
Old bundles are not updated automatically.

The gallery serves cached JPEG previews, never originals. It does not check
current file integrity or approve removal. See [safety](safety.md).
