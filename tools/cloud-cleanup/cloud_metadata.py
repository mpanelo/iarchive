"""Bounded, read-only Photos metadata for review preflight. No media or writes."""

from contextlib import contextmanager
import getpass
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-probe'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-prototype'))
from album import MAX_ASSETS, collect_pass, find_album, query_kind
from conflict import Pacer, read_guard
from probe import (PIN, authenticated_account, discover_zones, new_run, reuse_cookies,
                   save_new, verify_second_factor)
from resources import build_resource_report
from prototype import Blocked, canonical
from prompts import confirm


SOURCE = 'READ_ONLY_ICLOUD'
MAX_REQUESTS = 50


def require_provider():
    try:
        if version('icloudpd') == PIN:
            return
    except PackageNotFoundError:
        pass
    raise Blocked('Pinned cloud provider is required; use the cloud-probe environment')


class CloudMetadata:
    """Two single-use snapshots of one frozen album, with no upstream retries."""

    def __init__(self, service, root, zones, album, draft, pacer, output=None):
        require_provider()
        self.scope = json.loads(canonical(draft['scope']))
        primary = [item['zone_id'] for item in zones.get('private', [])
                   if not item.get('deleted') and item['zone_id'].get('zoneName') == 'PrimarySync']
        if (self.scope['database'] != 'private' or primary != [self.scope['zone_id']]
                or authenticated_account(service) != self.scope['account_id']):
            raise Blocked('Authenticated account or private zone differs from the draft')
        self.count = len(draft['assets'])
        if not 1 <= self.count <= MAX_ASSETS or not album:
            raise Blocked('A bounded named album is required')
        self.service, self.root, self.album, self.pacer = service, root.rstrip('/'), album, pacer
        self.query = parse_qs(urlencode({**service.params, 'remapEnums': True, 'getCurrentSyncToken': True}, doseq=True))
        self.output, self.reads, self.requests, self.stopped = output, 0, 0, False

    @contextmanager
    def transport(self):
        from requests import Session
        from requests.adapters import HTTPAdapter
        send = HTTPAdapter.send
        prefix = self.root + '/database/1/com.apple.photos.cloud/production/private'
        routes = {prefix + '/records/query': False, prefix + '/internal/records/query/batch': True}
        starts = self.requests

        def guarded(adapter, request, **kwargs):
            url = urlsplit(request.url)
            route = url._replace(query='').geturl()
            try:
                kind = query_kind(json.loads(request.body), self.scope['zone_id'],
                                  self.scope['album_id'], routes[route])
            except (ValueError, TypeError, KeyError):
                kind = None
            if (request.method != 'POST' or not kind or parse_qs(url.query) != self.query
                    or kwargs.get('verify', True) is not True
                    or adapter.max_retries.total != 0 or self.requests - starts >= MAX_REQUESTS
                    or authenticated_account(self.service) != self.scope['account_id']):
                raise Blocked('Read-only metadata request exceeded its scope or bounds')
            self.requests += 1
            kwargs.update(timeout=(10, 30), stream=True)
            return send(adapter, request, **kwargs)

        session = self.service.session
        # Keep authenticated cookies, but bypass provider retry/logging behavior.
        def request(method, url, **kwargs):
            kwargs.update(allow_redirects=False, timeout=(10, 30), stream=True)
            return Session.request(session, method, url, **kwargs)

        with patch.object(session, 'trust_env', False), patch.object(session, 'request', request), \
                patch.object(HTTPAdapter, 'send', guarded), read_guard(self.pacer, self.service, self.scope) as state:
            state.update(photo_root=self.root, approved_zone=self.scope['zone_id'], approved_album=self.scope['album_id'])
            yield

    def snapshot(self):
        if self.stopped or self.reads >= 2:
            raise Blocked('Read-only metadata session is closed; start a new preflight')
        self.reads += 1
        try:
            with self.transport():
                if find_album(self.service, self.root, self.scope['zone_id'], self.album) != self.scope['album_id']:
                    raise Blocked('Named album differs from the saved draft')
                passes = [collect_pass(self.service, self.root, self.scope['zone_id'],
                                       self.scope['album_id'], expected_count=self.count) for _ in range(2)]
            if (authenticated_account(self.service) != self.scope['account_id']
                    or canonical({key: passes[0][key] for key in ('assets', 'masters', 'relations')})
                    != canonical({key: passes[1][key] for key in ('assets', 'masters', 'relations')})):
                raise Blocked('Read-only metadata changed between passes')
            report = build_resource_report(passes[-1])
            if report['findings']:
                raise Blocked('Read-only resource metadata has unresolved findings')
            assets = []
            for asset in report['assets']:
                resources = []
                for index in asset['resource_indices']:
                    item = report['resources'][index]
                    resources.append({'resource_record_type': item['record_type'], 'resource_record_id': item['record_id'],
                                      'resource_field': item['resource_field'], 'owner_change_tag': item['record_change_tag'],
                                      **{key: item[key] for key in ('file_type', 'size', 'provider_checksum_opaque')}})
                assets.append({'id': asset['asset_id'], 'resources': resources,
                               **{key: asset[key] for key in ('master_id', 'asset_change_tag', 'master_change_tag')}})
            result = {'source': SOURCE, 'scope': self.scope, 'assets': assets, 'complete': True}
            if self.output is not None:
                save_new(self.output / f'metadata-{self.reads}-private.json', result)
            return result
        except (Exception, KeyboardInterrupt):
            self.stopped = True
            # Provider exceptions may contain URLs, response bodies or credentials.
            raise Blocked('Read-only metadata unavailable, changed, throttled or outside scope; no retry attempted') from None


