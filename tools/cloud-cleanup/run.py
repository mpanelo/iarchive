"""Guided, opt-in cleanup or read-only recovery from a saved review."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from cloud_metadata import require_provider
from cloud_workflow import CloudCleanup, recover_run, recovery_input
from conflict import Pacer, read_guard
from prepare_review import ReviewDraft
from probe import (PIN, authenticated_account, discover_zones, new_run, reuse_cookies,
                   save_new, verify_second_factor)
from prompts import Cancelled, confirm
from prototype import Blocked, canonical
from supervised import Backups, Cleanup, MAX_TARGETS


def ask_path(question, value):
    if value is not None:
        return value
    try:
        answer = input(question + ' (Enter cancels): ').strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    if not answer:
        raise Cancelled
    if len(answer) >= 2 and answer[0] == answer[-1] and answer[0] in ('"', "'"):
        answer = answer[1:-1]
    return Path(answer)


@contextmanager
def account_session(draft, *, recovery=False, reuse_session=None):
    """Separate from read-only preflight auth: writes may occur after yield."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Blocked('Use an interactive Terminal for account access and confirmation')
    require_provider()
    if recovery:
        print('Read-only recovery: look up the attempted target and master IDs. No writes or media downloads.')
    else:
        print('Cleanup moves only the saved selection to Recently Deleted on synced devices.')
        print('Concurrent iCloud edits cannot be prevented. No purge or automatic retry is available.')
    confirm('Sign in for read-only recovery?' if recovery else 'Sign in for this cleanup?')
    old_logging = logging.root.manager.disable
    service, run = None, None
    summary = {'schema': 1, 'provider_version': PIN, 'authenticated': False,
               'scope': 'read_only_cleanup_recovery' if recovery else 'supervised_cleanup',
               'authentication_attempted': False, 'status': 'stopped', 'modification_requests': 0,
               'started_at': datetime.now(timezone.utc).isoformat()}
    pacer = Pacer()
    try:
        logging.disable(logging.CRITICAL)
        from pyicloud_ipd import PyiCloudService
        repo = Path(__file__).resolve().parents[2]
        run = new_run(repo)
        print(f'Private account-session output: {run}')
        cookies = reuse_cookies(repo, reuse_session) if reuse_session else run / 'cookies'
        with read_guard(pacer):
            account = getpass.getpass('Apple account email (hidden, local only): ').strip()
            if not account:
                raise Cancelled
            summary['authentication_attempted'] = True
            service = PyiCloudService('com', account, lambda: getpass.getpass('Apple password (local only): '),
                                     cookie_directory=str(cookies), with_family=False, http_timeout=30)
            if service.requires_2fa:
                verify_second_factor(service)
            if service.requires_2sa or authenticated_account(service) != draft['scope']['account_id']:
                raise Blocked('Account does not match the saved selection')
            root = service._get_webservice_url('ckdatabasews')
            summary['authenticated'] = True
        with read_guard(pacer, service, draft['scope']) as state:
            state['photo_root'] = root
            zones, errors = discover_zones(service, root)
            if errors or set(zones) != {'private', 'shared'}:
                raise Blocked('Library discovery was incomplete')
        yield SimpleNamespace(service=service, root=root, zones=zones, pacer=pacer, summary=summary)
    except Cancelled:
        summary['status'] = 'cancelled'
        raise
    except Blocked:
        raise
    except (Exception, KeyboardInterrupt):
        raise Blocked('Cloud operation stopped. Inspect the retained cleanup records before any further action') from None
    finally:
        try:
            if service is not None:
                service.session.close()
        finally:
            try:
                summary['finished_at'] = datetime.now(timezone.utc).isoformat()
                summary['rate_limited'] = pacer.pause is not None
                if pacer.pause is not None:
                    summary['retry_after_seconds'] = pacer.pause.retry_after_seconds
                if run is not None:
                    save_new(run / 'summary.json', summary)
            finally:
                logging.disable(old_logging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument('--draft', type=Path)
    choices.add_argument('--recover', action='store_true')
    parser.add_argument('--run', type=Path, help='Attempted cleanup directory for read-only recovery')
    parser.add_argument('--album', help='Exact album name for cleanup; never used for recovery')
    parser.add_argument('--iarchive', type=Path)
    parser.add_argument('--reuse-session', help='Previously authenticated private cloud-probe run name')
    args = parser.parse_args()
    old_mask = os.umask(0o077)
    flow, connected = None, None
    try:
        recovery = args.recover
        if recovery:
            if args.album is not None or args.iarchive is not None:
                raise Blocked('Recovery uses the saved request only; omit --album and --iarchive')
            path = ask_path('Attempted cleanup directory', args.run)
            _, _, draft = recovery_input(path)
        else:
            if args.run is not None:
                raise Blocked('Use --run only with --recover')
            path = ask_path('Saved draft.json path', args.draft)
            review = ReviewDraft.reopen(path)
            draft = review.current
            if not 1 <= draft.data()['count'] <= MAX_TARGETS:
                raise Blocked('Select between one and 100 items before starting cleanup')
            repo = Path(__file__).resolve().parents[2]
            executable = args.iarchive or repo / 'target' / 'release' / ('iarchive.exe' if os.name == 'nt' else 'iarchive')
            backups = Backups(executable)
            proof = backups.check(review)
            if proof['physical_drives'] != 'VERIFIED BY OS TOPOLOGY':
                raise Blocked('Two physical drives must be verified before account access')
            print('Fresh local checks passed on two OS-verified physical drives.')
            album = args.album
            if album is None:
                try:
                    album = input('Exact iCloud album name (Enter cancels): ')
                except (EOFError, KeyboardInterrupt):
                    raise Cancelled from None
            if not album.strip():
                raise Cancelled
        with account_session(draft.data(), recovery=recovery, reuse_session=args.reuse_session) as connected:
            try:
                if recovery:
                    result = recover_run(path, connected.service, connected.root, connected.zones, connected.pacer)
                    connected.summary.update(status='read_only_recovery_recorded', lookup_requests=1)
                    print('Recovery observations saved beside all three cleanup records. Original outcomes are unchanged.')
                    print(f"Recovery records: {result['outputs'][0]}")
                    print('No removal or retry was attempted. Inspect the observations and Photos manually.')
                    return 0
                # Reopen after authentication: intervening selection/evidence edits must fail.
                review = ReviewDraft.reopen(path)
                if review.current != draft:
                    raise Blocked('The saved selection changed during account access')
                provider = CloudCleanup(connected.service, connected.root, connected.zones, album, draft, connected.pacer)
                flow = Cleanup(review, provider, backups)
                flow.prepare()
                result = flow.confirm_and_execute()
                connected.summary['status'] = 'completed' if result['complete'] else 'stopped'
                print('Cleanup post-checks passed. Both local copies are preserved.' if result['complete'] else
                      'Cleanup stopped with unresolved or rejected items. Use read-only recovery; never retry this draft.')
                return int(not result['complete'])
            finally:
                if flow is not None:
                    connected.summary.update(modification_requests=flow.provider.writer.writes,
                                             lookup_requests=flow.provider.writer.lookups)
        return 1
    except Cancelled:
        print('Cancelled. No removal request was sent.')
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if args.recover:
            print('Read-only recovery stopped. Original outcomes are unchanged; inspect the retained records.')
        elif connected is not None and connected.summary['modification_requests']:
            print('Stopped. Cloud changes may have occurred. Inspect the saved records and use read-only recovery; do not retry.')
        else:
            print('Stopped before removal. Check the saved draft, both drives and account access.')
        if isinstance(error, Blocked):
            print(str(error))
        return 1
    finally:
        if flow is not None and flow.outputs:
            print(f'Cleanup records: {flow.outputs[0]}')
        os.umask(old_mask)


if __name__ == '__main__':
    raise SystemExit(main())
