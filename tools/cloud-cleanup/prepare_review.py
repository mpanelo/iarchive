"""Prepare local review drafts from reconciled evidence. No cloud operations."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-prototype'))
import reconcile as evidence
from prototype import Blocked, Plan, canonical, digest, disjoint, file_set, hash_file, media_path, safe_path, write_new


MODE = 'LOCAL_CLOUD_REVIEW_DRAFT'
LIMITS = {'live_execution': 'UNAVAILABLE', 'removal_approved': False,
          'cloud_coverage': 'NOT VERIFIED', 'physical_drive_independence': 'NOT VERIFIED',
          'content_relationships': 'NOT VERIFIED'}
ERRORS = (Blocked, OSError, ValueError, TypeError, KeyError, IndexError, RecursionError)


class ReviewDraft:
    """Selections are notes bound to exact evidence, never executable plans."""
    review_draft = True

    def __init__(self, reconciliation, resource_report, download_manifest, primary_prefix='copy-a', secondary_prefix='copy-b'):
        self.lock = threading.RLock()
        self.inputs = {}
        self.selected = ()
        self.revision = 0
        self.current = None
        self.phase = 'reviewing'
        self.checked_at = None

        def read(path, jsonl=False):
            path = safe_path(path)
            value, sha = evidence.load_input(path, jsonl=jsonl)
            self.inputs[str(path)] = (sha, jsonl)
            return value if jsonl else evidence.object_value(value)

        report = read(reconciliation)
        observation, downloads = read(resource_report), read(download_manifest)
        for key in ('verification', 'limits', 'input_sha256'):
            evidence.object_value(report.get(key))
        if (report.get('schema') != 1 or report.get('mode') != evidence.FRESH_MODE
                or report.get('findings') != [] or report.get('verification', {}).get('attempted') is not True
                or report.get('limits', {}).get('fresh_local_bytes') != 'VERIFIED FOR MATCHED RESOURCES'
                or report.get('limits', {}).get('removal_approved') is not False):
            raise Blocked('A complete fresh reconciliation is required')
        bundles = report['verification'].get('artifact_bundles')
        if not isinstance(bundles, list) or len(bundles) != 3:
            raise Blocked('Three verification bundles are required')
        self.bundles = tuple(safe_path(Path(evidence.identity(path))) for path in bundles)
        replica = None
        for bundle in self.bundles:
            value = read(bundle / 'manifest.jsonl', jsonl=True)
            if self.inputs[str(bundle / 'manifest.jsonl')][0] != report['input_sha256']['replica_manifest']:
                raise Blocked('The reconciled replica manifest changed')
            replica = value
        for key, path in (('resource_report', resource_report), ('download_manifest', download_manifest)):
            if self.inputs[str(safe_path(path))][0] != report['input_sha256'][key]:
                raise Blocked('Reconciliation input changed')
        expected = evidence.reconcile(observation, downloads, replica, primary_prefix, secondary_prefix)
        if (expected['findings'] or any(expected[key] != report.get(key) for key in
                ('archive_id', 'recorded_replica_run', 'matches', 'summary'))
                or report['verification'].get('run_id') != replica[0]['run_id']):
            raise Blocked('Reconciliation is incomplete or does not match its evidence')
        header, *self.files = replica
        self.roots = tuple(safe_path(Path(evidence.identity(header[key]))) for key in ('source_root', 'secondary_root'))
        disjoint((*self.roots, *self.bundles))
        if any(path == root or root in path.parents for path in map(Path, self.inputs) for root in self.roots):
            raise Blocked('Review evidence must stay outside media')
        self.identities = self.root_identities()
        self.recorded_drives = header['independent_physical_drives']
        claimed_drives = 'VERIFIED BY OS TOPOLOGY' if evidence.independent(header['source_identity'], header['secondary_identity']) else 'NOT VERIFIED'
        if (self.recorded_drives != claimed_drives
                or report['verification'].get('independent_physical_drives') != claimed_drives
                or report['limits'].get('physical_drive_independence') != claimed_drives):
            raise Blocked('Recorded physical topology is inconsistent')
        self.scope = {key: observation[key] for key in ('account_id', 'database', 'zone_id', 'album_id')}
        self.evidence = {key: str(safe_path(path)) for key, path in (
            ('reconciliation', reconciliation), ('resource_report', resource_report), ('download_manifest', download_manifest))}
        self.evidence.update(primary_prefix=primary_prefix, secondary_prefix=secondary_prefix)
        matches = {(m['asset_id'], m['resource_record_type'], m['resource_record_id'], m['resource_field']): m
                   for m in expected['matches']}
        self.assets = []
        for asset in observation['assets']:
            resources = []
            for index in asset['resource_indices']:
                resource = observation['resources'][index]
                match = matches[(asset['asset_id'], resource['record_type'], resource['record_id'], resource['resource_field'])]
                resources.append({**match, 'role': evidence.identity(resource['role']), 'size': match['byte_count'],
                                  'owner_change_tag': resource['record_change_tag'], 'file_type': resource['file_type'],
                                  'provider_checksum_opaque': resource['provider_checksum_opaque']})
            self.assets.append({'id': asset['asset_id'], 'master_id': asset['master_id'],
                                'asset_change_tag': asset['asset_change_tag'], 'master_change_tag': asset['master_change_tag'],
                                'kind': 'Cloud item', 'resources': resources})
        self.check()

    @classmethod
    def reopen(cls, path):
        path = safe_path(path)
        envelope = evidence.object_value(evidence.load_input(path)[0])
        if set(envelope) != {'payload', 'sha256'} or not isinstance(envelope['payload'], str):
            raise Blocked('Saved draft envelope is invalid')
        plan = Plan(envelope['payload'], evidence.sha256(envelope['sha256']))
        data = evidence.object_value(plan.data())
        if (type(data.get('schema')) is not int or data['schema'] != 1 or data.get('mode') != MODE
                or canonical(data.get('limits')) != canonical(LIMITS)):
            raise Blocked('Saved draft format or limits changed')
        source = data.get('evidence')
        keys = ('reconciliation', 'resource_report', 'download_manifest')
        if not isinstance(source, dict) or set(source) != {*keys, 'primary_prefix', 'secondary_prefix'}:
            raise Blocked('Saved draft lacks reopen metadata; prepare a new review')
        input_hashes = evidence.object_value(data.get('input_sha256'))
        # Bind the reports before using their paths to discover media roots.
        for key in keys:
            report = safe_path(Path(evidence.identity(source[key])))
            if evidence.load_input(report)[1] != input_hashes.get(str(report)):
                raise Blocked('Saved draft evidence changed; prepare a new review')
        session = cls(*(Path(source[key]) for key in keys), source['primary_prefix'], source['secondary_prefix'])
        selected = data.get('selected')
        allowed = {asset['id'] for asset in session.assets}
        if (not isinstance(selected, list) or any(type(key) is not str for key in selected)
                or len(selected) != len(set(selected)) or selected != sorted(selected) or not set(selected) <= allowed
                or type(data.get('revision')) is not int or data['revision'] < 0):
            raise Blocked('Saved draft selection or revision is invalid')
        expected = {'scope': session.scope, 'assets': session.assets, 'evidence': session.evidence,
                    'input_sha256': {path: sha for path, (sha, _) in session.inputs.items()},
                    'kept': [a['id'] for a in session.assets if a['id'] not in selected], 'count': len(selected),
                    'bytes': sum(r['size'] for a in session.assets if a['id'] in selected for r in a['resources']),
                    'recorded_physical_drives': session.recorded_drives}
        if any(canonical(data.get(key)) != canonical(value) for key, value in expected.items()):
            raise Blocked('Saved draft no longer matches its review evidence')
        if canonical(data.get('root_identities')) != canonical(session.identities):
            raise Blocked('Saved media root identities changed')
        session.check_saved(plan)
        if path.name != 'draft.json' or not any(os.path.samefile(path.parent, Path(output)) for output in data['outputs']):
            raise Blocked('Saved draft must be reopened from a published copy')
        session.check_inputs()
        session.selected, session.revision, session.current = tuple(selected), data['revision'], plan
        return session

    def root_identities(self):
        result = []
        for root in self.roots:
            info = safe_path(root).stat()
            if not root.is_dir():
                raise Blocked('A media root is unavailable')
            result.append((info.st_dev, info.st_ino))
        return tuple(result)

    def check_inputs(self):
        for path, (sha, jsonl) in self.inputs.items():
            if evidence.load_input(Path(path), jsonl=jsonl)[1] != sha:
                raise Blocked('Review evidence changed; prepare a new review')
        if self.root_identities() != self.identities:
            raise Blocked('A media root changed; prepare a new review')
        for bundle in self.bundles:
            safe_path(bundle)

    def check(self):
        self.check_inputs()
        paths = {item['relative_path'] for item in self.files}
        for root in self.roots:
            if file_set(root) != paths:
                raise Blocked('Replica file set changed; prepare a new review')
            for item in self.files:
                if hash_file(media_path(root, item['relative_path'])) != (item['byte_count'], item['source_sha256']):
                    raise Blocked('Replica bytes changed; prepare a new review')
            if file_set(root) != paths:
                raise Blocked('Replica file set changed during review checks')
        self.check_inputs()
        self.checked_at = datetime.now(timezone.utc).isoformat()

    def stop(self):
        self.current = None
        self.phase = 'stopped'

    def check_saved(self, plan):
        outputs = plan.data().get('outputs')
        if not isinstance(outputs, list) or len(outputs) != 3:
            raise Blocked('Saved draft needs three published locations')
        names = set()
        try:
            for output, bundle in zip(outputs, self.bundles, strict=True):
                path = safe_path(Path(evidence.identity(output)))
                if path.parent.name != 'reviews' or not os.path.samefile(path.parent.parent, bundle):
                    raise Blocked('Saved draft locations do not match its verification bundles')
                names.add(path.name)
                if os.path.lexists(path / 'superseded.json'):
                    raise Blocked('Saved draft was superseded; use the newer draft')
                if (evidence.load_input(path / 'draft.json')[0] != {'payload': plan.payload, 'sha256': plan.sha256}
                        or evidence.load_input(path / 'complete.json')[0] != {'draft_sha256': plan.sha256}):
                    raise Blocked('A saved draft changed')
        except OSError:
            raise Blocked('Saved draft copies are missing or unsafe') from None
        if len(names) != 1:
            raise Blocked('Saved draft copies refer to different revisions')

    def selected_bytes(self):
        return sum(r['size'] for a in self.assets if a['id'] in self.selected for r in a['resources'])

    def editable(self, revision):
        if self.phase != 'reviewing':
            raise Blocked('This review is closed; prepare a new review')
        if type(revision) is not int or revision != self.revision:
            raise Blocked('Selection changed in another request; refresh the page')

    def view(self):
        return {'review_draft': True, 'snapshot': {'active': self.assets},
                'targets': [a for a in self.assets if a['id'] in self.selected],
                'roots': list(map(str, self.roots)), 'count': len(self.selected),
                'verification': self.verification()}

    def verification(self):
        return {'Last local byte check': self.checked_at,
                'Physical drives at recorded check': self.recorded_drives,
                'Current physical-drive independence': 'NOT VERIFIED',
                'Cloud coverage': 'NOT VERIFIED', 'Cloud removal': 'UNAVAILABLE'}

    def state(self):
        from review import cards
        with self.lock:
            if self.phase == 'reviewing':
                try:
                    self.check_inputs()
                    if self.current is not None:
                        self.check_saved(self.current)
                except ERRORS:
                    self.stop()
            return {'mode': 'review_draft', 'revision': self.revision, 'selected': list(self.selected),
                    'count': len(self.selected), 'total': len(self.assets),
                    'bytes': self.selected_bytes(),
                    'saved': self.current is not None, 'phase': self.phase, 'confirmation': None,
                    'draft_digest': self.current.sha256 if self.current else None,
                    'cards': cards(self.view()), 'roots': list(map(str, self.roots)),
                    'verification': self.verification()}

    def draft(self, revision, selected):
        with self.lock:
            self.editable(revision)
            allowed = {a['id'] for a in self.assets}
            if (not isinstance(selected, list) or any(type(key) is not str for key in selected)
                    or len(selected) != len(set(selected)) or not set(selected) <= allowed):
                raise Blocked('Selection must contain unique full item IDs from this review')
            try:
                self.check_inputs()
                if self.current is not None:
                    self.check_saved(self.current)
                if tuple(sorted(selected)) != self.selected:
                    previous, self.current = self.current, None
                    if previous is not None:
                        for output in previous.data()['outputs']:
                            write_new(safe_path(Path(output)) / 'superseded.json', canonical({'draft_sha256': previous.sha256}))
                    self.selected = tuple(sorted(selected))
                    self.revision += 1
            except ERRORS:
                self.stop()
                raise Blocked('Evidence changed or the old draft could not be retired; review stopped')
            return self.state()

    def save(self, revision):
        from review import render
        with self.lock:
            self.editable(revision)
            try:
                self.check()
                if self.current is not None:
                    self.check_saved(self.current)
                    return self.state()
                name, outputs = uuid.uuid4().hex, []
                for bundle in self.bundles:
                    parent = bundle / 'reviews'
                    parent.mkdir(exist_ok=True)
                    safe_path(parent)
                    output = parent / name
                    output.mkdir()
                    outputs.append(str(output))
                data = {'schema': 1, 'mode': MODE, 'scope': self.scope, 'limits': dict(LIMITS),
                        'created_at': self.checked_at, 'revision': self.revision,
                        'selected': list(self.selected), 'kept': [a['id'] for a in self.assets if a['id'] not in self.selected],
                        'assets': self.assets, 'count': len(self.selected), 'outputs': outputs,
                        'input_sha256': {path: sha for path, (sha, _) in self.inputs.items()},
                        'bytes': self.selected_bytes(), 'recorded_physical_drives': self.recorded_drives,
                        'evidence': self.evidence, 'root_identities': self.identities}
                plan = Plan(canonical(data), digest(data))
                html = render(self.view(), plan.sha256)
                for output in map(Path, outputs):
                    plan.save(output / 'draft.json')
                    write_new(output / 'review.html', html)
                for output in map(Path, outputs):
                    write_new(output / 'complete.json', canonical({'draft_sha256': plan.sha256}))
                self.check_inputs()
                self.check_saved(plan)
                self.current = plan
            except ERRORS:
                self.stop()
                raise Blocked('Draft could not be verified and saved at every location; review stopped')
            return self.state()

    def close(self):
        with self.lock:
            if self.phase == 'reviewing':
                self.phase = 'closed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', type=Path, help='Reopen a published draft.json after checking all copies and media')
    parser.add_argument('--reconciliation', type=Path)
    parser.add_argument('--resource-report', type=Path)
    parser.add_argument('--download-manifest', type=Path)
    parser.add_argument('--primary-prefix')
    parser.add_argument('--secondary-prefix')
    args = parser.parse_args()
    session = None
    try:
        paths = (args.reconciliation, args.resource_report, args.download_manifest)
        if args.resume:
            if any(value is not None for value in (*paths, args.primary_prefix, args.secondary_prefix)):
                raise Blocked('Use --resume alone; saved evidence cannot be overridden')
            session = ReviewDraft.reopen(args.resume)
        else:
            if any(path is None for path in paths):
                raise Blocked('Supply all three evidence paths or --resume')
            session = ReviewDraft(*paths, args.primary_prefix or 'copy-a', args.secondary_prefix or 'copy-b')
        from review import serve
        with serve(session) as url:
            print(f'Open the review: {url}', flush=True)
            print('Save a review draft in the page. Nothing will be removed. Press Enter here to close.', flush=True)
            input()
        if session.current is not None:
            path = Path(session.current.data()['outputs'][0]) / 'draft.json'
            command = [sys.executable, str(Path(__file__).resolve()), '--resume', str(path)]
            reopen = '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in command) if os.name == 'nt' else shlex.join(command)
            print(f'Saved draft: {path}\nReopen with: {reopen}', flush=True)
        return int(session.phase == 'stopped')
    except ERRORS:
        print('Review could not use complete, unchanged evidence. Prepare fresh reconciliation and try again.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    raise SystemExit(main())
