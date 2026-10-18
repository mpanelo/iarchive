"""Create one disposable JPEG with an old capture date, without reading media."""

import argparse
import os
from pathlib import Path
import stat
import uuid

from PIL import Image, ImageDraw


CAPTURE_DATE = "2001:01:02 03:04:05"


def generate(output):
    """Use a fresh directory beneath existing, unlinked parents."""
    output = Path(os.path.abspath(output))
    for parent in (*reversed(output.parents), output):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError("Linked or non-directory fixture destination")
    output.mkdir(mode=0o700, exist_ok=False)
    identity = uuid.uuid4().hex
    destination = output / f"iarchive-test-{identity}.jpg"
    with Image.new("RGB", (640, 360), (25, 50, 90)) as image:
        draw = ImageDraw.Draw(image)
        draw.text((35, 40), "iarchive disposable upload test", fill="white", font_size=28)
        draw.text((35, 100), "Capture date: 2001-01-02 UTC", fill="white", font_size=24)
        draw.text((35, 155), identity, fill="white", font_size=20)
        # Unique pixels as well as metadata prevent byte-identical fixtures.
        for index, value in enumerate(bytes.fromhex(identity)):
            x = 32 + index * 36
            draw.rectangle((x, 240, x + 28, 315), fill=(value, 255 - value, 150))
        exif = Image.Exif()
        exif[306] = CAPTURE_DATE
        exif[34665] = {36867: CAPTURE_DATE, 36868: CAPTURE_DATE,
                       36880: "+00:00", 36881: "+00:00", 36882: "+00:00"}
        with destination.open("xb") as stream:
            image.save(stream, format="JPEG", quality=92, exif=exif)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory beneath an existing parent")
    args = parser.parse_args()
    print(generate(args.output))
