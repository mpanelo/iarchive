"""Exercise the pinned provider's download request with synthetic responses."""

import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from originals import run_downloads, select_originals
from probe import request_guard, save_new
from test_originals import inventory


class ProviderDownloadTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_prepared_download_matches_allowlist(self):
        from requests import Response, Session
        from requests.adapters import HTTPAdapter

        private, urls = inventory()
        urls[("CPLMaster", "full-master-1")] = (
            "https://cvws.icloud-content.com/synthetic%2fpath?token=fixture%7evalue")
        plans = select_originals(private, private, urls, "synthetic-account")
        calls, responses = [], []

        def send(adapter, request, **kwargs):
            calls.append(request.url)
            self.assertEqual(request.method, "GET")
            self.assertIsNone(request.body)
            self.assertEqual(request.headers["Range"], "bytes=0-")
            self.assertTrue(kwargs["stream"])
            response = Response()
            response.status_code = 200
            response.headers["Content-Type"] = "application/octet-stream"
            response.raw = io.BytesIO(b"fixture")
            response.request, response.url = request, request.url
            response.close = Mock(wraps=response.close)
            responses.append(response)
            return response

        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory, \
                Session() as session, patch.object(HTTPAdapter, "send", send), \
                request_guard(True, True, True) as transport:
            run_downloads(session, plans, Path(directory), transport, save_new)
            self.assertEqual(transport["original_download_requests"], 2)
            self.assertEqual(transport["approved_download_urls"], set())
        self.assertEqual(calls, [
            "https://cvws.icloud-content.com/synthetic%2Fpath?token=fixture~value",
            urls[("CPLMaster", "full-master-2")],
        ])
        for response in responses:
            response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
