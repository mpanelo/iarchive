"""Download one explicitly approved, frozen research set. Never modify Photos."""

import argparse
from collections import Counter
from contextlib import contextmanager
import getpass
import json
import logging
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-cleanup'))
from cloud_metadata import CloudMetadata, SOURCE, require_provider
from album import collect_pass, find_album
from changes import ChangesThrottled, canonical, check_throttle, text, valid_zone
from conflict import Pacer, read_guard, read_report
from originals import (MAX_VARIANT_BYTES, MAX_VARIANT_TOTAL_BYTES, check_path,
                       run_downloads, valid_media_url)
from probe import (PIN, authenticated_account, new_run, discover_zones,
                   reuse_cookies, save_new, verify_second_factor)
from prototype import Blocked, hash_file
from resources import PREVIEW_FIELDS, RESOURCE_ROLES, build_resource_report
from prompts import Cancelled, confirm


MAX_ASSETS, MAX_RESOURCES = 5, 21
RESOURCE_KEYS = {'resource_record_type', 'resource_record_id', 'resource_field',
                 'owner_change_tag', 'file_type', 'size', 'provider_checksum_opaque'}
ASSET_KEYS = {'id', 'master_id', 'asset_change_tag', 'master_change_tag', 'resources'}


def frozen(value):
    """Validate an exact metadata projection, without merging resources by field."""
    value = json.loads(canonical(value))
    if (not isinstance(value, dict) or set(value) != {'source', 'scope', 'assets', 'complete'} or value['source'] != SOURCE
            or value['complete'] is not True or not isinstance(value['assets'], list)
            or not 1 <= len(value['assets']) <= MAX_ASSETS):
        raise Blocked('A complete bounded metadata capture is required')
    scope = value['scope']
    if (not isinstance(scope, dict) or set(scope) != {'account_id', 'database', 'zone_id', 'album_id'}
            or scope['database'] != 'private' or not valid_zone(scope['zone_id'])
            or scope['zone_id']['zoneName'] != 'PrimarySync'
            or any(not text(scope[key]) for key in ('account_id', 'album_id'))):
        raise Blocked('Captured account, private zone or album is incomplete')
    assets, masters, identities, sizes = set(), set(), set(), []
    for asset in value['assets']:
        if (not isinstance(asset, dict) or set(asset) != ASSET_KEYS or any(not text(asset[key]) for key in ASSET_KEYS - {'resources'})
                or asset['id'] in assets or asset['master_id'] in masters
                or not isinstance(asset['resources'], list) or not asset['resources']):
            raise Blocked('Captured asset or master ownership is ambiguous')
        assets.add(asset['id'])
        masters.add(asset['master_id'])
        fields = Counter()
        owners = {'CPLAsset': (asset['id'], asset['asset_change_tag']),
                  'CPLMaster': (asset['master_id'], asset['master_change_tag'])}
        for item in asset['resources']:
            if not isinstance(item, dict) or set(item) != RESOURCE_KEYS or any(not text(item[key]) for key in RESOURCE_KEYS - {'size'}):
                raise Blocked('Captured resource metadata is incomplete')
            key = tuple(item[key] for key in ('resource_record_type', 'resource_record_id', 'resource_field'))
            if (key in identities or owners.get(key[0]) != (key[1], item['owner_change_tag'])
                    or key[2] not in RESOURCE_ROLES):
                raise Blocked('Captured resource identity or owner is unknown or duplicated')
            identities.add(key)
            fields[key[2]] += 1
            if type(item['size']) is not int or not 0 < item['size'] <= MAX_VARIANT_BYTES:
                raise Blocked('Captured resource exceeds the 32 MiB limit')
            sizes.append(item['size'])
        if fields['resOriginalRes'] != 1 or any(count > 1 and field not in PREVIEW_FIELDS for field, count in fields.items()):
            raise Blocked('Captured originals or rendition ownership is ambiguous')
        asset['resources'].sort(key=lambda item: tuple(item[key] for key in
                                   ('resource_record_type', 'resource_record_id', 'resource_field')))
    if not 1 <= len(sizes) <= MAX_RESOURCES or sum(sizes) > MAX_VARIANT_TOTAL_BYTES:
        raise Blocked('Captured resources exceed the 21-resource or 64 MiB limit')
    value['assets'].sort(key=lambda asset: asset['id'])
    return canonical(value)


