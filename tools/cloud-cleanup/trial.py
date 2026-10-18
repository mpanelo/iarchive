"""Prepare one disposable JPEG trial; live use requires separate approval."""

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud-probe"))
from album import CORE_KEYS, RESOURCE_KEYS, collect_album, safe_record
from changes import ChangesThrottled, canonical, check_throttle, text
from conflict import (
    ConflictBlocked, Exchange, Pacer, load_experiment, read_guard, read_report, same_album,
)
from originals import valid_media_url
from resources import RESOURCE_ROLES
from probe import (
    PIN, ProbeBlocked, authenticated_account, discover_zones, new_run, reuse_cookies,
    save_new, verify_second_factor,
)


MAX_ORIGINAL = 1024 * 1024
SCOPE_PHRASE = "ONE DISPOSABLE PHOTO TRIAL"
LIMITS = {"archive_cleanup_enabled": False, "physical_drive_independence": "NOT VERIFIED",
          "whole_library_coverage": "NOT VERIFIED", "atomic_version_protection": "NOT VERIFIED",
          "resource_completeness": "NOT VERIFIED", "relation_target_binding": "NOT VERIFIED",
          "restored_remote_bytes": "NOT VERIFIED", "space_reclaimed": "NOT VERIFIED"}


class TrialBlocked(Exception):
    """Fixed diagnostic text only; never include raw provider data or URLs."""


