"""Compare a saved review draft with fresh local bytes and read-only metadata."""

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

from prepare_review import ERRORS, ReviewDraft, evidence
from prototype import Blocked, Plan, canonical, digest, safe_path, write_new
from cloud_metadata import CloudMetadata, SOURCE, authenticate


MODE = 'SIMULATED_REVIEW_PREFLIGHT'
CONFIRM_SECONDS = 120
LIMITS = {'live_execution': 'UNAVAILABLE', 'removal_approved': False,
          'cloud_coverage': 'NOT VERIFIED', 'physical_drive_independence': 'NOT VERIFIED',
          'content_relationships': 'NOT VERIFIED', 'server_version_condition': 'NOT VERIFIED'}


def metadata_for(draft, source='IN_MEMORY_MOCK'):
    """Model provider fields independently from local paths and SHA-256 values."""
    resources = ('resource_record_type', 'resource_record_id', 'resource_field',
                 'owner_change_tag', 'file_type', 'size', 'provider_checksum_opaque')
    assets = []
    for asset in draft['assets']:
        value = {key: asset[key] for key in ('id', 'master_id', 'asset_change_tag', 'master_change_tag')}
        value['resources'] = [{key: resource[key] for key in resources} for resource in asset['resources']]
        assets.append(value)
    return {'source': source, 'scope': draft['scope'], 'assets': assets, 'complete': True}


def ordered(snapshot):
    value = json.loads(canonical(evidence.object_value(snapshot)))
    value['assets'] = evidence.records(value.get('assets'))
    value['assets'].sort(key=lambda asset: evidence.identity(asset.get('id')))
    for asset in value['assets']:
        asset['resources'] = evidence.records(asset.get('resources'))
        asset['resources'].sort(key=lambda resource: tuple(evidence.identity(resource.get(key)) for key in
                                  ('resource_record_type', 'resource_record_id', 'resource_field')))
    return value


class MockMetadata:
    """Mutable metadata fixture only; it has no authentication or write API."""

    def __init__(self, draft):
        self.current = metadata_for(draft)
        self.reads = 0

    def snapshot(self):
        self.reads += 1
        return json.loads(canonical(self.current))


