"""Separately approved, one-asset Favorite conflict research. Never removal."""

from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import getpass
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import time
from unittest.mock import patch

from album import collect_album, safe_record
from album import CORE_KEYS, RESOURCE_KEYS
from changes import ChangesThrottled, canonical, check_throttle, strict_json, text, valid_zone
from originals import snapshot
from probe import (PIN, ProbeBlocked, allowed_request, authenticated_account, discover_zones,
                   new_run, request_guard, reuse_cookies, save_new, verify_second_factor)


INTERVAL = 1.0
MAX_RESPONSE = 256 * 1024
MAX_SECONDS = 45
CONFIRMATION = "TRY ONE STALE FAVORITE"
PHOTO_QUERY_FLAGS = {"remapEnums": True, "getCurrentSyncToken": True}


class ConflictBlocked(Exception):
    """The narrowly approved experiment cannot continue."""


def read_report(repo, path, limit=2 * 1024 * 1024):
    """Read bounded private JSON beneath .smoke without resolving links."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(repo / ".smoke"):
        raise ConflictBlocked("Research input must be inside this checkout's .smoke directory")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ConflictBlocked("Research input has a link or reparse ancestor")
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ConflictBlocked("Research input must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if (signature(info) != signature(before) or signature(before) != signature(after)
            or len(raw) > limit or not stat.S_ISREG(before.st_mode)):
        raise ConflictBlocked("Research input changed or exceeded its bound")
    value = strict_json(raw)
    if not isinstance(value, dict):
        raise ConflictBlocked("Research input must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def load_experiment(repo, evidence_path, baseline_name):
    evidence, evidence_hash = read_report(repo, evidence_path)
    if not re.fullmatch(r"cloud-probe-[a-z0-9_]+", baseline_name):
        raise ConflictBlocked("Unsafe baseline run name")
    directory = repo / ".smoke" / baseline_name
    summary, summary_hash = read_report(repo, directory / "summary.json", 65536)
    baseline, baseline_hash = read_report(repo, directory / "album-private.json")
    fixture = evidence.get("fixture", {})
    if (evidence.get("schema") != 1 or evidence.get("experiment") != "new_upload_old_capture_date"
            or evidence.get("new_id_and_tag_match_delta") is not True
            or evidence.get("saved_baseline_unchanged") is not True
            or not isinstance(fixture, dict)
            or not re.fullmatch(r"iarchive-test-[a-f0-9]{32}\.jpg", fixture.get("file", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", fixture.get("sha256", ""))
            or fixture.get("capture_unix_ms") != 978404645000):
        raise ConflictBlocked("A completed synthetic old-date upload experiment is required")
    hashes = evidence.get("captures_sha256", {}).get(baseline_name, {})
    if hashes.get("summary.json") != summary_hash or hashes.get("album-private.json") != baseline_hash:
        raise ConflictBlocked("Upload evidence no longer matches the saved baseline bytes")
    if (summary.get("provider_version") != PIN or summary.get("scope") != "album"
            or summary.get("phase") != "completed" or "blocked" in summary
            or summary.get("album_metadata_completed") is not True
            or summary.get("two_passes_match") is not True or summary.get("observed_assets") != 5
            or summary.get("account_binding_recorded") is not True
            or not text(baseline.get("account_id")) or baseline.get("database") != "private"
            or not valid_zone(baseline.get("zone_id")) or not text(baseline.get("album_id"))
            or len(baseline.get("passes", [])) != 2
            or canonical(snapshot(baseline["passes"][0])) != canonical(snapshot(baseline["passes"][1]))):
        raise ConflictBlocked("A completed account-bound five-item baseline is required")
    if any(canonical(evidence.get(key)) != canonical(baseline[key])
           for key in ("account_id", "database", "zone_id", "album_id")):
        raise ConflictBlocked("Upload evidence and baseline scope differ")
    target, master_id = evidence.get("new_asset_id"), evidence.get("new_master_id")
    selected = baseline["passes"][-1]
    assets = [a for a in selected["assets"] if a["recordName"] == target]
    masters = [m for m in selected["masters"] if m["recordName"] == master_id]
    if (len(selected["assets"]) != 5 or len(selected["masters"]) != 5
            or len(assets) != 1 or len(masters) != 1 or not text(target) or not text(master_id)
            or assets[0]["fields"].get("masterRef") != {"recordName": master_id}
            or assets[0]["fields"].get("assetDate") != fixture["capture_unix_ms"]
            or masters[0]["fields"].get("resOriginalFileType") != "public.jpeg"):
        raise ConflictBlocked("The synthetic JPEG cannot be bound to one exact asset and master")
    return {"account_id": baseline["account_id"], "zone_id": baseline["zone_id"],
            "album_id": baseline["album_id"], "asset_id": target, "master_id": master_id,
            "fixture_file": fixture["file"], "evidence_sha256": evidence_hash,
            "baseline_sha256": baseline_hash}, baseline


class Pacer:
    def __init__(self):
        self.last_start = None
        self.pause = None

    def wait(self):
        if self.pause is not None:
            raise self.pause
        if self.last_start is not None:
            while (delay := INTERVAL - (time.monotonic() - self.last_start)) > 0:
                time.sleep(delay)
        self.last_start = time.monotonic()


@contextmanager
def read_guard(pacer, service=None, binding=None):
    """Keep existing auth/album permissions intact, adding request pacing."""
    from requests.adapters import HTTPAdapter
    original = HTTPAdapter.send

    def paced(adapter, request, **kwargs):
        pacer.wait()
        if service is not None and authenticated_account(service) != binding["account_id"]:
            raise ConflictBlocked("Authenticated account changed")
        response = original(adapter, request, **kwargs)
        try:
            if 300 <= response.status_code < 400:
                raise ConflictBlocked("Redirect refused")
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
            if ("/database/1/com.apple.photos.cloud/" in request.url
                    or (content_type in ("application/json", "text/json") and response.status_code != 204)):
                _, raw, _ = bounded_response(response)
                # Upstream expects to parse this same response after send returns.
                response._content, response._content_consumed = raw, True
            else:
                check_throttle(response.status_code, response.headers, None)
            return response
        except ChangesThrottled as error:
            pacer.pause = error
            response.close()
            raise
        except BaseException:
            response.close()
            raise

    with patch.object(HTTPAdapter, "send", paced), request_guard(True, album=True) as state:
        yield state


def bounded_response(response):
    started, raw = time.monotonic(), bytearray()
    if 300 <= response.status_code < 400:
        raise ConflictBlocked("Redirect refused; outcome may be unknown")
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if len(raw) + len(chunk) > MAX_RESPONSE or time.monotonic() - started >= MAX_SECONDS:
                raise ConflictBlocked("Experiment response exceeded its bound")
            raw.extend(chunk)
        if time.monotonic() - started >= MAX_SECONDS:
            raise ConflictBlocked("Experiment response exceeded its time bound")
        value = strict_json(raw)
    except Exception:
        check_throttle(response.status_code, response.headers, None)
        raise
    check_throttle(response.status_code, response.headers, value)
    return response.status_code, bytes(raw), value


def same_album(before, after, target, *, ignore_all_tags=False):
    def comparable(value):
        result = json.loads(canonical(snapshot(value["passes"][-1])))
        for key in ("assets", "masters", "relations"):
            for record in result[key]:
                if ignore_all_tags or (key == "assets" and record["recordName"] == target):
                    record.pop("recordChangeTag", None)
        return result
    return (all(canonical(before[key]) == canonical(after[key]) for key in ("account_id", "zone_id", "album_id"))
            and canonical(comparable(before)) == canonical(comparable(after)))


def lookup_record(value, binding):
    if not isinstance(value, dict):
        raise ConflictBlocked("Favorite lookup did not return an object")
    records = value.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ConflictBlocked("Favorite lookup did not return exactly one record")
    record = records[0]
    if (record.get("recordName") != binding["asset_id"] or record.get("recordType") != "CPLAsset"
            or record.get("deleted", False) is not False or record.get("serverErrorCode")
            or ("zoneID" in record and canonical(record["zoneID"]) != canonical(binding["zone_id"]))):
        raise ConflictBlocked("Favorite lookup returned an error or different identity")
    return record


def favorite_diagnostics(value, binding):
    """Only presence, JSON types and identity comparisons; no raw field values."""
    record = lookup_record(value, binding)
    fields = record.get("fields")
    entries = fields if isinstance(fields, dict) else {}
    favorite_entry = entries.get("isFavorite")
    favorite = favorite_entry.get("value") if isinstance(favorite_entry, dict) else None
    master_entry = entries.get("masterRef")
    master = master_entry.get("value") if isinstance(master_entry, dict) else None
    return {"fields_type": type(fields).__name__,
            "favorite_field_present": "isFavorite" in entries,
            "favorite_entry_type": type(favorite_entry).__name__,
            "favorite_value_present": isinstance(favorite_entry, dict) and "value" in favorite_entry,
            "favorite_value_type": type(favorite).__name__,
            "favorite_value_known": type(favorite) is int and favorite in (0, 1),
            "master_reference_present": "masterRef" in entries,
            "master_entry_type": type(master_entry).__name__,
            "master_value_type": type(master).__name__,
            "master_reference_matches": isinstance(master, dict) and master.get("recordName") == binding["master_id"],
            "record_zone_returned": "zoneID" in record}


def favorite_record(value, binding):
    record = lookup_record(value, binding)
    cleaned = safe_record(record)
    entry = record["fields"].get("isFavorite")
    favorite = entry.get("value") if isinstance(entry, dict) else None
    if (type(favorite) is not int or favorite not in (0, 1)
            or cleaned["fields"].get("masterRef") != {"recordName": binding["master_id"]}):
        raise ConflictBlocked("Favorite value or master binding is unknown")
    return {"record": cleaned, "favorite": favorite}


class Exchange:
    """Only exact lookups and one pre-bound Favorite update; no session retries."""
    def __init__(self, service, root, binding, pacer, summary=None, *, metadata_only=False):
        if not allowed_request("POST", root.rstrip("/") +
                               "/database/1/com.apple.photos.cloud/production/private/zones/list", True, root):
            raise ConflictBlocked("Invalid authenticated Photos endpoint")
        self.service, self.root, self.pacer = service, root.rstrip("/"), pacer
        self.binding = json.loads(canonical(binding))
        self.lookups = self.writes = 0
        self.update_body = None
        self.summary = summary
        self.metadata_only = metadata_only
        self.query_flags = dict(PHOTO_QUERY_FLAGS)
        if summary is not None:
            summary["photo_query_flags"] = dict(self.query_flags)

    def lookup_body(self):
        return {"records": [{"recordName": self.binding["asset_id"]}],
                "zoneID": self.binding["zone_id"],
                "desiredKeys": sorted(CORE_KEYS | RESOURCE_KEYS | {"isFavorite"})}

    def freeze_update(self, before, changed):
        if self.metadata_only:
            raise ConflictBlocked("Updates are forbidden in metadata-only mode")
        if (self.update_body is not None or self.writes
                or before["record"]["recordName"] != self.binding["asset_id"]
                or changed["record"]["recordName"] != self.binding["asset_id"]
                or before["record"]["recordChangeTag"] == changed["record"]["recordChangeTag"]
                or type(before["favorite"]) is not int or before["favorite"] != 1
                or type(changed["favorite"]) is not int or changed["favorite"] != 1):
            raise ConflictBlocked("Favorite must be explicitly on at both captures and its version tag must change")
        self.update_body = canonical({"atomic": True, "desiredKeys": ["isFavorite"],
            "zoneID": self.binding["zone_id"], "operations": [{"operationType": "update", "record": {
                "recordName": self.binding["asset_id"], "recordType": "CPLAsset",
                "recordChangeTag": before["record"]["recordChangeTag"],
                "fields": {"isFavorite": {"value": 0}}}}]})
        return strict_json(self.update_body.encode())

    def send(self, kind):
        from requests import Request
        if self.metadata_only and kind != "lookup":
            raise ConflictBlocked("Updates are forbidden in metadata-only mode")
        if kind not in ("lookup", "modify"):
            raise ConflictBlocked("Unsupported experiment request")
        if (kind == "lookup" and self.lookups >= (1 if self.metadata_only else 3)) or (
                kind == "modify" and (self.writes or self.update_body is None)):
            raise ConflictBlocked("Experiment request budget exhausted")
        body = canonical(self.lookup_body()) if kind == "lookup" else self.update_body
        endpoint = self.root + "/database/1/com.apple.photos.cloud/production/private/records/" + kind
        if authenticated_account(self.service) != self.binding["account_id"]:
            raise ConflictBlocked("Authenticated account changed")
        # Match the pinned Photos wrapper without changing authentication params.
        params = {**self.service.params, **self.query_flags}
        request = Request("POST", endpoint, params=params, data=body,
                          headers={"Content-type": "application/json"})
        prepared = self.service.session.prepare_request(request)
        # Validate the actual prepared request, including any session hooks.
        expected_url = Request("POST", endpoint, params=params).prepare().url
        raw = prepared.body.encode() if isinstance(prepared.body, str) else prepared.body
        if prepared.method != "POST" or prepared.url != expected_url or raw != body.encode():
            raise ConflictBlocked("Prepared request changed the approved experiment")
        adapter = self.service.session.get_adapter(prepared.url)
        if adapter.max_retries.total != 0:
            raise ConflictBlocked("Experiment transport retries must be disabled")
        self.pacer.wait()
        if authenticated_account(self.service) != self.binding["account_id"]:
            raise ConflictBlocked("Authenticated account changed")
        # Consume before send, including lost replies. There is no write retry.
        self.lookups += kind == "lookup"
        self.writes += kind == "modify"
        if self.summary is not None:
            self.summary.update(lookup_requests=self.lookups, modification_requests=self.writes)
        response = adapter.send(prepared, stream=True, verify=True, cert=None, proxies={}, timeout=(10, 30))
        try:
            status, _, value = bounded_response(response)
            if authenticated_account(self.service) != self.binding["account_id"]:
                raise ConflictBlocked("Authenticated account changed")
            return status, value
        finally:
            response.close()

    def lookup_value(self):
        status, value = self.send("lookup")
        if status != 200 or not isinstance(value, dict):
            raise ConflictBlocked("Favorite lookup failed")
        return value

    def lookup(self, diagnostics_path=None):
        value = self.lookup_value()
        if diagnostics_path is not None:
            save_new(diagnostics_path, favorite_diagnostics(value, self.binding))
        return favorite_record(value, self.binding)


def classify_reply(status, value, binding, expected_favorite):
    if not isinstance(value, dict) or value.get("serverErrorCode") or value.get("error"):
        return "UNKNOWN"
    records = value.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        return "UNKNOWN"
    record = records[0]
    if (record.get("recordName") != binding["asset_id"]
            or record.get("deleted", False) is not False
            or record.get("error") or ("recordType" in record and record["recordType"] != "CPLAsset")
            or ("zoneID" in record and canonical(record["zoneID"]) != canonical(binding["zone_id"]))):
        return "UNKNOWN"
    if record.get("serverErrorCode") == "CONFLICT" and status in (200, 409):
        return "CONFLICT"
    fields = record.get("fields")
    entry = fields.get("isFavorite") if isinstance(fields, dict) else None
    favorite = entry.get("value") if isinstance(entry, dict) else None
    if (200 <= status < 300 and not record.get("serverErrorCode") and not record.get("error")
            and record.get("recordType") == "CPLAsset" and text(record.get("recordChangeTag"))
            and type(favorite) is int and favorite == expected_favorite):
        return "ACCEPTED"
    return "UNKNOWN"


def capture_album(service, root, zones, name, binding, pacer, run, label):
    with read_guard(pacer, service, binding) as state:
        state["photo_root"] = root
        private, summary = collect_album(service, root, zones, name, state,
            lambda pass_no, page_no, value: save_new(run / f"{label}-pass-{pass_no}-page-{page_no}.json", value),
            expected_count=5)
    if (authenticated_account(service) != binding["account_id"]
            or canonical(private["zone_id"]) != canonical(binding["zone_id"])
            or private["album_id"] != binding["album_id"] or not summary["two_passes_match"]):
        raise ConflictBlocked("Fresh album scope or metadata changed")
    private.update(account_id=binding["account_id"], database="private")
    save_new(run / f"{label}-album-private.json", private)
    return private


def check_lookup(album, observation, binding):
    matches = [a for a in album["passes"][-1]["assets"] if a["recordName"] == binding["asset_id"]]
    if len(matches) != 1 or canonical(matches[0]) != canonical(observation["record"]):
        raise ConflictBlocked("Favorite lookup differs from the fresh album metadata")


def initial_album(service, root, zones, name, binding, baseline, run, pacer):
    before_album = capture_album(service, root, zones, name, binding, pacer, run, "before")
    if not same_album(baseline, before_album, binding["asset_id"], ignore_all_tags=True):
        raise ConflictBlocked("Saved album membership, relationships or resource metadata changed")
    return before_album


def run_inspection(service, root, zones, name, binding, baseline, run, pacer, summary):
    summary["phase"] = "before_metadata"
    initial_album(service, root, zones, name, binding, baseline, run, pacer)
    wire = Exchange(service, root, binding, pacer, summary, metadata_only=True)
    summary["phase"] = "favorite_diagnostics"
    diagnostics = favorite_diagnostics(wire.lookup_value(), binding)
    save_new(run / "favorite-diagnostics.json", diagnostics)
    summary.update(phase="completed", metadata_diagnostics_completed=True, diagnostics=diagnostics)


def run_trial(service, root, zones, name, binding, baseline, run, pacer, summary):
    wire = Exchange(service, root, binding, pacer, summary)
    summary["phase"] = "before_metadata"
    before_album = initial_album(service, root, zones, name, binding, baseline, run, pacer)
    before = wire.lookup(run / "favorite-before-diagnostics.json")
    check_lookup(before_album, before, binding)
    save_new(run / "favorite-before-private.json", before)
    print(f"Synthetic photo: {binding['fixture_file']}; Favorite is {bool(before['favorite'])}.")
    if before["favorite"] != 1:
        raise ConflictBlocked("Favorite must be on before this trial; obtain approval for a fresh capture")
    if input("Turn Favorite OFF then ON on ONLY this photo; leave its heart on and type CYCLED: ").strip() != "CYCLED":
        raise ConflictBlocked("Favorite cycle confirmation did not match")
    summary["phase"] = "cycle_metadata"
    changed_album = capture_album(service, root, zones, name, binding, pacer, run, "cycled")
    changed = wire.lookup(run / "favorite-cycled-diagnostics.json")
    check_lookup(changed_album, changed, binding)
    if not same_album(before_album, changed_album, binding["asset_id"]):
        raise ConflictBlocked("Other album records or resource metadata changed during the Favorite cycle")
    save_new(run / "favorite-cycled-private.json", changed)
    update = wire.freeze_update(before, changed)
    ready_at = time.monotonic()
    print("One stale-tag update will try to clear Favorite on this synthetic photo.")
    print("Expected result: CONFLICT, leaving Favorite on. Acceptance could turn its heart off.")
    if input(f"Type {CONFIRMATION} to send once: ").strip() != CONFIRMATION:
        raise ConflictBlocked("Write confirmation did not match")
    if time.monotonic() - ready_at > 120:
        raise ConflictBlocked("Write confirmation took too long; start a new experiment")
    # Exclusive, file-fsynced intent before the only write. Never resume it.
    save_new(run / "write-intent-private.json", {"binding": binding, "request": update,
              "photo_query_flags": dict(wire.query_flags),
              "before": before, "cycled": changed, "outcome": "UNKNOWN"})
    summary.update(phase="write_attempt", reply="UNKNOWN")
    throttled = False
    try:
        status, value = wire.send("modify")
        summary["reply"] = classify_reply(status, value, binding, 0)
    except ChangesThrottled as error:
        throttled = True
        summary.update(rate_limited=True, retry_after_seconds=error.retry_after_seconds)
    except (Exception, KeyboardInterrupt):
        # A lost or invalid reply cannot authorize a second write.
        summary["write_reply_unavailable"] = True
    finally:
        summary.update(modification_requests=wire.writes, lookup_requests=wire.lookups)
        save_new(run / "write-outcome-private.json", {"reply": summary["reply"],
                 "photo_query_flags": dict(wire.query_flags),
                 "modification_requests": wire.writes, "rate_limited": throttled})
    if throttled:
        raise ConflictBlocked("Server requested a pause; post-check deferred, no automatic retry")
    summary["phase"] = "post_check"
    after = wire.lookup(run / "favorite-after-diagnostics.json")
    after_album = capture_album(service, root, zones, name, binding, pacer, run, "after")
    check_lookup(after_album, after, binding)
    save_new(run / "favorite-after-private.json", after)
    summary.update(lookup_requests=wire.lookups, post_check_completed=True,
                   other_metadata_unchanged=same_album(changed_album, after_album, binding["asset_id"]),
                   favorite_unchanged=after["favorite"] == changed["favorite"],
                   target_tag_unchanged=after["record"]["recordChangeTag"] == changed["record"]["recordChangeTag"])
    summary["stale_update_rejected"] = (summary["reply"] == "CONFLICT"
        and summary["favorite_unchanged"] and summary["target_tag_unchanged"]
        and summary["other_metadata_unchanged"] and wire.writes == 1)
    if not summary["stale_update_rejected"]:
        raise ConflictBlocked("Conflict rejection was not proved; inspect the saved evidence and Favorite flag")
    summary["phase"] = "completed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True, help="Private completed synthetic upload evidence")
    parser.add_argument("--baseline", required=True, help="Five-item album run referenced by that evidence")
    parser.add_argument("--album", required=True, help="Exact separately approved disposable album name")
    parser.add_argument("--reuse-session", help="Previously authenticated run beneath .smoke")
    parser.add_argument("--metadata-only", action="store_true", help="Inspect field presence/types; no toggle or update")
    args = parser.parse_args()
    run = pacer = None
    old_mask, old_logging = os.umask(0o077), logging.root.manager.disable
    summary = {"schema": 1, "scope": "favorite_metadata" if args.metadata_only else "favorite_conflict", "provider_version": PIN,
               "started_at": datetime.now(timezone.utc).isoformat(), "authenticated": False,
               "phase": "input_check", "modification_requests": 0, "stale_update_rejected": False,
               "removal_approved": False, "master_resource_guard": "NOT VERIFIED"}
    try:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise ConflictBlocked("Run in your own interactive Terminal")
        if version("icloudpd") != PIN:
            raise ConflictBlocked("Pinned provider version is required")
        repo = Path(__file__).resolve().parents[2]
        binding, baseline = load_experiment(repo, args.evidence, args.baseline)
        if args.metadata_only:
            print("Scope: five-item album metadata and one synthetic photo's Favorite field diagnostics.")
            print("Metadata-only mode: no toggle, update or media download.")
        else:
            print("Separate live approval is required: five-item album metadata and ONE synthetic photo's Favorite flag.")
            print("No media downloads, removal, forced updates, write retries or automatic restoration.")
        print("Request starts are at least one second apart. Server pauses stop the experiment.")
        phrase = "READ ONLY FAVORITE" if args.metadata_only else "FAVORITE CONFLICT TEST"
        if input(f"Type {phrase} to authenticate: ").strip() != phrase:
            raise ConflictBlocked("Scope confirmation did not match; no authentication attempted")
        run = new_run(repo)
        print(f"Private output directory: {run}")
        save_new(run / "binding-private.json", binding)
        cookies = reuse_cookies(repo, args.reuse_session) if args.reuse_session else run / "cookies"
        logging.disable(logging.CRITICAL)
        from pyicloud_ipd import PyiCloudService
        pacer = Pacer()
        summary["phase"] = "authentication"
        with read_guard(pacer) as state:
            account = getpass.getpass("Apple account email (hidden, local only): ").strip()
            if not account:
                raise ConflictBlocked("Missing account; no authentication attempted")
            service = PyiCloudService("com", account,
                lambda: getpass.getpass("Apple account password (local only): "),
                cookie_directory=str(cookies), with_family=False, http_timeout=30)
            if service.requires_2fa:
                verify_second_factor(service)
            if service.requires_2sa or authenticated_account(service) != binding["account_id"]:
                raise ConflictBlocked("Authentication or bound account did not match")
            summary["authenticated"] = True
            root = service._get_webservice_url("ckdatabasews")
        with read_guard(pacer, service, binding) as state:
            state["photo_root"] = root
            zones, errors = discover_zones(service, root)
            if errors or set(zones) != {"private", "shared"}:
                raise ConflictBlocked("Library discovery was incomplete")
        trial = run_inspection if args.metadata_only else run_trial
        trial(service, root, zones, args.album, binding, baseline, run, pacer, summary)
    except (Exception, KeyboardInterrupt) as error:
        summary["blocked"] = type(error).__name__
        if isinstance(error, (ConflictBlocked, ProbeBlocked)):
            summary["finding"] = str(error)
        if isinstance(error, ChangesThrottled):
            summary.update(rate_limited=True, retry_after_seconds=error.retry_after_seconds)
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        if run is not None:
            if pacer is not None and pacer.pause is not None:
                summary.update(rate_limited=True, retry_after_seconds=pacer.pause.retry_after_seconds)
            save_new(run / "summary.json", summary)
        logging.disable(old_logging)
        os.umask(old_mask)
    print(json.dumps(summary, indent=2))
    return int("blocked" in summary or not (
        summary.get("metadata_diagnostics_completed") if args.metadata_only else summary["stale_update_rejected"]))


if __name__ == "__main__":
    sys.exit(main())