class Baseline:
    def __init__(self, repo, path):
        self.repo, self.path = repo, Path(os.path.abspath(path))
        value, self.sha256 = read_report(repo, self.path)
        self.payload = frozen(value)
        self.summary_path = self.path.parent / 'summary.json'
        summary, self.summary_sha256 = read_report(repo, self.summary_path, 65536)
        match = re.fullmatch(r'metadata-([12])-private.json', self.path.name)
        if (not match or summary.get('schema') != 1 or summary.get('provider_version') != PIN
                or summary.get('scope') != 'read_only_preflight' or summary.get('authenticated') is not True
                or summary.get('completed') is not True or type(summary.get('metadata_snapshots')) is not int
                or not int(match[1]) <= summary['metadata_snapshots'] <= 2):
            raise Blocked('Baseline lacks completed pinned metadata-capture evidence')

    def data(self):
        return json.loads(self.payload)

    def check(self):
        if (read_report(self.repo, self.path)[1] != self.sha256
                or read_report(self.repo, self.summary_path, 65536)[1] != self.summary_sha256):
            raise Blocked('The approved metadata capture changed')



def capture(provider, baseline):
    """Collect URLs only in memory; compare all owner-qualified resource metadata."""
    baseline.check()
    if provider.stopped or provider.reads >= 2:
        raise Blocked('Metadata capture is closed')
    provider.reads += 1
    urls = {}
    try:
        with provider.transport():
            scope = provider.scope
            if find_album(provider.service, provider.root, scope['zone_id'], provider.album) != scope['album_id']:
                raise Blocked('Named album differs from the approved capture')
            first = collect_pass(provider.service, provider.root, scope['zone_id'], scope['album_id'], expected_count=provider.count)
            second = collect_pass(provider.service, provider.root, scope['zone_id'], scope['album_id'],
                                  resource_urls=urls, download_fields=tuple(RESOURCE_ROLES), expected_count=provider.count)
        if (authenticated_account(provider.service) != scope['account_id']
                or canonical({key: first[key] for key in ('assets', 'masters', 'relations')})
                != canonical({key: second[key] for key in ('assets', 'masters', 'relations')})):
            raise Blocked('Album changed between metadata passes')
        report = build_resource_report(second)
        if report['findings']:
            raise Blocked('Fresh resource metadata has unresolved findings')
        assets, plans = [], []
        for asset in report['assets']:
            resources = []
            for index in asset['resource_indices']:
                item = report['resources'][index]
                resources.append({'resource_record_type': item['record_type'], 'resource_record_id': item['record_id'],
                                  'owner_change_tag': item['record_change_tag'],
                                  **{key: item[key] for key in ('resource_field', 'file_type', 'size', 'provider_checksum_opaque')}})
                binding = {**scope, **{key: asset[key] for key in ('asset_id', 'master_id', 'asset_change_tag', 'master_change_tag')},
                           'resource_record_type': item['record_type'], 'resource_record_id': item['record_id'],
                           'resource_record_change_tag': item['record_change_tag'], 'resource_field': item['resource_field'],
                           'resource_size': item['size'], 'resource_file_type': item['file_type'],
                           'provider_checksum_opaque': item['provider_checksum_opaque']}
                plans.append({'binding': binding, 'url': urls.get((item['record_type'], item['record_id'], item['resource_field']))})
            assets.append({'id': asset['asset_id'], 'resources': resources,
                           **{key: asset[key] for key in ('master_id', 'asset_change_tag', 'master_change_tag')}})
        if frozen({'source': SOURCE, 'scope': scope, 'assets': assets, 'complete': True}) != baseline.payload:
            raise Blocked('Fresh metadata differs from the approved capture')
        if any(not valid_media_url(plan['url']) for plan in plans) or len({plan['url'] for plan in plans}) != len(plans):
            raise Blocked('Fresh resource URLs are missing, unsafe or ambiguous')
        baseline.check()
        report.update(**scope, metadata_passes_match=True)
        return report, plans
    except (Exception, KeyboardInterrupt):
        provider.stopped = True
        raise