class Preflight:
    def __init__(self, session, provider):
        if type(provider) not in (MockMetadata, CloudMetadata):
            raise Blocked('Use the in-memory metadata mock or the bounded read-only adapter')
        if type(session) is not ReviewDraft or session.current is None:
            raise Blocked('Preflight requires an exact saved review draft')
        self.session, self.provider, self.draft = session, provider, session.current
        if not self.draft.data()['selected']:
            raise Blocked('Nothing is selected; a keep-all draft needs no confirmation')
        self.cloud = type(provider) is CloudMetadata
        self.mode = 'READ_ONLY_REVIEW_PREFLIGHT' if self.cloud else MODE
        self.expected_metadata = canonical(ordered(metadata_for(self.draft.data(), SOURCE if self.cloud else 'IN_MEMORY_MOCK')))
        self.phase, self.plan, self.deadline = 'new', None, None
        self.outputs = ()

    def check_selection(self):
        data = self.draft.data()
        if (self.session.current != self.draft or list(self.session.selected) != data['selected']
                or self.session.revision != data['revision']):
            raise Blocked('Review selection changed; prepare a new preflight')
        self.session.check_inputs()
        self.session.check_saved(self.draft)

    def refresh(self):
        self.check_selection()
        source = Path(self.draft.data()['outputs'][0]) / 'draft.json'
        current = ReviewDraft.reopen(source)
        if current.current != self.draft:
            raise Blocked('Saved review changed; prepare a new preflight')
        if type(self.provider) is not (CloudMetadata if self.cloud else MockMetadata):
            raise Blocked('Preflight metadata adapter changed')
        try:
            observed = canonical(ordered(self.provider.snapshot()))
        except ERRORS:
            raise Blocked('Provider metadata is unavailable or malformed') from None
        if observed != self.expected_metadata:
            raise Blocked(('Provider' if self.cloud else 'Mock') + ' metadata changed or is incomplete')
        self.check_selection()

    def publish(self, name, value):
        for output in self.outputs:
            write_new(safe_path(output) / name, canonical(value))

    def check_copies(self, name, expected):
        for output in self.outputs:
            if evidence.load_input(safe_path(output) / name)[0] != expected:
                raise Blocked('Preflight ' + name + ' changed')

    def unexpired(self):
        if time.monotonic() >= self.deadline:
            raise Blocked('Preflight confirmation expired; prepare a new preflight')

    def prepare(self):
        if self.phase != 'new':
            raise Blocked('This preflight has already started; prepare a new one')
        self.phase = 'preparing'
        try:
            self.refresh()
            data = self.draft.data()
            name, outputs = uuid.uuid4().hex, []
            for draft_output in data['outputs']:
                parent = safe_path(Path(draft_output)) / 'preflight'
                parent.mkdir(exist_ok=True)
                safe_path(parent)
                output = parent / name
                output.mkdir()
                outputs.append(output)
            self.outputs = tuple(outputs)
            now = datetime.now(timezone.utc)
            self.deadline = time.monotonic() + CONFIRM_SECONDS
            payload = {'schema': 1, 'mode': self.mode, 'limits': dict(LIMITS), 'nonce': name,
                       'draft_sha256': self.draft.sha256, 'scope': data['scope'],
                       'targets': [asset for asset in data['assets'] if asset['id'] in data['selected']],
                       'kept': data['kept'], 'count': data['count'], 'bytes': data['bytes'],
                       'input_sha256': data['input_sha256'], 'root_identities': data['root_identities'],
                       'metadata_sha256': digest(json.loads(self.expected_metadata)),
                       'created_at': now.isoformat(), 'expires_at': (now + timedelta(seconds=CONFIRM_SECONDS)).isoformat(),
                       'outputs': list(map(str, self.outputs))}
            self.plan = Plan(canonical(payload), digest(payload))
            self.publish('plan.json', {'payload': self.plan.payload, 'sha256': self.plan.sha256})
            self.check_selection()
            self.unexpired()
            self.phase = 'prepared'
            return self.phrase()
        except (*ERRORS, KeyboardInterrupt):
            self.phase = 'blocked'
            raise

    def phrase(self):
        if self.phase != 'prepared':
            raise Blocked('No current preflight confirmation is available')
        prefix = 'CONFIRM READ ONLY PREFLIGHT' if self.cloud else 'SIMULATE PREFLIGHT'
        return f"{prefix} {self.plan.data()['count']} {self.plan.sha256}"

    def confirm(self, answer):
        if self.phase != 'prepared':
            raise Blocked('This preflight is closed; confirmations cannot be reused')
        expected_phrase = self.phrase()
        self.phase = 'checking'
        try:
            self.unexpired()
            if answer != expected_phrase:
                raise Blocked('Exact preflight count and full digest must be confirmed')
            envelope = {'payload': self.plan.payload, 'sha256': self.plan.sha256}
            self.check_copies('plan.json', envelope)
            if any(os.path.lexists(output / name) for output in self.outputs
                   for name in ('intent.json', 'receipt.json', 'complete.json')):
                raise Blocked('This preflight was already used; never reuse its confirmation')
            self.refresh()
            self.unexpired()
            intent = {'plan_sha256': self.plan.sha256, 'draft_sha256': self.draft.sha256,
                      'confirmed_at': datetime.now(timezone.utc).isoformat(), 'mode': self.mode}
            self.publish('intent.json', intent)
            self.check_selection()
            self.check_copies('plan.json', envelope)
            self.check_copies('intent.json', intent)
            self.unexpired()
            receipt = {**intent, 'count': self.plan.data()['count'], 'bytes': self.plan.data()['bytes'],
                       'result': 'READ_ONLY_CHECK_CONFIRMED' if self.cloud else 'SIMULATION_CONFIRMED',
                       'local_bytes': 'FRESHLY HASHED FOR THIS CHECK',
                       'provider_metadata': 'OBSERVED ICLOUD METADATA MATCHED' if self.cloud else 'MOCK MATCHED', 'limits': dict(LIMITS)}
            self.publish('receipt.json', receipt)
            completion = {'plan_sha256': self.plan.sha256, 'receipt_sha256': digest(receipt)}
            self.publish('complete.json', completion)
            self.check_copies('receipt.json', receipt)
            self.check_copies('complete.json', completion)
            self.check_selection()
            self.phase = 'confirmed'
            return receipt
        except (*ERRORS, KeyboardInterrupt):
            self.phase = 'blocked'
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--draft', required=True, type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--simulate', action='store_true', help='Use in-memory metadata only')
    modes.add_argument('--read-only-cloud', action='store_true', help='Explicitly authenticate for bounded metadata reads')
    parser.add_argument('--album', help='Exact album name, required with --read-only-cloud')
    parser.add_argument('--reuse-session', help='Previously authenticated private cloud-probe run name')
    args = parser.parse_args()
    try:
        session = ReviewDraft.reopen(args.draft)
        if (args.read_only_cloud and not args.album) or (args.simulate and (args.album or args.reuse_session)):
            raise Blocked('Use --album with --read-only-cloud only')
        data = session.current.data()
        if not data['selected']:
            raise Blocked('Nothing is selected; cloud authentication is unnecessary')
        context = authenticate(data, args.album, args.reuse_session) if args.read_only_cloud else nullcontext(MockMetadata(data))
        with context as provider:
            gate = Preflight(session, provider)
            phrase = gate.prepare()
            print('Read-only iCloud preflight. Cloud removal is unavailable.' if gate.cloud else
                  'Offline preflight rehearsal. Provider metadata is an in-memory model; cloud removal is unavailable.')
            print(f"Selected: {gate.plan.data()['count']} items; {gate.plan.data()['bytes']:,} resource bytes.")
            print('Confirmation expires after two minutes. Nothing will be removed.')
            answer = input(f'Type {phrase}: ').strip()
            if not answer:
                gate.phase = 'cancelled'
                print('Cancelled. No confirmation receipt was saved.')
                return 0
            result = gate.confirm(answer)
            print('Preflight check confirmed. Receipts saved in all three locations:')
            for output in gate.outputs:
                print(output)
            print(json.dumps(result, sort_keys=True))
            return 0
    except (*ERRORS, KeyboardInterrupt, EOFError):
        print('Preflight stopped. Check the draft, evidence and copies; start a new preflight before confirming again.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