def original_bytes(repo, path):
    """Read only bounded research files, rejecting every link/reparse ancestor."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(repo / ".smoke"):
        raise TrialBlocked("Original must be inside this checkout's research directory")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TrialBlocked("Original has a link or reparse ancestor")
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ORIGINAL:
        raise TrialBlocked("Original is not a bounded nonempty regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        value = stream.read(MAX_ORIGINAL + 1)
        after = os.fstat(stream.fileno())
        with os.fdopen(os.open(path, flags), "rb") as current:
            latest = os.fstat(current.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (not stat.S_ISREG(before.st_mode) or not 0 < len(value) <= MAX_ORIGINAL
                or any(getattr(before, f) != getattr(item, f) for item in (after, latest) for f in fields)):
            raise TrialBlocked("Original changed during its bounded read")
    return value


def save_bytes(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(repo, evidence_path, baseline_name, run):
    binding, baseline = load_experiment(repo, evidence_path, baseline_name)
    evidence, _ = read_report(repo, evidence_path)
    source = Path(os.path.abspath(evidence_path)).parent / binding["fixture_file"]
    value = original_bytes(repo, source)
    expected = evidence["fixture"]["sha256"]
    if hashlib.sha256(value).hexdigest() != expected:
        raise TrialBlocked("Synthetic original does not match the upload evidence SHA-256")
    binding.update(fixture_sha256=expected, fixture_size=len(value), fixture_source=str(source))
    # The saved provider checksum is opaque. Only the known SHA-256 binds bytes.
    pair = target_pair(baseline, binding)
    validate_original(pair, binding)
    save_bytes(run / "recovery-original.jpg", value)
    if original_bytes(repo, run / "recovery-original.jpg") != value:
        raise TrialBlocked("Recovery copy did not match the synthetic original")
    save_new(run / "offline-plan-private.json", {"schema": 1, "scope": "one_disposable_jpeg",
             "binding": binding, "limits": LIMITS, "live_trial_authorized": False})
    return binding, baseline


def target_pair(album, binding):
    references = [a for a in album["passes"][-1]["assets"]
                  if a["fields"].get("masterRef") == {"recordName": binding["master_id"]}]
    if len(references) != 1 or references[0]["recordName"] != binding["asset_id"]:
        raise TrialBlocked("Synthetic original master ownership is ambiguous")
    result = {}
    for key, group, kind, identity in (("asset", "assets", "CPLAsset", binding["asset_id"]),
                                        ("master", "masters", "CPLMaster", binding["master_id"])):
        records = [r for r in album["passes"][-1][group] if r["recordName"] == identity]
        if len(records) != 1 or records[0]["recordType"] != kind:
            raise TrialBlocked("Exact synthetic asset and master are unavailable")
        result[key] = records[0]
    if result["asset"]["fields"].get("masterRef") != {"recordName": binding["master_id"]}:
        raise TrialBlocked("Synthetic asset master binding changed")
    return result


def validate_original(pair, binding):
    for key, kind in (("asset", "CPLAsset"), ("master", "CPLMaster")):
        record = pair[key]
        if (record["recordName"] != binding[key + "_id"] or record["recordType"] != kind
                or not text(record["recordChangeTag"])):
            raise TrialBlocked("Exact original owner identities are required")
        flag = record["fields"].get("isDeleted")
        if flag is not None and (type(flag) is not int or flag not in (0, 1)):
            raise TrialBlocked("Deletion metadata has an unknown value")
        purged = record["fields"].get("isExpunged")
        if purged is not None and (type(purged) is not int or purged != 0):
            raise TrialBlocked("Purged or ambiguous records are outside this trial")
    fields = pair["master"]["fields"]
    resource = fields.get("resOriginalRes")
    if ("resOriginalRes" in pair["asset"]["fields"] or not isinstance(resource, dict)
            or type(resource.get("size")) is not int or resource["size"] != binding["fixture_size"]
            or fields.get("resOriginalFileType") != "public.jpeg"
            or not text(resource.get("fileChecksum")) or resource.get("download_url_present") is not True):
        raise TrialBlocked("The exact JPEG original resource is missing or changed")
    for record in pair.values():
        # Edited originals, motion companions and unknown added resources are
        # outside this one-JPEG trial, not silently treated as covered variants.
        if any(k in record["fields"] for k in ("resOriginalVidComplRes", "resOriginalAltRes", "resSidecarRes")):
            raise TrialBlocked("Additional original, motion or sidecar is outside this trial")
        if any(k.startswith("res") and k.endswith("Res") and k not in RESOURCE_ROLES for k in record["fields"]):
            raise TrialBlocked("An unknown resource is outside this trial")
        adjustment = record["fields"].get("adjustmentType")
        if adjustment is not None and (type(adjustment) is not int or adjustment != 0):
            raise TrialBlocked("An edited item is outside this trial")


def comparable_pair(pair):
    value = json.loads(canonical(pair))
    for record in value.values():
        record.pop("recordChangeTag", None)
        record["fields"].pop("isDeleted", None)
    return value


class TrialWire(Exchange):
    """Reuse bounded transport, narrowing writes to one exact soft-delete update."""

    def lookup_body(self):
        return {"records": [{"recordName": self.binding[key]} for key in ("asset_id", "master_id")],
                "zoneID": self.binding["zone_id"], "desiredKeys": sorted(CORE_KEYS | RESOURCE_KEYS)}

    def freeze_update(self, *_args):
        raise TrialBlocked("Favorite and arbitrary updates are unavailable in this trial")

    def freeze_removal(self, pair):
        if self.update_body is not None or self.writes:
            raise TrialBlocked("Only one removal can be prepared")
        validate_original(pair, self.binding)
        asset = pair["asset"]
        if (asset["recordName"] != self.binding["asset_id"] or asset["recordType"] != "CPLAsset"
                or not text(asset["recordChangeTag"])
                or asset["fields"].get("masterRef") != {"recordName": self.binding["master_id"]}
                or any(r["fields"].get("isDeleted") == 1 for r in pair.values())):
            raise TrialBlocked("The exact active synthetic asset is required")
        self.update_body = canonical({"atomic": True, "desiredKeys": ["isDeleted"],
            "zoneID": self.binding["zone_id"], "operations": [{"operationType": "update", "record": {
                "recordName": self.binding["asset_id"], "recordType": "CPLAsset",
                "recordChangeTag": asset["recordChangeTag"], "fields": {"isDeleted": {"value": 1}}}}]})
        self.removal_body = self.update_body
        return json.loads(self.update_body)

    def send(self, kind):
        if kind == "modify" and (self.update_body is None or self.update_body != getattr(self, "removal_body", None)):
            raise TrialBlocked("Only the frozen one-photo removal request is allowed")
        try:
            return super().send(kind)
        except ChangesThrottled as error:
            self.pacer.pause = error
            raise

    def lookup(self):
        status, value = self.send("lookup")
        records = value.get("records") if isinstance(value, dict) else None
        if status != 200 or not isinstance(records, list) or len(records) != 2:
            raise TrialBlocked("Exact asset/master lookup did not complete")
        expected = {self.binding["asset_id"]: "CPLAsset", self.binding["master_id"]: "CPLMaster"}
        by_id = {}
        for record in records:
            if (not isinstance(record, dict) or record.get("recordName") not in expected
                    or record.get("recordName") in by_id or record.get("recordType") != expected[record["recordName"]]
                    or record.get("deleted", False) is not False or record.get("serverErrorCode") or record.get("error")
                    or ("zoneID" in record and canonical(record["zoneID"]) != canonical(self.binding["zone_id"]))):
                raise TrialBlocked("Lookup returned a different, missing or purged record")
            by_id[record["recordName"]] = record
        pair = {key: safe_record(by_id[self.binding[key + "_id"]]) for key in ("asset", "master")}
        validate_original(pair, self.binding)
        if pair["asset"]["fields"].get("masterRef") != {"recordName": self.binding["master_id"]}:
            raise TrialBlocked("Lookup master reference changed")
        url = by_id[self.binding["master_id"]]["fields"]["resOriginalRes"]["value"].get("downloadURL")
        return pair, url


def download_original(wire, url, run):
    from requests import Request, Session
    if not valid_media_url(url) or wire.summary.get("media_gets", 0):
        raise TrialBlocked("Only one bound original URL may be downloaded")
    if authenticated_account(wire.service) != wire.binding["account_id"]:
        raise TrialBlocked("Authenticated account changed")
    # Signed URL only. Never forward account cookies/headers to the media host.
    with Session() as session:
        session.trust_env = False
        request = session.prepare_request(Request("GET", url))
        expected = Request("GET", url).prepare().url
        if (request.method != "GET" or request.url != expected or request.body is not None
                or "Cookie" in request.headers or "Authorization" in request.headers):
            raise TrialBlocked("Prepared original download changed")
        adapter = session.get_adapter(request.url)
        if adapter.max_retries.total != 0:
            raise TrialBlocked("Download retries must be disabled")
        wire.pacer.wait()
        if authenticated_account(wire.service) != wire.binding["account_id"]:
            raise TrialBlocked("Authenticated account changed")
        wire.summary["media_gets"] = 1  # Consume even a lost response.
        response = adapter.send(request, stream=True, verify=True, cert=None, proxies={}, timeout=(10, 30))
        try:
            check_throttle(response.status_code, response.headers, None)
            if response.status_code != 200:
                raise TrialBlocked("Original did not return a full response; no redirect or retry")
            if response.headers.get("Content-Type", "").split(";")[0] not in ("image/jpeg", "application/octet-stream"):
                raise TrialBlocked("Original response was not JPEG media")
            value, started = bytearray(), time.monotonic()
            for chunk in response.iter_content(chunk_size=65536):
                if len(value) + len(chunk) > wire.binding["fixture_size"] or time.monotonic() - started > 45:
                    raise TrialBlocked("Original download exceeded its byte or time bound")
                value.extend(chunk)
            if (time.monotonic() - started > 45 or len(value) != wire.binding["fixture_size"]
                    or hashlib.sha256(value).hexdigest() != wire.binding["fixture_sha256"]):
                raise TrialBlocked("Downloaded original differs from the preserved upload fixture")
            save_bytes(run / "downloaded-original.jpg", value)
        except ChangesThrottled as error:
            wire.pacer.pause = error
            raise
        finally:
            response.close()


def capture(service, root, zones, name, binding, pacer, run, label, count):
    with read_guard(pacer, service, binding) as state:
        state["photo_root"] = root
        album, summary = collect_album(service, root, zones, name, state,
            lambda p, n, value: save_new(run / f"{label}-pass-{p}-page-{n}.json", value), expected_count=count)
    if (authenticated_account(service) != binding["account_id"] or not summary["two_passes_match"]
            or canonical(album["zone_id"]) != canonical(binding["zone_id"]) or album["album_id"] != binding["album_id"]):
        raise TrialBlocked("Fresh album identity or metadata changed")
    album.update(account_id=binding["account_id"], database="private")
    save_new(run / f"{label}-album-private.json", album)
    return album


def remaining_matches(before, after, binding):
    expected = before["passes"][-1]
    current = after["passes"][-1]
    for group, key in (("assets", "asset_id"), ("masters", "master_id")):
        wanted = {r["recordName"]: r for r in expected[group] if r["recordName"] != binding[key]}
        if canonical(wanted) != canonical({r["recordName"]: r for r in current[group]}):
            return False
    # Relation ownership is not invented from record names. Only assert that
    # remaining relations are an unchanged subset, not which removed one belonged to the target.
    old = {r["recordName"]: r for r in expected["relations"]}
    return (len(current["relations"]) <= len(old)
            and all(old.get(r["recordName"]) == r for r in current["relations"]))


def restored_matches(before, restored, binding):
    """Prove asset/master membership without inventing relation ownership."""
    for group, key in (("assets", "asset_id"), ("masters", "master_id")):
        old = {r["recordName"]: r for r in before["passes"][-1][group]}
        now = {r["recordName"]: r for r in restored["passes"][-1][group]}
        if old.keys() != now.keys() or any(old[k] != now[k] for k in old if k != binding[key]):
            return False
    return comparable_pair(target_pair(before, binding)) == comparable_pair(target_pair(restored, binding))


def reply_kind(status, value, binding):
    records = value.get("records") if isinstance(value, dict) and not value.get("serverErrorCode") and not value.get("error") else None
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        return "UNKNOWN"
    record = records[0]
    if (record.get("recordName") != binding["asset_id"] or record.get("deleted", False) is not False
            or record.get("error") or record.get("recordType", "CPLAsset") != "CPLAsset"
            or ("zoneID" in record and canonical(record["zoneID"]) != canonical(binding["zone_id"]))):
        return "UNKNOWN"
    if record.get("serverErrorCode") == "CONFLICT" and status in (200, 409):
        return "CONFLICT"
    fields = record.get("fields")
    entry = fields.get("isDeleted") if isinstance(fields, dict) else None
    deleted = entry.get("value") if isinstance(entry, dict) else None
    return "ACCEPTED" if (200 <= status < 300 and not record.get("serverErrorCode")
        and record.get("recordType") == "CPLAsset" and text(record.get("recordChangeTag"))
        and type(deleted) is int and deleted == 1) else "UNKNOWN"


def verify_preserved(repo, binding, run, downloaded=False):
    paths = [Path(binding["fixture_source"]), run / "recovery-original.jpg"]
    if downloaded:
        paths.append(run / "downloaded-original.jpg")
    for path in paths:
        value = original_bytes(repo, path)
        if len(value) != binding["fixture_size"] or hashlib.sha256(value).hexdigest() != binding["fixture_sha256"]:
            raise TrialBlocked("A preserved original changed; no removal is allowed")


def run_trial(repo, service, root, zones, name, binding, baseline, run, pacer, summary):
    wire = TrialWire(service, root, binding, pacer, summary)
    summary["phase"] = "before_metadata"
    before = capture(service, root, zones, name, binding, pacer, run, "before", 5)
    if not same_album(baseline, before, binding["asset_id"], ignore_all_tags=True):
        raise TrialBlocked("Album identities or resources differ from the upload baseline")
    pair, url = wire.lookup()
    if canonical(pair) != canonical(target_pair(before, binding)):
        raise TrialBlocked("Target lookup differs from the fresh album")
    save_new(run / "before-target-private.json", pair)
    summary["phase"] = "preserve_original"
    download_original(wire, url, run)
    verify_preserved(repo, binding, run, downloaded=True)
    summary["original_sha256_verified"] = True
    summary["phase"] = "preflight"
    preflight = capture(service, root, zones, name, binding, pacer, run, "preflight", 5)
    fresh, _ = wire.lookup()
    if (canonical(fresh) != canonical(target_pair(preflight, binding))
            or canonical(fresh) != canonical(pair)
            or not same_album(before, preflight, binding["asset_id"])):
        raise TrialBlocked("Target or album changed while preserving the original")
    request = wire.freeze_removal(fresh)
    plan = {"schema": 1, "binding": binding, "request": request, "original_sha256": binding["fixture_sha256"],
            "preflight_sha256": hashlib.sha256(canonical(preflight).encode()).hexdigest(),
            "photo_query_flags": dict(wire.query_flags), "limits": LIMITS}
    plan_hash = hashlib.sha256(canonical(plan).encode()).hexdigest()
    save_new(run / "remove-plan-private.json", plan)
    phrase = f"REMOVE ONE TEST PHOTO {plan_hash}"
    print("Only the blue synthetic JPEG is selected. This affects synced devices; version races remain possible.")
    print("No purge, automatic restoration or write retry. Local originals are preserved.")
    summary["phase"] = "confirmation"
    ready = time.monotonic()
    if input(f"Type {phrase} to send once: ").strip() != phrase or time.monotonic() - ready > 120:
        raise TrialBlocked("Removal confirmation did not match or expired; no write sent")
    verify_preserved(repo, binding, run, downloaded=True)
    saved, _ = read_report(repo, run / "remove-plan-private.json")
    if canonical(saved) != canonical(plan):
        raise TrialBlocked("The removal plan changed")
    save_new(run / "write-intent-private.json", {"plan_sha256": plan_hash, "plan": plan, "outcome": "UNKNOWN"})
    summary.update(phase="write_attempt", research_removal_confirmed=True, reply="UNKNOWN")
    try:
        status, value = wire.send("modify")
        summary["reply"] = reply_kind(status, value, binding)
    except ChangesThrottled:
        raise
    except (Exception, KeyboardInterrupt):
        summary["write_reply_unavailable"] = True
    finally:
        save_new(run / "write-outcome-private.json", {"reply": summary["reply"], "modification_requests": wire.writes,
                 "lookup_requests": wire.lookups, "photo_query_flags": dict(wire.query_flags)})
    if summary["reply"] == "CONFLICT":
        raise TrialBlocked("Removal was rejected; no retry")
    summary["phase"] = "removal_postcheck"
    removed, _ = wire.lookup()
    value = removed["asset"]["fields"].get("isDeleted")
    if (type(value) is not int or value != 1 or comparable_pair(removed) != comparable_pair(pair)):
        raise TrialBlocked("Exact soft-delete metadata was not established; no retry")
    after = capture(service, root, zones, name, binding, pacer, run, "after", 4)
    if not remaining_matches(preflight, after, binding):
        raise TrialBlocked("Remaining album metadata changed; inspect manually")
    save_new(run / "removed-target-private.json", removed)
    summary.update(removal_metadata_observed=True, remaining_assets_unchanged=True)
    if input("Check only the blue image in Recently Deleted. Type SEEN IN RECENTLY DELETED: ").strip() != "SEEN IN RECENTLY DELETED":
        raise TrialBlocked("Recently Deleted UI observation was not confirmed")
    summary["recently_deleted_ui"] = "USER REPORTED"
    if input("Restore that blue image manually in Photos, then type RESTORED ONE PHOTO: ").strip() != "RESTORED ONE PHOTO":
        raise TrialBlocked("Manual restore was not confirmed")
    summary["phase"] = "restore_postcheck"
    restored = capture(service, root, zones, name, binding, pacer, run, "restored", 5)
    restored_pair = target_pair(restored, binding)
    validate_original(restored_pair, binding)
    if (restored_pair["asset"]["fields"].get("isDeleted") == 1
            or comparable_pair(restored_pair) != comparable_pair(pair)
            or not restored_matches(before, restored, binding)):
        raise TrialBlocked("Restored identity, membership or resource metadata did not match")
    verify_preserved(repo, binding, run, downloaded=True)
    summary.update(phase="completed", restored_album_membership_verified=True,
                   restored_resource_metadata_matches=True, preserved_originals_unchanged=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--album", required=True)
    parser.add_argument("--reuse-session")
    parser.add_argument("--live", action="store_true", help="Only after separate approval: authenticate and offer one trial request")
    args = parser.parse_args()
    run, pacer = None, None
    old_mask, old_logging = os.umask(0o077), logging.root.manager.disable
    summary = {"schema": 1, "scope": "one_disposable_cleanup_trial", "provider_version": PIN,
               "started_at": datetime.now(timezone.utc).isoformat(), "phase": "offline_preparation",
               "authentication_attempted": False, "media_gets": 0, "lookup_requests": 0,
               "modification_requests": 0, "research_removal_confirmed": False, "limits": dict(LIMITS)}
    try:
        if version("icloudpd") != PIN:
            raise TrialBlocked("Pinned provider version is required")
        repo = Path(__file__).resolve().parents[2]
        run = new_run(repo)
        binding, baseline = prepare(repo, args.evidence, args.baseline, run)
        summary["offline_prepared"] = True
        print(f"Private trial output: {run}")
        if args.live:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise TrialBlocked("Live trial requires your interactive Terminal and separate approval")
            print("Separate live approval required: five-item album metadata, one original GET and at most one removal.")
            print("Only the upload-evidence-bound synthetic JPEG. Restore manually; Recently Deleted is never emptied.")
            if input(f"Type {SCOPE_PHRASE} to authenticate: ").strip() != SCOPE_PHRASE:
                raise TrialBlocked("Scope confirmation did not match; no authentication attempted")
            logging.disable(logging.CRITICAL)
            from pyicloud_ipd import PyiCloudService
            pacer = Pacer()
            summary["phase"] = "authentication"
            cookies = reuse_cookies(repo, args.reuse_session) if args.reuse_session else run / "cookies"
            with read_guard(pacer) as state:
                account = getpass.getpass("Apple account email (hidden, local only): ").strip()
                if not account:
                    raise TrialBlocked("Missing account; no authentication attempted")
                summary["authentication_attempted"] = True
                service = PyiCloudService("com", account, lambda: getpass.getpass("Apple password (local only): "),
                    cookie_directory=str(cookies), with_family=False, http_timeout=30)
                if service.requires_2fa:
                    verify_second_factor(service)
                if service.requires_2sa or authenticated_account(service) != binding["account_id"]:
                    raise TrialBlocked("Authenticated account did not match the bound experiment")
                root = service._get_webservice_url("ckdatabasews")
            with read_guard(pacer, service, binding) as state:
                state["photo_root"] = root
                zones, errors = discover_zones(service, root)
                if errors or set(zones) != {"private", "shared"}:
                    raise TrialBlocked("Library discovery was incomplete")
            run_trial(repo, service, root, zones, args.album, binding, baseline, run, pacer, summary)
        else:
            summary["phase"] = "offline_prepared"
    except (Exception, KeyboardInterrupt) as error:
        summary["blocked"] = type(error).__name__
        if isinstance(error, (TrialBlocked, ConflictBlocked, ProbeBlocked)):
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
    if summary["modification_requests"]:
        print("Check Recently Deleted and restore the blue test image manually if present. Never retry an uncertain write.")
    print(json.dumps(summary, indent=2))
    return int("blocked" in summary)


if __name__ == "__main__":
    sys.exit(main())
