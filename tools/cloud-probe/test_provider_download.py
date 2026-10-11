"""Exercise the pinned provider's download request with synthetic responses."""

from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from originals import stream_original
from test_originals import Response, URLS


class ProviderDownloadTests(unittest.TestCase):
    def setUp(self):
        try:
            from pyicloud_ipd.services.photos import download_asset
            from requests import Request
        except ImportError:
            self.skipTest("Optional provider integration: run in tools/cloud-probe environment")
        self.download_asset, self.Request = download_asset, Request
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_actual_provider_requests_full_stream_and_closes_response(self):
        calls, response = [], Response()
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return response
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            stream_original(SimpleNamespace(get=get), URLS[0], Path(directory) / "original.bin", 7)
        self.assertEqual(calls, [(URLS[0], {"headers": {"Range": "bytes=0-"}, "stream": True})])
        self.assertTrue(response.closed)

    def test_requests_preparation_normalizes_signed_url_without_network(self):
        value = "https://cvws.icloud-content.com/synthetic%2fpath?token=fixture%7evalue"
        prepared = self.Request("GET", value).prepare()
        self.assertEqual(prepared.method, "GET")
        self.assertIsNone(prepared.body)
        self.assertEqual(prepared.url, "https://cvws.icloud-content.com/synthetic%2Fpath?token=fixture~value")


if __name__ == "__main__":
    unittest.main()
