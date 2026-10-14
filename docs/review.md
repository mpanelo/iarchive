# Browser review

The [guided demo](workflows.md) starts review for you. For an existing archive,
use its cycle ID and state directory:

```powershell
.\target\release\iarchive.exe review --cycle archive-2026-10-13
```

On macOS, use `./target/release/iarchive`. Add `--state-dir PATH` if your archive
uses a custom state directory.

Open the full printed URL, including everything after `#`. Keep the terminal
running. The URL works only for this session.

## Review a group

1. Compare the images in one group.
2. To flag images, check **Flag this image for later**, then click
   **Save selected flags** beneath that group.
3. Look for the saved confirmation beside the group. Checking a box alone
   does not save anything.

| Button | Applies to this group only |
| --- | --- |
| Keep this group | Mark every image in the group to keep. |
| Save selected flags | Save flags for its checked images. |
| Decide on this group later | Defer this group's decision. |
| Clear this group's decision | Clear its decision and selections; retain history. |

Selections in other groups stay intact when you save or clear a group. Filters
and pages also preserve unsaved selections. Save before closing or refreshing
the tab. The same image can appear in several groups; decisions are independent.

Flags are saved review decisions. They do not move or delete photos.
Reopening the same audit keeps saved decisions. A new index or audit leaves
previous decisions as history.

## Finish

Press **Ctrl+C** in the terminal to stop review. The guided demo then verifies
both copies automatically. For an existing archive, run `verify --deep` as shown
in the [walkthrough](workflows.md) to include your decisions in fresh bundles.

Missing preview? Stop review and rerun audit. Exact-only audits do not generate
previews. Decode failures stay visible as findings.

The gallery serves cached previews, never originals. Review does not verify
current file integrity or complete cloud coverage. See [safety](safety.md).
