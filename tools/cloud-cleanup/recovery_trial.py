"""Shared bounded recovery research; entry points select fixed media profiles."""

import argparse
from datetime import datetime, timezone
from dataclasses import dataclass
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

import trial
from album import safe_record
from changes import ChangesThrottled, check_throttle, text, valid_zone
from conflict import ConflictBlocked, Exchange, Pacer, canonical, read_guard, read_report
from originals import snapshot, valid_media_url
from probe import (PIN, ProbeBlocked, authenticated_account, discover_zones, new_run,
                   reuse_cookies, save_new, verify_second_factor)
from resources import RESOURCE_ROLES
from verify_restored import same_capture


@dataclass(frozen=True)
class RecoveryProfile:
    key: str
    label: str
    max_resource: int
    resources: tuple[tuple[str, tuple[str, str, str]], ...]

    @property
    def scope_phrase(self):
        return f"ONE DISPOSABLE {self.label.upper()} TRIAL"

    @property
    def max_gets(self):
        return 2 * len(self.resources)

    @property
    def resource_map(self):
        return dict(self.resources)


LIVE_PHOTO = RecoveryProfile("live_photo", "Live Photo", 4 * 1024 * 1024, (
    ("resOriginalRes", ("still.heic", "public.heic", "image/heic")),
    ("resOriginalVidComplRes", ("motion.mov", "com.apple.quicktime-movie", "video/quicktime"))))
VIDEO = RecoveryProfile("video", "video", 8 * 1024 * 1024, (
    ("resOriginalRes", ("original.mov", "com.apple.quicktime-movie", "video/quicktime")),))


def run_name(value):
    if not re.fullmatch(r"cloud-probe-[a-z0-9_]+", value):
        raise trial.TrialBlocked("Unsafe research run name")
    return value


