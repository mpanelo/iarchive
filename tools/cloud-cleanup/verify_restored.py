"""Check one restored synthetic JPEG; live reads require separate approval."""

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import sys

import trial
from conflict import ConflictBlocked, Pacer, canonical, read_guard, read_report
from changes import ChangesThrottled, text, valid_zone
from originals import snapshot
from probe import (PIN, ProbeBlocked, authenticated_account, discover_zones, new_run,
                   reuse_cookies, save_new, verify_second_factor)


SCOPE_PHRASE = "READ ONE RESTORED JPEG"
REPORTS = ("summary.json", "offline-plan-private.json", "remove-plan-private.json",
           "write-intent-private.json", "write-outcome-private.json",
           "before-album-private.json", "restored-album-private.json")


class ReadOnlyWire(trial.TrialWire):
    def __init__(self, *args):
        super().__init__(*args, metadata_only=True)

    def freeze_removal(self, *_args):
        raise trial.TrialBlocked("Removal is forbidden in the restored-byte check")

    def send(self, kind):
        if kind != "lookup" or self.lookups >= 1:
            raise trial.TrialBlocked("Only one read-only asset/master lookup is allowed")
        return super().send(kind)


def same_capture(before, after):
    return (all(canonical(before[key]) == canonical(after[key])
                for key in ("account_id", "database", "zone_id", "album_id"))
            and canonical(snapshot(before["passes"][-1])) == canonical(snapshot(after["passes"][-1])))


def load_trial(repo, name):
    if not re.fullmatch(r"cloud-probe-[a-z0-9_]+", name):
        raise trial.TrialBlocked("Unsafe completed-trial run name")
    directory = repo / ".smoke" / name
    reports, hashes = {}, {}
    for filename in REPORTS:
        reports[filename], hashes[filename] = read_report(repo, directory / filename)
    summary = reports["summary.json"]
    required = ("original_sha256_verified", "removal_metadata_observed",
                "restored_album_membership_verified", "restored_resource_metadata_matches",
                "preserved_originals_unchanged")
    if (summary.get("schema") != 1 or summary.get("provider_version") != PIN
            or summary.get("scope") != "one_disposable_cleanup_trial"
            or summary.get("phase") != "completed" or "blocked" in summary
            or summary.get("media_gets") != 1 or summary.get("modification_requests") != 1
            or summary.get("recently_deleted_ui") != "USER REPORTED"
            or any(summary.get(key) is not True for key in required)):
        raise trial.TrialBlocked("A completed removal and manual-restoration trial is required")
    plan, intent = reports["remove-plan-private.json"], reports["write-intent-private.json"]
    binding = plan["binding"]
    if (plan.get("schema") != 1 or canonical(intent.get("plan")) != canonical(plan)
            or intent.get("plan_sha256") != hashlib.sha256(canonical(plan).encode()).hexdigest()
            or canonical(reports["offline-plan-private.json"].get("binding")) != canonical(binding)
            or reports["write-outcome-private.json"].get("modification_requests") != 1):
        raise trial.TrialBlocked("Saved trial plan or intent changed")
    if (any(not text(binding.get(key)) for key in ("account_id", "album_id", "asset_id", "master_id"))
            or not valid_zone(binding.get("zone_id"))
            or not re.fullmatch(r"[a-f0-9]{64}", binding.get("fixture_sha256", ""))
            or not re.fullmatch(r"iarchive-test-[a-f0-9]{32}\.jpg", binding.get("fixture_file", ""))
            or type(binding.get("fixture_size")) is not int
            or not 0 < binding["fixture_size"] <= trial.MAX_ORIGINAL
            or Path(binding["fixture_source"]).name != binding["fixture_file"]
            or plan.get("original_sha256") != binding["fixture_sha256"]):
        raise trial.TrialBlocked("Saved synthetic original binding is invalid")
    restored = reports["restored-album-private.json"]
    for album in (reports["before-album-private.json"], restored):
        if (album.get("database") != "private" or len(album.get("passes", [])) != 2
                or any(canonical(album.get(key)) != canonical(binding.get(key))
                       for key in ("account_id", "zone_id", "album_id"))
                or canonical(snapshot(album["passes"][0])) != canonical(snapshot(album["passes"][1]))
                or any(len(album["passes"][-1][key]) != 5 for key in ("assets", "masters"))):
            raise trial.TrialBlocked("Saved trial album scope or metadata changed")
        pair = trial.target_pair(album, binding)
        trial.validate_original(pair, binding)
        if any(record["fields"].get("isDeleted") == 1 for record in pair.values()):
            raise trial.TrialBlocked("Saved original is still marked deleted")
    if not trial.restored_matches(reports["before-album-private.json"], restored, binding):
        raise trial.TrialBlocked("Saved restored membership or resources changed")
    return directory, binding, restored, hashes


def verify_inputs(repo, directory, binding, hashes):
    for filename, expected in hashes.items():
        if read_report(repo, directory / filename)[1] != expected:
            raise trial.TrialBlocked("Completed-trial evidence changed during the check")
    trial.verify_preserved(repo, binding, directory, downloaded=True)


