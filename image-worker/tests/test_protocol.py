"""These protocol tests use only the standard library, even without media dependencies."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from iarchive_worker.protocol import serve, validate_request, authorize_request


class ProtocolTests(unittest.TestCase):
    def test_json_cannot_change_worker_authorized_roots(self):
        with self.assertRaises(ValueError):
            authorize_request({"archive_root": "/unrelated", "cache_root": "/cache"}, Path("/photos"), Path("/cache"))
    def test_error_has_no_dark_metrics_and_next_request_survives(self):
        def handler(request):
            if request["request_id"] == "bad":
                raise ValueError("private path or token must not leak")
            return {"success": True}
        output = io.StringIO()
        serve(io.BytesIO(b'{"request_id":"bad"}\n{"request_id":"good"}\n'), output, handler)
        bad, good = map(json.loads, output.getvalue().splitlines())
        self.assertEqual(bad["status"], "error")
        self.assertIsNone(bad["features"])
        self.assertNotIn("private", output.getvalue())
        self.assertEqual(good["status"], "ok")

    def test_traversal_outside_input_and_cache_overlap_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "source"
            root.mkdir()
            source = root / "photo.png"
            source.write_bytes(b"synthetic")
            request = {"protocol_version": 1, "request_id": "1", "file_id": 1, "archive_root": str(root), "input_path": str(source), "cache_root": str(base / "cache"), "algorithms": ["phash", "dark", "sharpness", "thumbnail"], "max_preview_pixels": 512}
            self.assertEqual(validate_request(request)[0], source)
            for key, value in [("input_path", str(base / "elsewhere")), ("cache_root", str(root / "cache")), ("input_path", str(root / ".." / "source" / "photo.png")), ("max_preview_pixels", 1), ("protocol_version", 2), ("file_id", "../x")]:
                with self.assertRaises((ValueError, FileNotFoundError)):
                    validate_request({**request, key: value})

    def test_oversized_line_is_not_split_into_requests(self):
        output = io.StringIO()
        code = serve(io.BytesIO(b"x" * 17000 + b"\n{}\n"), output, lambda _: {})
        self.assertEqual(code, 2)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
