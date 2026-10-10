"""Bounded JSONL protocol; callers provide explicit archive and cache roots."""

from pathlib import Path
import json
import stat

from . import PROTOCOL_VERSION

MAX_LINE_BYTES = 16_384
MAX_BATCH = 64
ALGORITHMS = {"phash", "dark", "sharpness", "thumbnail"}


def safe_error(error: Exception) -> str:
    reasons = {
        "UnidentifiedImageError": "unsupported or corrupt image",
        "DecompressionBombError": "image exceeds decompression safety limit",
        "DecompressionBombWarning": "image exceeds decompression safety limit",
        "PermissionError": "input/cache permission denied",
        "FileNotFoundError": "input or required path unavailable",
        "MemoryError": "image exceeded available memory",
        "ModuleNotFoundError": "worker dependency unavailable",
    }
    allowed = {
        "unsupported protocol version", "invalid request ID", "invalid file ID",
        "unsupported algorithms; v1 requires all four", "preview dimension must be between 32 and 1024",
        "paths must be absolute and traversal-free", "symlink/reparse point refused",
        "input is outside archive root or not a file", "cache and archive must not overlap",
        "incomplete input", "cache root is not a directory", "request exceeds protocol line limit",
        "request root differs from configured worker root", "image exceeds safe decode pixel limit",
        "multi-frame image is not fully visually audited", "source changed during analysis",
    }
    name = type(error).__name__
    reason = str(error) if str(error) in allowed else reasons.get(name, "image/request could not be processed")
    return f"{name}: {reason}"


def is_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def reject_links(path: Path) -> None:
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            if is_link(component):
                raise ValueError("symlink/reparse point refused")


def authorize_request(request: dict, archive_root: Path, cache_root: Path) -> None:
    """Root authority comes from process arguments, never from an untrusted JSON line."""
    for key, authorized in (("archive_root", archive_root), ("cache_root", cache_root)):
        supplied = request.get(key)
        if not isinstance(supplied, str) or Path(supplied) != authorized:
            raise ValueError("request root differs from configured worker root")


def validate_request(request: dict) -> tuple[Path, Path, int]:
    if not isinstance(request, dict) or request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if not isinstance(request.get("request_id"), str) or len(request["request_id"]) > 128:
        raise ValueError("invalid request ID")
    file_id = request.get("file_id")
    if type(file_id) is not int or file_id < 1:
        raise ValueError("invalid file ID")
    algorithms = request.get("algorithms")
    if not isinstance(algorithms, list) or set(algorithms) != ALGORITHMS:
        raise ValueError("unsupported algorithms; v1 requires all four")
    preview = request.get("max_preview_pixels", 512)
    if type(preview) is not int or not 32 <= preview <= 1024:
        raise ValueError("preview dimension must be between 32 and 1024")
    paths = []
    for key in ("archive_root", "input_path", "cache_root"):
        value = request.get(key)
        if not isinstance(value, str):
            raise ValueError(f"missing {key}")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("paths must be absolute and traversal-free")
        reject_links(path)
        paths.append(path.resolve(strict=key != "cache_root"))
    root, source, cache = paths
    if not root.is_dir() or not source.is_file() or not source.is_relative_to(root):
        raise ValueError("input is outside archive root or not a file")
    if cache.is_relative_to(root) or root.is_relative_to(cache):
        raise ValueError("cache and archive must not overlap")
    if source.suffix.lower() in {".partial", ".tmp", ".part"}:
        raise ValueError("incomplete input")
    if cache.exists() and not cache.is_dir():
        raise ValueError("cache root is not a directory")
    # A caller cannot replace a source through a thumbnail path or supply arbitrary output filenames.
    cache.mkdir(parents=True, exist_ok=True)
    reject_links(cache)
    return source, cache, preview


def serve(reader, writer, handler) -> int:
    """A malformed or failed file has no metrics and does not poison the next request."""
    for _ in range(MAX_BATCH):
        line = reader.readline(MAX_LINE_BYTES + 1)
        if not line:
            return 0
        request_id = None
        try:
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("request exceeds protocol line limit")
            request = json.loads(line)
            if isinstance(request, dict):
                request_id = request.get("request_id")
            result = handler(request)
            response = {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "status": "ok", "features": result}
        except Exception as error:
            # Paths/metadata and stack traces are intentionally absent from protocol errors.
            response = {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "status": "error", "error": safe_error(error), "features": None}
        writer.write(json.dumps(response, allow_nan=False) + "\n")
        writer.flush()
        if len(line) > MAX_LINE_BYTES:
            # Do not parse the rest of an oversized line as a second request.
            return 2
    return 0 if not reader.read(1) else 2