def check_restored(repo, service, root, zones, name, directory, binding, restored, hashes, run, pacer, summary):
    wire = ReadOnlyWire(service, root, binding, pacer, summary)
    summary["phase"] = "before_metadata"
    before = trial.capture(service, root, zones, name, binding, pacer, run, "before", 5)
    if not same_capture(restored, before):
        raise trial.TrialBlocked("Album changed since the completed restoration; no download attempted")
    pair, url = wire.lookup()
    if canonical(pair) != canonical(trial.target_pair(before, binding)):
        raise trial.TrialBlocked("Restored original lookup differs from the fresh album")
    verify_inputs(repo, directory, binding, hashes)
    summary["phase"] = "download_original"
    trial.download_original(wire, url, run)
    summary["phase"] = "after_metadata"
    after = trial.capture(service, root, zones, name, binding, pacer, run, "after", 5)
    if not same_capture(before, after):
        raise trial.TrialBlocked("Album changed while reading the restored original")
    verify_inputs(repo, directory, binding, hashes)
    trial.verify_preserved(repo, binding, run, downloaded=True)
    save_new(run / "byte-match-private.json", {"schema": 1, "binding": binding,
             "completed_trial": directory.name, "input_sha256": hashes,
             "restored_sha256": binding["fixture_sha256"], "byte_count": binding["fixture_size"]})
    summary.update(phase="completed", restored_original_sha256_verified=True,
                   preserved_originals_unchanged=True)
    summary["limits"]["restored_remote_bytes"] = "VERIFIED FOR ONE TEST JPEG"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", required=True, help="Completed disposable-photo trial run name")
    parser.add_argument("--album", required=True)
    parser.add_argument("--reuse-session")
    parser.add_argument("--live", action="store_true", help="Only after approval: authenticate and read one original")
    args = parser.parse_args()
    run, pacer = None, None
    old_mask, old_logging = os.umask(0o077), logging.root.manager.disable
    summary = {"schema": 1, "scope": "restored_jpeg_byte_check", "provider_version": PIN,
               "started_at": datetime.now(timezone.utc).isoformat(), "phase": "offline_preparation",
               "authentication_attempted": False, "media_gets": 0, "lookup_requests": 0,
               "modification_requests": 0, "limits": dict(trial.LIMITS)}
    try:
        if version("icloudpd") != PIN:
            raise trial.TrialBlocked("Pinned provider version is required")
        repo = Path(__file__).resolve().parents[2]
        run = new_run(repo)
        directory, binding, restored, hashes = load_trial(repo, args.trial)
        verify_inputs(repo, directory, binding, hashes)
        trial.save_bytes(run / "recovery-original.jpg", trial.original_bytes(repo, Path(binding["fixture_source"])))
        save_new(run / "offline-plan-private.json", {"schema": 1, "binding": binding,
                 "completed_trial": args.trial, "input_sha256": hashes, "read_only": True})
        summary["offline_prepared"] = True
        print(f"Private byte-check output: {run}")
        if args.live:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise trial.TrialBlocked("Live check requires your interactive Terminal and separate approval")
            print("Read only: the five-item album, one asset/master lookup and one JPEG original up to 1 MiB.")
            if input(f"Type {SCOPE_PHRASE} to authenticate: ").strip() != SCOPE_PHRASE:
                raise trial.TrialBlocked("Scope confirmation did not match; no authentication attempted")
            logging.disable(logging.CRITICAL)
            from pyicloud_ipd import PyiCloudService
            pacer = Pacer()
            summary["phase"] = "authentication"
            cookies = reuse_cookies(repo, args.reuse_session) if args.reuse_session else run / "cookies"
            with read_guard(pacer):
                account = getpass.getpass("Apple account email (hidden, local only): ").strip()
                if not account:
                    raise trial.TrialBlocked("Missing account; no authentication attempted")
                summary["authentication_attempted"] = True
                service = PyiCloudService("com", account, lambda: getpass.getpass("Apple password (local only): "),
                    cookie_directory=str(cookies), with_family=False, http_timeout=30)
                if service.requires_2fa:
                    verify_second_factor(service)
                if service.requires_2sa or authenticated_account(service) != binding["account_id"]:
                    raise trial.TrialBlocked("Authenticated account did not match the completed trial")
                root = service._get_webservice_url("ckdatabasews")
            with read_guard(pacer, service, binding) as state:
                state["photo_root"] = root
                zones, errors = discover_zones(service, root)
                if errors or set(zones) != {"private", "shared"}:
                    raise trial.TrialBlocked("Library discovery was incomplete")
            check_restored(repo, service, root, zones, args.album, directory, binding, restored,
                           hashes, run, pacer, summary)
        else:
            summary["phase"] = "offline_prepared"
    except (Exception, KeyboardInterrupt) as error:
        summary["blocked"] = type(error).__name__
        if isinstance(error, (trial.TrialBlocked, ConflictBlocked, ProbeBlocked)):
            summary["finding"] = str(error)
        if isinstance(error, ChangesThrottled):
            summary.update(rate_limited=True, retry_after_seconds=error.retry_after_seconds)
    finally:
        if pacer is not None and pacer.pause is not None:
            summary.update(rate_limited=True, retry_after_seconds=pacer.pause.retry_after_seconds)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        if run is not None:
            save_new(run / "summary.json", summary)
        logging.disable(old_logging)
        os.umask(old_mask)
    print(json.dumps(summary, indent=2))
    return int("blocked" in summary)


if __name__ == "__main__":
    sys.exit(main())