def media_bytes(repo, path, *, profile):
    """Bounded, stable research-file reads; the JPEG reader keeps its 1 MiB cap."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(repo / ".smoke"):
        raise trial.TrialBlocked("Research media must stay inside research storage")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise trial.TrialBlocked("Research media has a link or reparse ancestor")
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= profile.max_resource:
        raise trial.TrialBlocked("Research media is not a bounded nonempty regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        value = stream.read(profile.max_resource + 1)
        after = os.fstat(stream.fileno())
        with os.fdopen(os.open(path, flags), "rb") as current:
            latest = os.fstat(current.fileno())
        # Windows path and handle timestamps can differ for an unchanged file.
        # Compare fstat snapshots while retaining the original open handle.
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (not stat.S_ISREG(before.st_mode) or not 0 < len(value) <= profile.max_resource
                or any(getattr(before, key) != getattr(item, key)
                       for item in (after, latest) for key in fields)):
            raise trial.TrialBlocked("Research media changed during its bounded read")
    return value


def validate_pair(pair, binding, *, profile):
    if binding.get("trial_profile") != profile.key:
        raise trial.TrialBlocked("Recovery profile does not match the bound trial")
    for key, kind in (("asset", "CPLAsset"), ("master", "CPLMaster")):
        record = pair[key]
        if (not text(record.get("recordName")) or record["recordName"] != binding[key + "_id"] or record["recordType"] != kind
                or not text(record.get("recordChangeTag"))):
            raise trial.TrialBlocked("Original owner identity is missing or changed")
        fields = record["fields"]
        flag = fields.get("isDeleted")
        purged = fields.get("isExpunged")
        if ((flag is not None and (type(flag) is not int or flag not in (0, 1)))
                or (purged is not None and (type(purged) is not int or purged != 0))):
            raise trial.TrialBlocked("Original deletion metadata is ambiguous or purged")
        adjustment = fields.get("adjustmentType")
        if ((adjustment is not None and (type(adjustment) is not int or adjustment != 0))
                or any(k in fields for k in ({"resOriginalAltRes", "resSidecarRes", "resJPEGFullRes", "resVidFullRes",
                                               "resOriginalVidComplRes"} - profile.resource_map.keys()))
                or any(k.startswith("res") and k.endswith("Res") and k not in RESOURCE_ROLES for k in fields)):
            raise trial.TrialBlocked("Edited, alternate, sidecar or unknown resources are outside this trial")
    if pair["asset"]["fields"].get("masterRef") != {"recordName": binding["master_id"]}:
        raise trial.TrialBlocked("Original master reference changed")
    for field, (_, file_type, _) in profile.resources:
        value = pair["master"]["fields"].get(field)
        if (field in pair["asset"]["fields"] or not isinstance(value, dict)
                or type(value.get("size")) is not int or not 0 < value["size"] <= profile.max_resource
                or not text(value.get("fileChecksum")) or value.get("download_url_present") is not True
                or pair["master"]["fields"].get(field.removesuffix("Res") + "FileType") != file_type):
            raise trial.TrialBlocked("Exact original resources for the approved profile are required")
        if "resources" in binding:
            expected = binding["resources"][field]
            if value["size"] != expected["size"] or value["fileChecksum"] != expected["checksum"]:
                raise trial.TrialBlocked("Original resource metadata changed")


def prepare(repo, baseline_name, downloads_name, run, *, profile):
    baseline_dir = repo / ".smoke" / run_name(baseline_name)
    downloads = repo / ".smoke" / run_name(downloads_name)
    inputs = {}
    def read(path):
        value, digest = read_report(repo, path)
        inputs[path.relative_to(repo).as_posix()] = digest
        return value
    status = read(baseline_dir / "summary.json")
    scopes = {"album": ("album-private.json", "album_metadata_completed"),
              "restored_jpeg_byte_check": ("after-album-private.json", "restored_original_sha256_verified"),
              "one_disposable_live_photo_trial": ("final-album-private.json", "restored_originals_sha256_verified"),
              "one_disposable_video_trial": ("final-album-private.json", "restored_originals_sha256_verified")}
    if status.get("scope") not in scopes:
        raise trial.TrialBlocked("A completed five-item metadata baseline is required")
    filename, completed = scopes[status["scope"]]
    if (status.get("schema") != 1 or status.get("provider_version") != PIN
            or status.get("phase") != "completed" or "blocked" in status or status.get(completed) is not True):
        raise trial.TrialBlocked("A completed five-item metadata baseline is required")
    baseline = read(baseline_dir / filename)
    if (not text(baseline.get("account_id")) or not valid_zone(baseline.get("zone_id"))
            or not text(baseline.get("album_id")) or baseline.get("database") != "private"
            or len(baseline.get("passes", [])) != 2
            or canonical(snapshot(baseline["passes"][0])) != canonical(snapshot(baseline["passes"][1]))
            or any(len(baseline["passes"][-1][key]) != 5 for key in ("assets", "masters"))):
        raise trial.TrialBlocked("Baseline scope, count or matching passes are unavailable")
    if profile == LIVE_PHOTO:
        masters = [m for m in baseline["passes"][-1]["masters"] if "resOriginalVidComplRes" in m["fields"]]
    elif profile == VIDEO:
        masters = [m for m in baseline["passes"][-1]["masters"]
                   if m["fields"].get("resOriginalFileType") == "com.apple.quicktime-movie"]
    else:
        raise trial.TrialBlocked("Unsupported recovery profile")
    if len(masters) != 1:
        raise trial.TrialBlocked(f"Exactly one {profile.label} master is required")
    assets = [a for a in baseline["passes"][-1]["assets"]
              if a["fields"].get("masterRef") == {"recordName": masters[0]["recordName"]}]
    if len(assets) != 1:
        raise trial.TrialBlocked("Original master ownership is ambiguous")
    binding = {key: baseline[key] for key in ("account_id", "database", "zone_id", "album_id")}
    binding.update(asset_id=assets[0]["recordName"], master_id=masters[0]["recordName"], trial_profile=profile.key)
    pair = trial.target_pair(baseline, binding)
    validate_pair(pair, binding, profile=profile)
    if any(r["fields"].get("isDeleted") == 1 for r in pair.values()):
        raise trial.TrialBlocked("The selected original must be active")
    status = read(downloads / "summary.json")
    manifest = read(downloads / "download-manifest-private.json")
    if (status.get("provider_version") != PIN or status.get("phase") != "completed" or "blocked" in status
            or status.get("scope") != "variants" or status.get("resource_download_completed") is not True
            or manifest.get("schema") != 1 or manifest.get("complete") is not True):
        raise trial.TrialBlocked("Completed original-resource download evidence is required")
    binding["resources"] = {}
    paths = []
    for field, (filename, file_type, _) in profile.resources:
        matches = [item for item in manifest["resources"]
                   if item.get("asset_id") == binding["asset_id"] and item.get("resource_field") == field]
        if len(matches) != 1:
            raise trial.TrialBlocked("Saved original resource mapping is missing or ambiguous")
        item, metadata = matches[0], pair["master"]["fields"][field]
        if (any(canonical(item.get(key)) != canonical(binding[key])
                for key in ("account_id", "database", "zone_id", "album_id", "master_id"))
                or item.get("resource_record_id") != binding["master_id"] or item.get("resource_record_type") != "CPLMaster"
                or item.get("resource_file_type") != file_type or item.get("resource_size") != metadata["size"]
                or item.get("downloaded_bytes") != metadata["size"] or item.get("provider_checksum_opaque") != metadata["fileChecksum"]
                or item.get("copy_verified") is not True or item.get("download_complete") is not True
                or not re.fullmatch(r"[a-f0-9]{64}", item.get("response_sha256", ""))
                or any(item.get(key) != item["response_sha256"] for key in ("copy_a_sha256", "copy_b_sha256"))):
            raise trial.TrialBlocked("Saved original download binding or hashes differ")
        sources = []
        for key, prefix in (("copy_a", "copy-a"), ("copy_b", "copy-b")):
            relative = item.get(key, "")
            extension = re.escape(filename.rsplit('.', 1)[-1])
            if not re.fullmatch(prefix + r"/resource-[0-9]{2}\." + extension, relative):
                raise trial.TrialBlocked("Unsafe saved resource path")
            sources.append((downloads / relative).relative_to(repo).as_posix())
        paths.extend(sources)
        binding["resources"][field] = {"size": metadata["size"], "checksum": metadata["fileChecksum"],
                                       "sha256": item["response_sha256"], "sources": sources}
    if len(set(paths)) != 2 * len(profile.resources):
        raise trial.TrialBlocked("Saved original copies must use distinct paths")
    verify_preserved(repo, binding, inputs, run, profile=profile)
    for field, (filename, _, _) in profile.resources:
        trial.save_bytes(run / ("recovery-" + filename), media_bytes(repo, repo / binding["resources"][field]["sources"][0], profile=profile))
    verify_preserved(repo, binding, inputs, run, ("recovery",), profile=profile)
    save_new(run / "offline-plan-private.json", {"schema": 1, "scope": "one_disposable_" + profile.key,
             "max_resource_bytes": profile.max_resource, "max_media_gets": profile.max_gets,
             "binding": binding, "input_sha256": inputs, "limits": trial.LIMITS, "live_trial_authorized": False})
    return binding, baseline, inputs


def verify_preserved(repo, binding, inputs, run, stages=(), *, profile):
    if binding.get("trial_profile") != profile.key:
        raise trial.TrialBlocked("Recovery profile does not match the bound trial")
    for relative, expected in inputs.items():
        if read_report(repo, repo / relative)[1] != expected:
            raise trial.TrialBlocked("Saved recovery evidence changed")
    for field, (filename, _, _) in profile.resources:
        resource = binding["resources"][field]
        paths = [repo / path for path in resource["sources"]] + [run / (stage + "-" + filename) for stage in stages]
        for path in paths:
            value = media_bytes(repo, path, profile=profile)
            if len(value) != resource["size"] or hashlib.sha256(value).hexdigest() != resource["sha256"]:
                raise trial.TrialBlocked("A preserved original changed")


class RecoveryWire(Exchange):
    lookup_body = trial.TrialWire.lookup_body
    freeze_update = trial.TrialWire.freeze_update

    def __init__(self, *args, profile):
        super().__init__(*args)
        if profile not in (LIVE_PHOTO, VIDEO) or self.binding.get("trial_profile") != profile.key:
            raise trial.TrialBlocked("Recovery profile does not match the bound trial")
        self.profile = profile
        self.downloads = set()

    def lookup(self):
        status, value = self.send("lookup")
        records = value.get("records") if isinstance(value, dict) else None
        if (status != 200 or not isinstance(records, list) or len(records) != 2
                or value.get("serverErrorCode") or value.get("error")):
            raise trial.TrialBlocked("Original lookup did not return the exact asset and master")
        expected = {self.binding["asset_id"]: "CPLAsset", self.binding["master_id"]: "CPLMaster"}
        indexed = {}
        for record in records:
            if (not isinstance(record, dict) or record.get("recordName") not in expected
                    or record.get("recordName") in indexed or record.get("recordType") != expected[record["recordName"]]
                    or record.get("deleted", False) is not False or record.get("serverErrorCode") or record.get("error")
                    or ("zoneID" in record and canonical(record["zoneID"]) != canonical(self.binding["zone_id"]))):
                raise trial.TrialBlocked("Original lookup identity or scope changed")
            indexed[record["recordName"]] = record
        pair = {key: safe_record(indexed[self.binding[key + "_id"]]) for key in ("asset", "master")}
        validate_pair(pair, self.binding, profile=self.profile)
        urls = {field: indexed[self.binding["master_id"]]["fields"][field]["value"].get("downloadURL") for field, _ in self.profile.resources}
        return pair, urls

    def freeze_removal(self, pair):
        validate_pair(pair, self.binding, profile=self.profile)
        if self.update_body is not None or self.writes or any(r["fields"].get("isDeleted") == 1 for r in pair.values()):
            raise trial.TrialBlocked("Only one active original removal can be prepared")
        self.update_body = canonical({"atomic": True, "desiredKeys": ["isDeleted"], "zoneID": self.binding["zone_id"],
            "operations": [{"operationType": "update", "record": {"recordName": self.binding["asset_id"],
                "recordType": "CPLAsset", "recordChangeTag": pair["asset"]["recordChangeTag"],
                "fields": {"isDeleted": {"value": 1}}}}]})
        self.removal_body = self.update_body
        return json.loads(self.update_body)

    def send(self, kind):
        if kind == "modify" and (self.update_body is None or self.update_body != getattr(self, "removal_body", None)):
            raise trial.TrialBlocked("Only the frozen original removal is allowed")
        try:
            return super().send(kind)
        except ChangesThrottled as error:
            self.pacer.pause = error
            raise


def download_resources(wire, urls, run, stage):
    from requests import Request, Session
    profile = wire.profile
    if profile not in (LIVE_PHOTO, VIDEO) or wire.binding.get("trial_profile") != profile.key:
        raise trial.TrialBlocked("Recovery profile does not match the bound trial")
    if stage not in ("before", "restored") or wire.writes != int(stage == "restored") or set(urls) != set(profile.resource_map):
        raise trial.TrialBlocked("Original downloads are outside the approved trial stage")
    normalized = {field: Request("GET", url).prepare().url for field, url in urls.items() if valid_media_url(url)}
    if len(normalized) != len(profile.resources) or len(set(normalized.values())) != len(profile.resources):
        raise trial.TrialBlocked("Original URLs are invalid or ambiguous")
    for field, (filename, _, content_type) in profile.resources:
        resource = wire.binding["resources"][field]
        expected_size, expected_sha256 = resource["size"], resource["sha256"]
        if type(expected_size) is not int or not 0 < expected_size <= profile.max_resource:
            raise trial.TrialBlocked("Original exceeds the fixed profile byte limit")
        slot = (stage, field)
        if slot in wire.downloads or wire.summary.get("media_gets", 0) >= profile.max_gets:
            raise trial.TrialBlocked("Original download budget exhausted; no retry")
        with Session() as session:
            session.trust_env = False
            request = session.prepare_request(Request("GET", urls[field]))
            if (request.method != "GET" or request.url != normalized[field] or request.body is not None
                    or "Cookie" in request.headers or "Authorization" in request.headers):
                raise trial.TrialBlocked("Prepared original download changed")
            adapter = session.get_adapter(request.url)
            if adapter.max_retries.total != 0:
                raise trial.TrialBlocked("Original download retries must be disabled")
            wire.pacer.wait()
            if authenticated_account(wire.service) != wire.binding["account_id"]:
                raise trial.TrialBlocked("Authenticated account changed")
            wire.downloads.add(slot)
            wire.summary["media_gets"] = wire.summary.get("media_gets", 0) + 1
            response = adapter.send(request, stream=True, verify=True, cert=None, proxies={}, timeout=(10, 30))
            try:
                check_throttle(response.status_code, response.headers, None)
                if response.status_code != 200 or response.headers.get("Content-Type", "").split(";")[0] not in (content_type, "application/octet-stream"):
                    raise trial.TrialBlocked("Original response was incomplete, redirected or not the expected media type")
                value, started = bytearray(), time.monotonic()
                for chunk in response.iter_content(chunk_size=65536):
                    if len(value) + len(chunk) > expected_size or time.monotonic() - started > 45:
                        raise trial.TrialBlocked("Original download exceeded its byte or time bound")
                    value.extend(chunk)
                if (time.monotonic() - started > 45 or len(value) != expected_size
                        or hashlib.sha256(value).hexdigest() != expected_sha256):
                    raise trial.TrialBlocked("Downloaded original differs from the preserved bytes")
                trial.save_bytes(run / (stage + "-" + filename), value)
            except ChangesThrottled as error:
                wire.pacer.pause = error
                raise
            finally:
                response.close()


def run_trial(repo, service, root, zones, name, binding, baseline, inputs, run, pacer, summary, *, profile):
    wire = RecoveryWire(service, root, binding, pacer, summary, profile=profile)
    summary["phase"] = "before_metadata"
    before = trial.capture(service, root, zones, name, binding, pacer, run, "before", 5)
    if not same_capture(baseline, before):
        raise trial.TrialBlocked("Album changed since the approved recovery baseline")
    pair, urls = wire.lookup()
    if canonical(pair) != canonical(trial.target_pair(before, binding)):
        raise trial.TrialBlocked("Original lookup differs from the fresh album")
    summary["phase"] = "preserve_originals"
    download_resources(wire, urls, run, "before")
    verify_preserved(repo, binding, inputs, run, ("recovery", "before"), profile=profile)
    preflight = trial.capture(service, root, zones, name, binding, pacer, run, "preflight", 5)
    if not same_capture(before, preflight):
        raise trial.TrialBlocked("Album changed while preserving the originals")
    plan = {"schema": 1, "binding": binding, "input_sha256": inputs, "limits": trial.LIMITS,
            "max_resource_bytes": profile.max_resource, "max_media_gets": profile.max_gets,
            "request": wire.freeze_removal(trial.target_pair(preflight, binding)),
            "preflight_sha256": hashlib.sha256(canonical(preflight).encode()).hexdigest()}
    digest = hashlib.sha256(canonical(plan).encode()).hexdigest()
    save_new(run / "remove-plan-private.json", plan)
    summary["phase"] = "confirmation"
    print(f"Only the one disposable {profile.label} in the album is selected. This affects synced devices.")
    print("Originals are preserved locally. Version races remain possible; Recently Deleted is never emptied.")
    phrase = f"REMOVE ONE {profile.label.upper()} {digest}"
    ready = time.monotonic()
    if input(f"Type {phrase} to send once: ").strip() != phrase or time.monotonic() - ready > 120:
        raise trial.TrialBlocked("Removal confirmation did not match or expired")
    verify_preserved(repo, binding, inputs, run, ("recovery", "before"), profile=profile)
    if canonical(read_report(repo, run / "remove-plan-private.json")[0]) != canonical(plan):
        raise trial.TrialBlocked("Removal plan changed")
    save_new(run / "write-intent-private.json", {"plan_sha256": digest, "plan": plan, "outcome": "UNKNOWN"})
    summary.update(phase="write_attempt", research_removal_confirmed=True, reply="UNKNOWN")
    try:
        status, value = wire.send("modify")
        summary["reply"] = trial.reply_kind(status, value, binding)
    except ChangesThrottled:
        raise
    except (Exception, KeyboardInterrupt):
        summary["write_reply_unavailable"] = True
    finally:
        save_new(run / "write-outcome-private.json", {"reply": summary["reply"], "modification_requests": wire.writes,
                 "lookup_requests": wire.lookups, "photo_query_flags": dict(wire.query_flags)})
    if summary["reply"] == "CONFLICT":
        raise trial.TrialBlocked("Removal was rejected; no retry")
    summary["phase"] = "removal_postcheck"
    removed, _ = wire.lookup()
    if (removed["asset"]["fields"].get("isDeleted") != 1
            or trial.comparable_pair(removed) != trial.comparable_pair(pair)):
        raise trial.TrialBlocked("Exact removal metadata was not established; inspect manually")
    after = trial.capture(service, root, zones, name, binding, pacer, run, "after", 4)
    if not trial.remaining_matches(preflight, after, binding):
        raise trial.TrialBlocked("Other album records changed after removal")
    summary["removal_metadata_observed"] = True
    seen_phrase = "SEEN " + profile.label.upper()
    if input(f"Find only the {profile.label} in Recently Deleted. Type {seen_phrase}: ").strip() != seen_phrase:
        raise trial.TrialBlocked("Recently Deleted observation was not confirmed")
    summary["recently_deleted_ui"] = "USER REPORTED"
    restore_phrase = "RESTORED ONE " + profile.label.upper()
    if input(f"Restore that {profile.label} manually, then type {restore_phrase}: ").strip() != restore_phrase:
        raise trial.TrialBlocked("Manual restoration was not confirmed")
    summary["phase"] = "restore_postcheck"
    restored = trial.capture(service, root, zones, name, binding, pacer, run, "restored", 5)
    fresh, urls = wire.lookup()
    if (canonical(fresh) != canonical(trial.target_pair(restored, binding))
            or any(r["fields"].get("isDeleted") == 1 for r in fresh.values())
            or not trial.restored_matches(before, restored, binding)):
        raise trial.TrialBlocked("Restored identity, membership or resources changed")
    summary["restored_album_membership_verified"] = True
    download_resources(wire, urls, run, "restored")
    final = trial.capture(service, root, zones, name, binding, pacer, run, "final", 5)
    if not same_capture(restored, final):
        raise trial.TrialBlocked("Album changed while checking restored bytes")
    verify_preserved(repo, binding, inputs, run, ("recovery", "before", "restored"), profile=profile)
    save_new(run / "byte-match-private.json", {"schema": 1, "binding": binding, "input_sha256": inputs,
             "restored_resource_hashes": {field: value["sha256"] for field, value in binding["resources"].items()}})
    summary.update(restored_originals_sha256_verified=True, preserved_originals_unchanged=True, phase="playback_confirmation")
    summary["limits"]["restored_remote_bytes"] = "VERIFIED FOR ONE TEST " + profile.label.upper()
    plays_phrase = profile.label.upper() + " PLAYS"
    playback = "video playback and audio, if present" if profile == VIDEO else "Live Photo playback"
    if input(f"Check {playback} in Photos. Type {plays_phrase}: ").strip() != plays_phrase:
        raise trial.TrialBlocked("Playback remains unverified")
    summary.update(phase="completed", restored_playback="USER REPORTED")


def main(profile):
    if profile not in (LIVE_PHOTO, VIDEO):
        raise trial.TrialBlocked("Unsupported recovery profile")
    parser = argparse.ArgumentParser(description=f"Prepare one disposable {profile.label} recovery trial; live use needs approval.")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--downloads", required=True)
    parser.add_argument("--album", required=True)
    parser.add_argument("--reuse-session")
    parser.add_argument("--live", action="store_true", help=f"Only after separate approval: offer one disposable {profile.label} trial")
    args = parser.parse_args()
    run, pacer = None, None
    old_mask, old_logging = os.umask(0o077), logging.root.manager.disable
    summary = {"schema": 1, "scope": f"one_disposable_{profile.key}_trial", "provider_version": PIN,
               "started_at": datetime.now(timezone.utc).isoformat(), "phase": "offline_preparation",
               "authentication_attempted": False, "media_gets": 0, "lookup_requests": 0,
               "modification_requests": 0, "restored_playback": "NOT VERIFIED", "limits": dict(trial.LIMITS)}
    try:
        if version("icloudpd") != PIN:
            raise trial.TrialBlocked("Pinned provider version is required")
        repo = Path(__file__).resolve().parents[2]
        run = new_run(repo)
        binding, baseline, inputs = prepare(repo, args.baseline, args.downloads, run, profile=profile)
        summary["offline_prepared"] = True
        print(f"Private {profile.label} trial output: {run}")
        if args.live:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise trial.TrialBlocked("Recovery trial requires your interactive Terminal and separate approval")
            print(f"Scope: five-item album metadata, three paired lookups, {profile.max_gets} original GETs up to {profile.max_resource // (1024 * 1024)} MiB each, one removal.")
            if input(f"Type {profile.scope_phrase} to authenticate: ").strip() != profile.scope_phrase:
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
                    raise trial.TrialBlocked("Authenticated account did not match the approved original")
                root = service._get_webservice_url("ckdatabasews")
            with read_guard(pacer, service, binding) as state:
                state["photo_root"] = root
                zones, errors = discover_zones(service, root)
                if errors or set(zones) != {"private", "shared"}:
                    raise trial.TrialBlocked("Library discovery was incomplete")
            run_trial(repo, service, root, zones, args.album, binding, baseline, inputs, run, pacer, summary, profile=profile)
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
    if summary["modification_requests"]:
        print(f"If the {profile.label} is still in Recently Deleted, restore it manually. Never retry an uncertain write.")
    print(json.dumps(summary, indent=2))
    return int("blocked" in summary)
