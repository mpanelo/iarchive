"""Rehearse one supervised cleanup request from a saved review. No cloud writes."""

import argparse
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

from preflight import CHECK_SECONDS, MockMetadata, Preflight, metadata_for, ordered
from prepare_review import ERRORS, ReviewDraft, evidence
from cleanup import Journal
from prototype import Blocked, Plan, canonical, digest, disjoint, safe_path, write_new
from prompts import Cancelled, confirm


MODE = 'OFFLINE_SUPERVISED_REVIEW'
MAX_TARGETS = 100  # Our rehearsal bound, not a provider capability claim.
LIMITS = {'live_execution': 'UNAVAILABLE', 'server_version_condition': 'NOT ENFORCED',
          'cloud_coverage': 'NOT VERIFIED', 'resource_completeness': 'NOT VERIFIED',
          'recovery_and_space_reclaimed': 'NOT VERIFIED'}
CLOUD_MODE = 'SUPERVISED_ICLOUD_CLEANUP'
CLOUD_LIMITS = {**LIMITS, 'live_execution': 'SEPARATE_APPROVAL_REQUIRED'}


class Backups:
    """Fresh local hashes, plus either modeled or strict OS drive verification."""

    def __init__(self, executable=None):
        self.executable = executable
        self.independent = True  # Invented topology in simulation only.

    def check(self, session):
        session.check()
        if self.executable is None:
            if not self.independent:
                raise Blocked('The modeled backups are not on independent drives')
            return {'physical_drives': 'SIMULATED; NOT VERIFIED',
                    'root_identities': session.root_identities()}
        previous, _ = evidence.load_input(session.bundles[0] / 'manifest.jsonl', jsonl=True)
        args = SimpleNamespace(iarchive=Path(self.executable),
                               state_dir=session.bundles[0].parent.parent,
                               primary_artifacts=session.bundles[1].parent,
                               secondary_artifacts=session.bundles[2].parent,
                               allow_unverified_devices=False)
        fresh, _, verification = evidence.fresh_replica(args, previous)
        if (verification['independent_physical_drives'] != 'VERIFIED BY OS TOPOLOGY'
                or not evidence.independent(fresh[0]['source_identity'], fresh[0]['secondary_identity'])
                or canonical(fresh[1:]) != canonical(previous[1:])):
            raise Blocked('Fresh physical backups do not match the reviewed files')
        session.check()
        return {'physical_drives': 'VERIFIED BY OS TOPOLOGY',
                'root_identities': session.root_identities()}


class MockRemoval:
    """Unconditional in-memory soft-delete model; no transport or authentication."""

    def __init__(self, draft):
        self.metadata = MockMetadata(draft)
        self.trash, self.requests, self.faults = {}, [], {}

    def remove(self, request):
        self.requests.append(copy.deepcopy(request))
        replies = []
        for operation in request['operations']:
            key = operation['record']['recordName']
            fault = self.faults.get(key)
            if fault == 'lost_before':
                raise TimeoutError
            if fault == 'error':
                replies.append({'id': key, 'error': 'CONFLICT'})
                continue
            asset = next(a for a in self.metadata.current['assets'] if a['id'] == key)
            self.metadata.current['assets'].remove(asset)
            self.trash[key] = asset
            if fault == 'lost_after':
                raise TimeoutError
            if fault == 'interrupt_after':
                raise KeyboardInterrupt
            replies.append({'id': 'wrong-id' if fault == 'wrong_id' else key, 'status': 'accepted'})
        return replies

    def before(self):
        return self.metadata.snapshot()

    def postcheck(self, draft, states):
        done = {key for key, status in states.items() if status == 'accepted'}
        expected = metadata_for(draft)
        expected['assets'] = [a for a in expected['assets'] if a['id'] not in done]
        trash = {a['id']: a for a in metadata_for(draft)['assets'] if a['id'] in done}
        if canonical(ordered(self.metadata.snapshot())) != canonical(ordered(expected)) or self.trash != trash:
            raise Blocked('Accepted reply did not establish the exact mock postcondition')