class MediaTransport:
    def __init__(self, provider, baseline, plans):
        from requests import Request
        self.provider, self.baseline = provider, baseline
        self.ordered = [Request('GET', plan['url']).prepare().url for plan in plans]
        self.pending = set(self.ordered)
        if len(self.pending) != len(plans):
            raise Blocked('Resource URLs collide after request normalization')
        self.attempted = 0
        self.state = {'approved_download_urls': set(self.pending)}

    @contextmanager
    def guard(self):
        from requests import Session
        from requests.adapters import HTTPAdapter
        send = HTTPAdapter.send

        def guarded(adapter, request, **kwargs):
            self.baseline.check()
            if (request.method != 'GET' or request.body is not None or request.url not in self.pending
                    or request.url != self.ordered[self.attempted]
                    or request.headers.get('Range') != 'bytes=0-' or kwargs.get('verify', True) is not True
                    or adapter.max_retries.total != 0 or self.attempted >= MAX_RESOURCES
                    or authenticated_account(self.provider.service) != self.provider.scope['account_id']):
                raise Blocked('Media request exceeded its frozen scope or bounds')
            self.provider.pacer.wait()
            self.pending.remove(request.url)
            self.state['approved_download_urls'].discard(request.url)
            self.attempted += 1
            response = send(adapter, request, **{**kwargs, 'timeout': (10, 30), 'stream': True})
            try:
                check_throttle(response.status_code, response.headers, None)
                if 300 <= response.status_code < 400:
                    raise Blocked('Media redirect refused')
                return response
            except ChangesThrottled as error:
                self.provider.pacer.pause = error
                response.close()
                raise
            except BaseException:
                response.close()
                raise

        session = self.provider.service.session
        def request(method, url, **kwargs):
            return Session.request(session, method, url, **{**kwargs, 'allow_redirects': False, 'timeout': (10, 30), 'stream': True})

        with patch.object(session, 'trust_env', False), patch.object(session, 'request', request), patch.object(HTTPAdapter, 'send', guarded):
            yield self.state


def publish(path, value):
    check_path(path)
    save_new(path, value)


