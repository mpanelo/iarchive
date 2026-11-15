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


def same_copies(outputs, name, expected):
    for output in outputs:
        if canonical(evidence.load_input(safe_path(output) / name)[0]) != canonical(expected):
            raise Blocked('Cleanup records differ or are incomplete; do not retry')


class Cleanup:
    def __init__(self, session, provider, backups):
        if type(provider) is not MockRemoval or type(backups) is not Backups:
            raise Blocked('Only the offline removal model and bounded backup checker are supported')
        self.session, self.provider, self.backups = session, provider, backups
        self.gate = Preflight(session, provider.metadata)
        self.draft = self.gate.draft
        if not 0 < self.draft.data()['count'] <= MAX_TARGETS:
            raise Blocked('Select between one and 100 items for one rehearsal request')
        self.phase, self.outputs, self.plan = 'new', (), None

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
            preflight = self.gate.prepare()
            self.receipt = self.gate.finish(preflight)
            topology = self.backups.check(self.session)
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
            data = {'schema': 1, 'mode': MODE, 'limits': dict(LIMITS), 'draft_sha256': self.draft.sha256,
                    'count': draft['count'], 'kept': draft['kept'], 'request': request,
                    'preflight_sha256': preflight.sha256, 'backups': topology,
                    'outputs': list(map(str, self.outputs)), 'roots': list(map(str, self.session.roots)),
                    'created_at': datetime.now(timezone.utc).isoformat()}
            self.plan = Plan(canonical(data), digest(data))
            self.publish('plan.json', {'payload': self.plan.payload, 'sha256': self.plan.sha256})
            self.deadline = time.monotonic() + CHECK_SECONDS
            self.phase = 'prepared'
            return self.plan
        except (*ERRORS, KeyboardInterrupt):
            self.phase = 'blocked'
            raise

    def execute(self, plan):
        with self.session.lock:
            if self.phase != 'prepared':
                raise Blocked('This cleanup is closed; inspect its journal without retrying')
            self.phase = self.session.phase = 'executing'
            try:
                if type(plan) is not Plan or plan != self.plan:
                    raise Blocked('Cleanup plan changed')
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
                topology = self.backups.check(self.session)
                if canonical(topology) != canonical(data['backups']):
                    raise Blocked('Backup topology changed')
                if canonical(ordered(self.provider.metadata.snapshot())) != self.gate.expected_metadata:
                    raise Blocked('Cloud model metadata changed before the request')
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
                journal.append(None, 'attempted', 'One frozen mock request; intent flushed before execution')
                # Read all durable intent copies back before crossing the write boundary.
                read_events(self.outputs, plan)
                states = {key: 'unknown' for key in self.draft.data()['selected']}
                try:
                    stage = 'request'
                    replies = self.provider.remove(copy.deepcopy(data['request']))
                    stage = 'reply'
                    if (not isinstance(replies, list) or len(replies) != len(states)
                            or any(not isinstance(r, dict) or r.get('id') not in states for r in replies)
                            or len({r['id'] for r in replies}) != len(states)):
                        raise Blocked('Mock reply identities are uncertain')
                    for reply in replies:
                        if reply == {'id': reply['id'], 'status': 'accepted'}:
                            states[reply['id']] = 'accepted'
                        elif reply == {'id': reply['id'], 'error': 'CONFLICT'}:
                            states[reply['id']] = 'rejected'
                        else:
                            raise Blocked('Mock per-record reply is uncertain')
                    stage = 'postcheck'
                    done = {key for key, status in states.items() if status == 'accepted'}
                    expected = metadata_for(self.draft.data())
                    expected['assets'] = [a for a in expected['assets'] if a['id'] not in done]
                    trash = {a['id']: a for a in metadata_for(self.draft.data())['assets'] if a['id'] in done}
                    if (canonical(ordered(self.provider.metadata.snapshot())) != canonical(ordered(expected))
                            or self.provider.trash != trash):
                        raise Blocked('Accepted reply did not establish the exact mock postcondition')
                    finding = None
                except (*ERRORS, TimeoutError, KeyboardInterrupt):
                    states = dict.fromkeys(states, 'unknown')
                    finding = {'request': 'Request outcome unavailable', 'reply': 'Reply identities or outcomes uncertain',
                               'postcheck': 'Exact postcondition not established'}[stage]
                for key, status in states.items():
                    journal.append(key, status, 'Mock outcome only; no retry or automatic restore')
                result = {'mode': MODE, 'plan_sha256': plan.sha256, 'outcomes': states,
                          'complete': all(status == 'accepted' for status in states.values()),
                          'request_count': 1, 'backups': topology, 'limits': dict(LIMITS), 'finding': finding}
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


def inspect_run(path):
    """Read saved outcomes only. Never resume or issue a provider request."""
    path = safe_path(path)
    envelope = evidence.object_value(evidence.load_input(path / 'plan.json')[0])
    plan = Plan(envelope['payload'], envelope['sha256'])
    data = plan.data()
    if data.get('mode') != MODE or data.get('limits') != LIMITS or data.get('schema') != 1:
        raise Blocked('Not a supervised offline cleanup record')
    outputs = tuple(safe_path(Path(p)) for p in data['outputs'])
    if len(outputs) != 3 or not any(os.path.samefile(path, output) for output in outputs):
        raise Blocked('Inspect a published cleanup directory')
    disjoint((*[safe_path(Path(p)) for p in data['roots']], *outputs))
    same_copies(outputs, 'plan.json', envelope)
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
    if (result.get('plan_sha256') != plan.sha256 or result.get('mode') != MODE or result.get('limits') != LIMITS
            or result.get('request_count') != 1 or result.get('complete') is not complete
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
        print(f"Selected: {plan.data()['count']} items. Kept: {len(plan.data()['kept'])} items.")
        print('One mock soft-delete request; local copies are preserved.')
        confirm('Simulate this selection?')
        result = cleanup.execute(plan)
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
