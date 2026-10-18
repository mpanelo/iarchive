"""Block the pinned provider's removal helper using synthetic records only."""

import io
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from icloudpd.base import delete_photo
from pyicloud_ipd.session import PyiCloudSession
from pyicloud_ipd.services.photos import PhotoLibrary, PhotosService
from requests import Response
from requests.adapters import HTTPAdapter

from changes import canonical, request_body
from conflict import Exchange, Pacer
from probe import ProbeBlocked, request_guard


ROOT = "https://p123-ckdatabasews.icloud.com"
PRIVATE = ROOT + "/database/1/com.apple.photos.cloud/production/private"
ZONE = {"zoneName": "synthetic-zone", "ownerRecordName": "synthetic-owner",
        "zoneType": "REGULAR_CUSTOM_ZONE"}


class ProviderMutationTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        dns = patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))
        dns.start()
        self.addCleanup(dns.stop)

    def test_removal_is_blocked_before_transport(self):
        scopes = {"auth": {"discover": False}, "libraries": {"discover": True},
                  "album": {"discover": True, "album": True},
                  "downloads": {"discover": True, "album": True, "downloads": True},
                  "changes": {"discover": True, "changes": True}}
        for scope, options in scopes.items():
            with self.subTest(scope=scope), \
                    PyiCloudSession(SimpleNamespace(password_filter=None, http_timeout=30)) as session, \
                    patch.object(session, "send", wraps=session.send) as prepared_send, \
                    patch.object(HTTPAdapter, "send",
                                 side_effect=AssertionError("Request reached transport")) as wire, \
                    request_guard(**options) as state:
                session.trust_env = False
                state.update(photo_root=ROOT, approved_zone=ZONE, approved_album="synthetic-album")
                state["change_request"] = canonical(request_body(ZONE, "synthetic-token"))
                library = SimpleNamespace(service_endpoint=PRIVATE, zone_id=ZONE,
                                          params={"dsid": "synthetic-account"}, session=session)
                photo = SimpleNamespace(_asset_record={"recordName": "synthetic-asset",
                                                        "recordChangeTag": "asset-v1"})
                logger = Mock()
                with self.assertRaisesRegex(ProbeBlocked, "outside the approved probe scope"):
                    delete_photo(logger, library, photo, lambda _: "synthetic.jpg")

                # Exercise the real helper, session and request preparation. The
                # guard must reject the modification before its underlying send.
                prepared_send.assert_called_once()
                request = prepared_send.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(urlsplit(request.url)._replace(query="").geturl(),
                                 PRIVATE + "/records/modify")
                self.assertIs(prepared_send.call_args.kwargs["verify"], True)
                self.assertEqual(json.loads(request.body), {
                    "atomic": True, "desiredKeys": ["isDeleted"], "zoneID": ZONE,
                    "operations": [{"operationType": "update", "record": {
                        "recordName": "synthetic-asset", "recordChangeTag": "asset-v1",
                        "recordType": "CPLAsset", "fields": {"isDeleted": {"value": 1}}}}],
                })
                wire.assert_not_called()
                logger.info.assert_not_called()
                self.assertTrue(all(value == 0 for key, value in state.items()
                                    if key.endswith("_requests")))

    def test_favorite_matches_provider_request(self):
        # Conflicting input flags exercise the real wrapper's override behavior.
        params = {"dsid": "synthetic-account", "clientId": "synthetic-client",
                  "remapEnums": False, "getCurrentSyncToken": False}
        service = SimpleNamespace(params=dict(params), data={"dsInfo": {"dsid": params["dsid"]}},
                                  password_filter=None, http_timeout=30)
        with PyiCloudSession(service) as session:
            session.trust_env = False
            service.session = session
            finished = SimpleNamespace(json=lambda: {"records": [{"fields": {"state": {"value": "FINISHED"}}}]})
            # Both constructors check indexing. Stub only those initial reads.
            with patch.object(session, "post", return_value=finished):
                photos = PhotosService(ROOT, session, params)
                library = PhotoLibrary(photos.service_endpoint, photos.params, session, ZONE, "private")
            photo = SimpleNamespace(_asset_record={"recordName": "synthetic-asset", "recordChangeTag": "old-tag"})
            with patch.object(session, "send", wraps=session.send) as prepared_send, \
                    patch.object(HTTPAdapter, "send") as blocked_wire, request_guard(True) as state:
                state["photo_root"] = ROOT
                with self.assertRaisesRegex(ProbeBlocked, "outside the approved probe scope"):
                    delete_photo(Mock(), library, photo, lambda _: "synthetic.jpg")
                upstream = prepared_send.call_args.args[0]
                blocked_wire.assert_not_called()

            binding = {"account_id": params["dsid"], "zone_id": ZONE,
                       "asset_id": "synthetic-asset", "master_id": "synthetic-master"}
            wire = Exchange(service, ROOT, binding, Pacer())
            before = {"record": photo._asset_record, "favorite": 1}
            cycled = {"record": {**photo._asset_record, "recordChangeTag": "new-tag"}, "favorite": 1}
            body = wire.freeze_update(before, cycled)

            def send(adapter, request, **kwargs):
                self.assertEqual(request.method, upstream.method)
                self.assertEqual(urlsplit(request.url)._replace(query=""), urlsplit(upstream.url)._replace(query=""))
                self.assertEqual(parse_qs(urlsplit(request.url).query), parse_qs(urlsplit(upstream.url).query))
                # Only the purpose-specific field and return selection differ.
                expected_body = json.loads(upstream.body)
                expected_body["desiredKeys"] = ["isFavorite"]
                expected_body["operations"][0]["record"]["fields"] = {"isFavorite": {"value": 0}}
                self.assertEqual(json.loads(request.body), expected_body)
                self.assertEqual(body, expected_body)
                response = Response()
                response.status_code = 200
                response.raw = io.BytesIO(b'{"records":[]}')
                return response

            with patch.object(HTTPAdapter, "send", autospec=True, side_effect=send) as sent:
                wire.send("modify")
                sent.assert_called_once()
            self.assertEqual(service.params, params)


if __name__ == "__main__":
    unittest.main()