def download(provider, baseline, run, summary):
    """Publish completion only after postflight and fresh hashes of both copies."""
    run = Path(os.path.abspath(run))
    if not run.is_relative_to(baseline.repo / '.smoke'):
        raise Blocked('Research output must stay inside this checkout\'s .smoke directory')
    check_path(run)
    report, plans = capture(provider, baseline)
    publish(run / 'resource-report-private.json', report)
    publish(run / 'approved-metadata-private.json', baseline.data())
    pending = []
    def save(path, value):
        if path.name == 'download-manifest-private.json':
            pending.append(value)
        else:
            publish(path, value)
    media = MediaTransport(provider, baseline, plans)
    try:
        with media.guard() as transport:
            result = run_downloads(provider.service.session, plans, run, transport, save)
        if media.pending or len(pending) != 1:
            raise Blocked('Resource download set is incomplete')
        after, _ = capture(provider, baseline)
        publish(run / 'resource-report-after-private.json', after)
        for item in pending[0]['resources']:
            expected = (item['resource_size'], item['response_sha256'])
            if any(hash_file(run / item[key]) != expected for key in ('copy_a', 'copy_b')):
                raise Blocked('A local research copy changed after download')
        baseline.check()
        manifest = {**pending[0], 'metadata_before_and_after_match': True, 'baseline_sha256': baseline.sha256}
        manifest_path = run / 'download-manifest-private.json'
        publish(manifest_path, manifest)
        saved_manifest, manifest_sha = read_report(baseline.repo, manifest_path)
        saved_report, report_sha = read_report(baseline.repo, run / 'resource-report-private.json')
        saved_after, after_sha = read_report(baseline.repo, run / 'resource-report-after-private.json')
        if any(canonical(saved) != canonical(expected) for saved, expected in
               ((saved_manifest, manifest), (saved_report, report), (saved_after, after))):
            raise Blocked('Published download evidence changed')
        completion = {'baseline_sha256': baseline.sha256, 'manifest_sha256': manifest_sha,
                      'resource_report_sha256': report_sha, 'postflight_report_sha256': after_sha}
        publish(run / 'complete.json', completion)
        if read_report(baseline.repo, run / 'complete.json')[0] != completion:
            raise Blocked('Published download completion changed')
        summary.update(result, completed=True)
    finally:
        summary.update(media_gets=media.attempted, metadata_requests=provider.requests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path, help='Private metadata-N-private.json capture')
    parser.add_argument('--album', required=True)
    parser.add_argument('--reuse-session')
    parser.add_argument('--live', action='store_true', help='Requires separately approved media access and confirmation')
    args = parser.parse_args()
    repo, run, service, provider = Path(__file__).resolve().parents[2], None, None, None
    summary = {'schema': 1, 'scope': 'observed_resource_download', 'provider_version': PIN,
               'authenticated': False, 'completed': False, 'media_gets': 0, 'metadata_requests': 0,
               'modification_requests': 0, 'removal_approved': False, 'physical_drive_independence': 'NOT VERIFIED',
               'cloud_coverage': 'NOT VERIFIED'}
    pacer = Pacer()
    old_mask, old_logging = os.umask(0o077), logging.root.manager.disable
    try:
        require_provider()
        baseline = Baseline(repo, args.baseline)
        data = baseline.data()
        resources = [item for asset in data['assets'] for item in asset['resources']]
        print(f'Album: {args.album}')
        print(f"Frozen test set: {len(data['assets'])} items, {len(resources)} resources, {sum(item['size'] for item in resources):,} bytes.")
        print('Limits: 32 MiB each, 64 MiB total; two local copies. No Photos modifications or removal.')
        print('Metadata is checked before and after. One-second request spacing; throttling stops without retry.')
        if not args.live:
            print(f'Destination: a fresh research directory under {repo / ".smoke"}')
            print('Offline preparation only. No authentication or requests attempted. Live downloads require separate approval.')
            return 0
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise Blocked('Live research requires your interactive Terminal')
        run = new_run(repo)
        print(f'Destination: {run}')
        confirm('Download these resources?')
        baseline.check()
        logging.disable(logging.CRITICAL)
        from pyicloud_ipd import PyiCloudService
        cookies = reuse_cookies(repo, args.reuse_session) if args.reuse_session else run / 'cookies'
        with read_guard(pacer):
            account = getpass.getpass('Apple account email (hidden, local only): ').strip()
            if not account:
                raise Blocked('Missing account')
            service = PyiCloudService('com', account, lambda: getpass.getpass('Apple password (local only): '),
                                     cookie_directory=str(cookies), with_family=False, http_timeout=30)
            if service.requires_2fa:
                verify_second_factor(service)
            if service.requires_2sa or authenticated_account(service) != data['scope']['account_id']:
                raise Blocked('Authenticated account differs from the approved capture')
            summary['authenticated'] = True
            root = service._get_webservice_url('ckdatabasews')
        with read_guard(pacer, service, data['scope']) as state:
            state['photo_root'] = root
            zones, errors = discover_zones(service, root)
            if errors or set(zones) != {'private', 'shared'}:
                raise Blocked('Library discovery was incomplete')
        provider = CloudMetadata(service, root, zones, args.album, data, pacer)
        download(provider, baseline, run, summary)
    except Cancelled:
        summary['cancelled'] = True
        print('Cancelled. No account access or downloads were started.')
        return 0
    except (Exception, KeyboardInterrupt):
        summary['blocked'] = True
        print('Research download stopped. No retry or Photos modification was attempted.', file=sys.stderr)
    finally:
        try:
            if service is not None:
                service.session.close()
            if provider is not None:
                summary['metadata_requests'] = provider.requests
            if pacer.pause is not None:
                summary.update(rate_limited=True, retry_after_seconds=pacer.pause.retry_after_seconds)
            if run is not None:
                publish(run / 'summary.json', summary)
        finally:
            logging.disable(old_logging)
            os.umask(old_mask)
    print(json.dumps(summary, sort_keys=True))
    return int(not summary['completed'])


if __name__ == '__main__':
    raise SystemExit(main())