@contextmanager
def authenticate(draft, album, reuse_session=None):
    """Interactive opt-in only; never called by offline rehearsal."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Blocked('Read-only cloud preflight requires your interactive Terminal')
    require_provider()
    print(f'Album: {album}; {len(draft["assets"])} recorded items.')
    print('Read-only scope: authenticate, discover library zones and compare the album metadata.')
    print('No media downloads or changes. Requests start at least one second apart; throttling stops the run.')
    confirm('Run read-only check?')
    old_mask, old_logging = os.umask(0o077), logging.root.manager.disable
    run, provider, service = None, None, None
    summary = {'schema': 1, 'provider_version': PIN, 'authenticated': False,
               'scope': 'read_only_preflight', 'completed': False, 'removal_approved': False}
    pacer = Pacer()
    try:
        logging.disable(logging.CRITICAL)
        from pyicloud_ipd import PyiCloudService
        repo = Path(__file__).resolve().parents[2]
        run = new_run(repo)
        cookies = reuse_cookies(repo, reuse_session) if reuse_session else run / 'cookies'
        print(f'Private metadata output: {run}')
        with read_guard(pacer):
            account = getpass.getpass('Apple account email (hidden, local only): ').strip()
            if not account:
                raise Blocked('Missing account')
            service = PyiCloudService('com', account, lambda: getpass.getpass('Apple password (local only): '),
                                     cookie_directory=str(cookies), with_family=False, http_timeout=30)
            if service.requires_2fa:
                verify_second_factor(service)
            if service.requires_2sa or authenticated_account(service) != draft['scope']['account_id']:
                raise Blocked('Authenticated account differs from the draft')
            summary['authenticated'] = True
            root = service._get_webservice_url('ckdatabasews')
        with read_guard(pacer, service, draft['scope']) as state:
            state['photo_root'] = root
            zones, errors = discover_zones(service, root)
            if errors or set(zones) != {'private', 'shared'}:
                raise Blocked('Library discovery was incomplete')
        provider = CloudMetadata(service, root, zones, album, draft, pacer, run)
        yield provider
        summary['completed'] = True
    except (Exception, KeyboardInterrupt):
        raise Blocked('Read-only preflight stopped; no cloud changes were made') from None
    finally:
        try:
            if service is not None:
                service.session.close()
            summary.update(metadata_snapshots=provider.reads if provider else 0,
                           metadata_requests=provider.requests if provider else 0,
                           rate_limited=pacer.pause is not None)
            if pacer.pause is not None:
                summary['retry_after_seconds'] = pacer.pause.retry_after_seconds
            if run is not None:
                save_new(run / 'summary.json', summary)
        finally:
            logging.disable(old_logging)
            os.umask(old_mask)
