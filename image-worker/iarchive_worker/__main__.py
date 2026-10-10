import hashlib
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from . import ALGORITHM_VERSION, PROTOCOL_VERSION
from .protocol import serve, validate_request, reject_links, authorize_request


def handle(request):
    from .decoder import decode
    from .features import measure
    from PIL import Image

    source, cache, preview = validate_request(request)
    before = source.stat()
    image = decode(source)
    features = measure(image)
    image.thumbnail((preview, preview), Image.Resampling.LANCZOS)
    # Names depend on the original bytes and algorithm, never on input-supplied paths.
    digest = hashlib.sha256()
    with source.open("rb") as media:
        for block in iter(lambda: media.read(1024 * 1024), b""):
            digest.update(block)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("source changed during analysis")
    thumbnail = cache / f"{digest.hexdigest()}-{ALGORITHM_VERSION}-{preview}.jpg"
    reject_links(thumbnail)
    descriptor, temporary = tempfile.mkstemp(prefix="preview-", suffix=".partial", dir=cache)
    try:
        with os.fdopen(descriptor, "wb") as output:
            image.save(output, format="JPEG", quality=85)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, thumbnail)
    finally:
        Path(temporary).unlink(missing_ok=True)
        image.close()
    features["algorithm_version"] = ALGORITHM_VERSION
    features["thumbnail"] = str(thumbnail)
    return features


def main():
    if sys.argv[1:] == ["--probe"]:
        from .decoder import register_heif_opener  # noqa: F401
        from .features import measure  # noqa: F401
        print(json.dumps({"protocol_version": PROTOCOL_VERSION, "algorithm_version": ALGORITHM_VERSION, "heic": True}))
        return 0
    parser = argparse.ArgumentParser(description="Local read-only image analysis; JSONL protocol v1")
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    args = parser.parse_args()
    def authorized_handle(request):
        authorize_request(request, args.archive_root, args.cache_root)
        return handle(request)
    return serve(sys.stdin.buffer, sys.stdout, authorized_handle)


if __name__ == "__main__":
    raise SystemExit(main())
