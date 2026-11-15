"""Connect approved cloud transport to preflight and read-only postconditions.

No authentication or command-line entry point. Callers need separate account
access approval; tests use synthetic records and mocked HTTP only.
"""

import copy
from datetime import datetime, timezone
from pathlib import Path
import uuid

from cloud_metadata import CloudMetadata, SOURCE, assets_from_report
from cloud_removal import CloudRemoval
from preflight import metadata_for, ordered
from prototype import Blocked, Plan, canonical, digest, safe_path, write_new
from resources import ResourceReportBlocked, build_resource_report


def target_observations(draft, records):
    try:
        report = build_resource_report({'assets': [r for r in records if r['recordType'] == 'CPLAsset'],
                                        'masters': [r for r in records if r['recordType'] == 'CPLMaster']})
    except ResourceReportBlocked:
        raise Blocked('Target resource observations are unavailable') from None
    if report['findings']:
        raise Blocked('Target resource observations are incomplete')
    observed = {a['id']: a for a in assets_from_report(report)}
    expected = {a['id']: a for a in metadata_for(draft)['assets'] if a['id'] in draft['selected']}
    by_id = {r['recordName']: r for r in records}
    if observed.keys() != expected.keys():
        raise Blocked('Target observations differ from the selection')
    result = []
    for key, asset in observed.items():
        old = expected[key]
        def resources(value):
            return sorted(({k: v for k, v in r.items() if k != 'owner_change_tag'} for r in value['resources']),
                          key=lambda r: (r['resource_record_type'], r['resource_record_id'], r['resource_field']))
        result.append({'id': key, 'master_id': asset['master_id'],
                       'deletion_flag': by_id[key]['fields']['isDeleted'],
                       'resources_match': asset['master_id'] == old['master_id'] and resources(asset) == resources(old),
                       'asset_tag_matches': asset['asset_change_tag'] == old['asset_change_tag'],
                       'master_tag_matches': asset['master_change_tag'] == old['master_change_tag']})
    return result


class CloudCleanup:
    def __init__(self, service, root, zones, album, draft, pacer):
        self.draft = draft
        self.writer = CloudRemoval(service, root, zones, draft, pacer)
        self.service, self.root, self.zones = service, root, copy.deepcopy(zones)
        self.album, self.pacer = album, pacer
        self.metadata = self._metadata(draft.data())

    def _metadata(self, data, *, allow_empty=False):
        return CloudMetadata(self.service, self.root, self.zones, self.album, data, self.pacer, allow_empty=allow_empty)

    def before(self):
        return self._metadata(self.draft.data()).snapshot()

    def remove(self, request):
        return [{'id': item['id'], **({'status': 'accepted'} if item['reply'] == 'ACCEPTED' else
                                    {'status': 'rejected'} if item['reply'] == 'REJECTED' else {'status': 'unknown'})}
                for item in self.writer.remove(request)]

    def postcheck(self, draft, states):
        if canonical(draft) != canonical(self.draft.data()):
            raise Blocked('Post-check draft changed')
        records = self.writer.lookup()
        observations = target_observations(self.draft.data(), records)
        removed = set()
        for item in observations:
            key, deleted = item['id'], item['deletion_flag'] == 1
            if (not item['resources_match'] or not item['master_tag_matches']
                    or states[key] == 'accepted' and not deleted
                    or states[key] == 'rejected' and (deleted or not item['asset_tag_matches'])):
                raise Blocked('Target postcondition differs from the reviewed resources or reply')
            if deleted:
                removed.add(key)
        remaining = self.draft.data()
        remaining['assets'] = [a for a in remaining['assets'] if a['id'] not in removed]
        current = self._metadata(remaining, allow_empty=True).snapshot()
        if canonical(ordered(current)) != canonical(ordered(metadata_for(remaining, SOURCE))):
            raise Blocked('Remaining album changed after the request')
        return {'targets': observations, 'lookup_records': records, 'remaining_album': current, 'remaining_album_matched': True,
                'unknown_replies_remain_unresolved': True}


def recovery_input(path):
    """Validate the attempted selection locally before asking for account access."""
    from supervised import load_run, read_events, same_copies
    plan, outputs = load_run(path)
    data = plan.data()
    events = read_events(outputs, plan)
    if data['mode'] != 'SUPERVISED_ICLOUD_CLEANUP' or not events or events[0].get('status') != 'attempted':
        raise Blocked('Recovery requires an attempted connected cleanup')
    same_copies(tuple(output.parent.parent for output in outputs), 'cleanup-started.json',
                {'plan_sha256': plan.sha256, 'outputs': data['outputs']})
    envelope = data['draft']
    draft = Plan(envelope['payload'], envelope['sha256'])
    if draft.sha256 != data['draft_sha256']:
        raise Blocked('Recovery draft binding changed')
    draft.data()
    return plan, outputs, draft


def recover_run(path, service, root, zones, pacer):
    """Record new target facts only; never alter outcomes, clear intent or retry."""
    from supervised import same_copies
    plan, outputs, draft = recovery_input(path)
    data = plan.data()
    reader = CloudRemoval(service, root, zones, draft, pacer, recovery_only=True)
    if canonical(reader.request()) != canonical(data['request']):
        raise Blocked('Recovery selection differs from the attempted request')
    records = reader.lookup()
    result = {'plan_sha256': plan.sha256, 'at': datetime.now(timezone.utc).isoformat(),
              'targets': target_observations(draft.data(), records), 'lookup_records': records, 'retry_allowed': False,
              'album_membership': 'NOT VERIFIED', 'outcomes_changed': False}
    name, destinations = uuid.uuid4().hex, []
    for output in outputs:
        parent = safe_path(output) / 'recovery'
        parent.mkdir(mode=0o700, exist_ok=True)
        safe_path(parent)
        destination = parent / name
        destination.mkdir(mode=0o700)
        destinations.append(destination)
    result['outputs'] = list(map(str, destinations))
    for destination in destinations:
        write_new(destination / 'observations.json', canonical(result))
    same_copies(destinations, 'observations.json', result)
    marker = {'plan_sha256': plan.sha256, 'observations_sha256': digest(result)}
    for destination in destinations:
        write_new(destination / 'complete.json', canonical(marker))
    same_copies(destinations, 'complete.json', marker)
    return result