def same_copies(outputs, name, expected):
    for output in outputs:
        if canonical(evidence.load_input(safe_path(output) / name)[0]) != canonical(expected):
            raise Blocked('Cleanup records differ or are incomplete; do not retry')


class Cleanup:
    def __init__(self, session, provider, backups):
        self.cloud = type(provider) is not MockRemoval
        if self.cloud:
            from cloud_workflow import CloudCleanup
            if type(provider) is not CloudCleanup:
                raise Blocked('Use the offline removal model or connected cloud workflow')
            if provider.draft != session.current:
                raise Blocked('Connected provider differs from the saved selection')
        if type(backups) is not Backups or self.cloud and backups.executable is None:
            raise Blocked('Connected cleanup requires strict physical-drive verification')
        self.session, self.provider, self.backups = session, provider, backups
        self.gate = Preflight(session, provider.metadata)
        self.draft = self.gate.draft
        if not 0 < self.draft.data()['count'] <= MAX_TARGETS:
            raise Blocked('Select between one and 100 items for one rehearsal request')
        self.phase, self.outputs, self.plan = 'new', (), None
        self.mode, self.limits = (CLOUD_MODE, CLOUD_LIMITS) if self.cloud else (MODE, LIMITS)
        self._approval = None

    def check_backups(self):
        topology = self.backups.check(self.session)
        if self.cloud and topology['physical_drives'] != 'VERIFIED BY OS TOPOLOGY':
            raise Blocked('Connected cleanup requires verified physical drives')
        return topology

    def publish(self, name, value):
        for output in self.outputs:
            write_new(safe_path(output) / name, canonical(value))
        same_copies(self.outputs, name, value)

    def prepare(self):
        if self.phase != 'new':
            raise Blocked('This cleanup has already started')
        self.phase = 'preparing'
        try:
            if any(os.path.lexists(Path(p) / 'cleanup-started.json') for p in self.draft.data()['outputs']):
                raise Blocked('This draft already started cleanup; inspect the retained run without retrying')
            if self.cloud:
                topology = self.check_backups()
            preflight = self.gate.prepare()
            self.receipt = self.gate.finish(preflight)
            if not self.cloud:
                topology = self.check_backups()
            name, outputs = uuid.uuid4().hex, []
            for source in self.draft.data()['outputs']:
                parent = safe_path(Path(source)) / 'cleanup'
                parent.mkdir(mode=0o700, exist_ok=True)
                safe_path(parent)
                output = parent / name
                output.mkdir(mode=0o700)
                (output / 'journal').mkdir(mode=0o700)
                outputs.append(output)
            self.outputs = tuple(outputs)
            disjoint((*self.session.roots, *self.outputs))
            draft = self.draft.data()
            targets = [a for a in draft['assets'] if a['id'] in draft['selected']]
            request = {'atomic': True, 'desiredKeys': ['isDeleted'], 'zoneID': draft['scope']['zone_id'],
                       'operations': [{'operationType': 'update', 'record': {
                           'recordType': 'CPLAsset', 'recordName': a['id'],
                           'recordChangeTag': a['asset_change_tag'], 'fields': {'isDeleted': {'value': 1}}}}
                           for a in targets]}
            if self.cloud and canonical(request) != canonical(self.provider.writer.request()):
                raise Blocked('Connected request differs from the saved selection')
            data = {'schema': 2 if self.cloud else 1, 'mode': self.mode, 'limits': dict(self.limits), 'draft_sha256': self.draft.sha256,
                    'count': draft['count'], 'kept': draft['kept'], 'request': request,
                    'preflight_sha256': preflight.sha256, 'backups': topology,
                    'outputs': list(map(str, self.outputs)), 'roots': list(map(str, self.session.roots)),
                    'created_at': datetime.now(timezone.utc).isoformat()}
            if self.cloud:
                data['draft'] = {'payload': self.draft.payload, 'sha256': self.draft.sha256}
            self.plan = Plan(canonical(data), digest(data))
            self.publish('plan.json', {'payload': self.plan.payload, 'sha256': self.plan.sha256})
            self.deadline = time.monotonic() + CHECK_SECONDS
            self.phase = 'prepared'
            return self.plan
        except (*ERRORS, KeyboardInterrupt):
            self.phase = 'blocked'
            raise

    def confirm_and_execute(self):
        if self.phase != 'prepared':
            raise Blocked('Prepare a cleanup before confirming it')
        plan = self.plan
        data = plan.data()
        print(f"Selected: {data['count']} items. Kept: {len(data['kept'])} items.")
        print('One soft-delete request affects synced devices. Concurrent edits cannot be prevented.' if self.cloud
              else 'One mock soft-delete request; local copies are preserved.')
        try:
            confirm('Move this selection to iCloud Recently Deleted?' if self.cloud else 'Simulate this selection?')
        except Cancelled:
            self.phase = 'cancelled'
            raise
        self._approval = plan
        return self.execute(plan)

    def execute(self, plan):
        with self.session.lock:
            if self.phase != 'prepared':
                raise Blocked('This cleanup is closed; inspect its journal without retrying')
            self.phase = self.session.phase = 'executing'
            try:
                if type(plan) is not Plan or plan != self.plan:
                    raise Blocked('Cleanup plan changed')
                if self.cloud and self._approval != plan:
                    raise Blocked('Connected cleanup requires final confirmation')
                data = plan.data()
                self.gate.check_selection()
                same_copies(self.outputs, 'plan.json', {'payload': plan.payload, 'sha256': plan.sha256})
                self.gate.check_copies('plan.json', {'payload': self.gate.plan.payload,
                                                     'sha256': self.gate.plan.sha256})
                self.gate.check_copies('receipt.json', self.receipt)
                self.gate.check_copies('complete.json', {'plan_sha256': self.gate.plan.sha256,
                                                       'receipt_sha256': digest(self.receipt)})
                if any(list(safe_path(output / 'journal').iterdir()) for output in self.outputs):
                    raise Blocked('Cleanup already has a journal; never retry a started request')
                topology = self.check_backups()
                if canonical(topology) != canonical(data['backups']):
                    raise Blocked('Backup topology changed')
                if canonical(ordered(self.provider.before())) != self.gate.expected_metadata:
                    raise Blocked('Cloud metadata changed before the request')
                self.gate.check_selection()
                same_copies(self.outputs, 'plan.json', {'payload': plan.payload, 'sha256': plan.sha256})
                if time.monotonic() >= self.deadline:
                    raise Blocked('Cleanup confirmation expired; prepare a new rehearsal')
                draft_outputs = tuple(safe_path(Path(p)) for p in self.draft.data()['outputs'])
                marker = {'plan_sha256': plan.sha256, 'outputs': data['outputs']}
                # Exclusive markers prevent another process or restart from replaying this draft.
                for output in draft_outputs:
                    write_new(output / 'cleanup-started.json', canonical(marker))
                same_copies(draft_outputs, 'cleanup-started.json', marker)
                journal = Journal(self.outputs, plan)
                journal.append(None, 'attempted', 'One frozen request; intent flushed before execution')
                # Read all durable intent copies back before crossing the write boundary.
                read_events(self.outputs, plan)
                states = {key: 'unknown' for key in self.draft.data()['selected']}
                replies_sha, postcheck_sha = None, None
                try:
                    stage = 'request'
                    self.gate.check_selection()
                    if time.monotonic() >= self.deadline:
                        raise Blocked('Confirmation expired while persisting intent; no request sent')
                    replies = self.provider.remove(copy.deepcopy(data['request']))
                    stage = 'reply'
                    if (not isinstance(replies, list) or len(replies) != len(states)
                            or any(not isinstance(r, dict) or r.get('id') not in states for r in replies)
                            or len({r['id'] for r in replies}) != len(states)):
                        raise Blocked('Reply identities are uncertain')
                    for reply in replies:
                        if reply == {'id': reply['id'], 'status': 'accepted'}:
                            states[reply['id']] = 'accepted'
                        elif reply == {'id': reply['id'], 'error': 'CONFLICT'} or reply == {'id': reply['id'], 'status': 'rejected'}:
                            states[reply['id']] = 'rejected'
                        elif reply == {'id': reply['id'], 'status': 'unknown'}:
                            states[reply['id']] = 'unknown'
                        else:
                            raise Blocked('Per-record reply is uncertain')
                    self.publish('replies.json', replies)
                    replies_sha = digest(replies)
                    stage = 'postcheck'
                    observation = self.provider.postcheck(self.draft.data(), states)
                    if observation is not None:
                        self.publish('postcheck.json', observation)
                        postcheck_sha = digest(observation)
                    finding = 'Some request outcomes remain unresolved' if 'unknown' in states.values() else None
                except (*ERRORS, TimeoutError, KeyboardInterrupt):
                    states = dict.fromkeys(states, 'unknown')
                    finding = {'request': 'Request outcome unavailable', 'reply': 'Reply identities or outcomes uncertain',
                               'postcheck': 'Exact postcondition not established'}[stage]
                for key, status in states.items():
                    journal.append(key, status, 'No retry or automatic restore')
                result = {'mode': self.mode, 'plan_sha256': plan.sha256, 'outcomes': states,
                          'complete': all(status == 'accepted' for status in states.values()),
                          'request_count': self.provider.writer.writes if self.cloud else len(self.provider.requests),
                          'backups': topology, 'limits': dict(self.limits), 'finding': finding,
                          'replies_sha256': replies_sha, 'postcheck_sha256': postcheck_sha}
                self.publish('result.json', result)
                journal.append(None, 'completed' if result['complete'] else 'stopped', 'Result retained at every location')
                self.publish('complete.json', {'plan_sha256': plan.sha256, 'result_sha256': digest(result)})
                self.phase = 'completed' if result['complete'] else 'stopped'
                return result
            except (*ERRORS, KeyboardInterrupt):
                self.phase = 'blocked'
                raise


def read_events(outputs, plan):
    events = None
    for output in outputs:
        paths = sorted(safe_path(output / 'journal').iterdir())
        current, previous = [], None
        for number, path in enumerate(paths, 1):
            event = evidence.object_value(evidence.load_input(path)[0])
            if (path.name != f'{number:06d}.json' or event.get('sequence') != number
                    or event.get('previous_sha256') != previous or event.get('plan_sha256') != plan.sha256):
                raise Blocked('Cleanup journal is incomplete or changed; do not retry')
            previous = digest(event)
            current.append(event)
        if events is not None and events != current:
            raise Blocked('Cleanup journal copies differ; do not retry')
        events = current
    return events


def load_run(path):
    path = safe_path(path)
    envelope = evidence.object_value(evidence.load_input(path / 'plan.json')[0])
    plan = Plan(envelope['payload'], envelope['sha256'])
    data = plan.data()
    formats = {MODE: (1, LIMITS), CLOUD_MODE: (2, CLOUD_LIMITS)}
    if data.get('mode') not in formats or (data.get('schema'), data.get('limits')) != formats[data['mode']]:
        raise Blocked('Not a supported supervised cleanup record')
    outputs = tuple(safe_path(Path(p)) for p in data['outputs'])
    if len(outputs) != 3 or not any(os.path.samefile(path, output) for output in outputs):
        raise Blocked('Inspect a published cleanup directory')
    disjoint((*[safe_path(Path(p)) for p in data['roots']], *outputs))
    same_copies(outputs, 'plan.json', envelope)
    return plan, outputs


def inspect_run(path):
    """Read saved outcomes only. Never resume or issue a provider request."""
    plan, outputs = load_run(path)
    data = plan.data()
    events = read_events(outputs, plan)
    if not events:
        if any(os.path.lexists(output / name) for output in outputs for name in ('result.json', 'complete.json')):
            raise Blocked('Cleanup outcomes have no journal; do not retry')
        return {'state': 'NO_REQUEST_RECORDED', 'retry_allowed': False}
    same_copies(tuple(output.parent.parent for output in outputs), 'cleanup-started.json',
                {'plan_sha256': plan.sha256, 'outputs': data['outputs']})
    try:
        result = evidence.object_value(evidence.load_input(outputs[0] / 'result.json')[0])
        same_copies(outputs, 'result.json', result)
        same_copies(outputs, 'complete.json', {'plan_sha256': plan.sha256, 'result_sha256': digest(result)})
    except ERRORS:
        return {'state': 'UNRESOLVED; INSPECT MANUALLY', 'retry_allowed': False}
    targets = {op['record']['recordName'] for op in data['request']['operations']}
    outcomes = evidence.object_value(result.get('outcomes'))
    complete = all(status == 'accepted' for status in outcomes.values())
    for name in ('replies', 'postcheck'):
        expected_sha = result.get(name + '_sha256')
        if expected_sha is not None:
            value = evidence.load_input(outputs[0] / (name + '.json'))[0]
            same_copies(outputs, name + '.json', value)
            if digest(value) != expected_sha:
                raise Blocked('Cleanup observation digest changed')
    if (result.get('plan_sha256') != plan.sha256 or result.get('mode') != data['mode'] or result.get('limits') != data['limits']
            or type(result.get('request_count')) is not int or result['request_count'] not in (0, 1)
            or result['request_count'] == 0 and any(status != 'unknown' for status in outcomes.values())
            or result.get('complete') is not complete
            or result.get('backups') != data['backups'] or set(outcomes) != targets
            or any(status not in ('accepted', 'rejected', 'unknown') for status in outcomes.values())
            or len(events) != len(targets) + 2 or events[0].get('status') != 'attempted'
            or events[0].get('asset_id') is not None or events[-1].get('asset_id') is not None
            or events[-1]['status'] != ('completed' if complete else 'stopped')
            or {event.get('asset_id'): event.get('status') for event in events[1:-1]} != outcomes):
        raise Blocked('Cleanup completion is inconsistent; do not retry')
    return {'state': 'COMPLETED' if result['complete'] else 'STOPPED; INSPECT MANUALLY', 'retry_allowed': False,
            'accepted': sum(s == 'accepted' for s in result['outcomes'].values()),
            'rejected': sum(s == 'rejected' for s in result['outcomes'].values()),
            'unknown': sum(s == 'unknown' for s in result['outcomes'].values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--draft', type=Path, help='Rehearse a saved review using only in-memory cloud state')
    modes.add_argument('--inspect', type=Path, help='Read a retained cleanup directory without resuming')
    parser.add_argument('--simulate', action='store_true', help='Required with --draft; no cloud requests')
    parser.add_argument('--iarchive', type=Path, help='Require fresh OS-verified physical drives using this executable')
    args = parser.parse_args()
    old_mask = os.umask(0o077)
    cleanup = None
    try:
        if args.inspect:
            if args.iarchive or args.simulate:
                raise Blocked('Use --inspect alone')
            print(canonical(inspect_run(args.inspect)))
            return 0
        if not args.simulate:
            raise Blocked('Use --simulate with --draft; live execution is unavailable')
        session = ReviewDraft.reopen(args.draft)
        cleanup = Cleanup(session, MockRemoval(session.current.data()), Backups(args.iarchive))
        print('Offline cleanup rehearsal. Cloud state is an in-memory model.')
        print('Concurrent iCloud edits cannot be prevented. Asset tags do not enforce a version condition.')
        print('Physical drives: OS verification required.' if args.iarchive else 'Physical drives: simulated; NOT VERIFIED.')
        plan = cleanup.prepare()
        result = cleanup.confirm_and_execute()
        print('Rehearsal completed.' if result['complete'] else 'Rehearsal stopped. Inspect outcomes; never retry blindly.')
        return int(not result['complete'])
    except Cancelled:
        print('Cancelled. No mock removal request sent.')
        return 0
    except (*ERRORS, KeyboardInterrupt):
        print('Rehearsal stopped. Inspect retained records; no automatic retry.')
        return 1
    finally:
        if cleanup and cleanup.outputs:
            print(f'Cleanup records: {cleanup.outputs[0]}')
        os.umask(old_mask)


if __name__ == '__main__':
    raise SystemExit(main())
