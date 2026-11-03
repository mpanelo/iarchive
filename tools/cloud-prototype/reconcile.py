"""Compare saved cloud observations and download provenance with a raw replica manifest.

Default: reports only. Optional local verification invokes iarchive verify --deep
and publishes verification bundles. No authentication, network or cloud plans.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unicodedata

from prototype import Blocked, canonical, safe_path


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 20_000
RESOURCE_FIELDS = frozenset((
    "resOriginalRes", "resOriginalAltRes", "resOriginalVidComplRes",
    "resJPEGFullRes", "resVidFullRes", "resSidecarRes", "resJPEGLargeRes",
    "resJPEGMedRes", "resJPEGThumbRes", "resVidMedRes", "resVidSmallRes",
))
LIMITS = {
    "cloud_coverage": "NOT VERIFIED",
    "fresh_local_bytes": "NOT VERIFIED",
    "physical_drive_independence": "NOT VERIFIED",
    "provider_checksum_algorithm": "NOT VERIFIED",
    "content_relationships": "NOT VERIFIED",
    "removal_approved": False,
}
FRESH_MODE = "FRESH_LOCAL_CHECK_WITH_RECORDED_CLOUD_EVIDENCE"


def object_value(value):
    if not isinstance(value, dict):
        raise Blocked("Expected a JSON object")
    return value


def records(value):
    if not isinstance(value, list) or len(value) > MAX_RECORDS:
        raise Blocked("Expected a bounded record list")
    return [object_value(item) for item in value]


def identity(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(char) < 32 for char in value)):
        raise Blocked("Missing or malformed recorded identity")
    return value


def positive(value):
    return type(value) is int and value > 0


def sha256(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Blocked("Missing or malformed recorded SHA-256")
    return value


def relative(value):
    value = identity(value)
    if ("\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise Blocked("Unsafe or noncanonical recorded relative path")
    return value


def path_key(value):
    return unicodedata.normalize("NFC", value).casefold()


def resource_key(item):
    return tuple(identity(item.get(key)) for key in
                 ("record_type", "record_id", "resource_field"))


def download_key(item):
    return tuple(identity(item.get(key)) for key in
                 ("resource_record_type", "resource_record_id", "resource_field"))


def reconcile(observation, download, replica, primary_prefix="copy-a", secondary_prefix="copy-b"):
    observation, download = object_value(observation), object_value(download)
    replica = records(replica)
    if (type(observation.get("schema")) is not int or observation["schema"] != 1
            or type(download.get("schema")) is not int or download["schema"] != 1
            or not replica):
        raise Blocked("Unsupported or empty evidence schema")
    header, *files = replica
    if (header.get("schema_version") != 1 or type(header.get("schema_version")) is not int
            or header.get("record_type") != "replica_summary"
            or header.get("snapshot_type") != "deep_local_byte_verification"
            or not positive(header.get("run_id"))):
        raise Blocked("A deep replica manifest is required; inventory exports are insufficient")
    archive = identity(header.get("archive_id"))
    for side in ("source_identity", "secondary_identity"):
        root = object_value(header.get(side))
        identity(root.get("volume_id"))
        identity(root.get("root_id"))
    primary_prefix, secondary_prefix = relative(primary_prefix), relative(secondary_prefix)
    first_key, second_key = path_key(primary_prefix), path_key(secondary_prefix)
    if (first_key == second_key or first_key.startswith(second_key + "/")
            or second_key.startswith(first_key + "/")):
        raise Blocked("Copy prefixes must be distinct and nonoverlapping")
    findings, matches = [], []

    def finding(code, **details):
        findings.append({"code": code, **details})

    zone = object_value(observation.get("zone_id"))
    identity(zone.get("zoneName"))
    owner_bound = isinstance(zone.get("ownerRecordName"), str) and bool(zone["ownerRecordName"])
    if owner_bound:
        identity(zone["ownerRecordName"])
    else:
        finding("observation_zone_owner_unbound")
    report_scope = (identity(observation.get("database")), zone, identity(observation.get("album_id")))
    account = observation.get("account_id")
    if account is None:
        finding("observation_account_unbound")
    else:
        identity(account)
    stable = observation.get("metadata_passes_match") is True
    if not stable:
        finding("observation_metadata_unstable")
    if download.get("complete") is not True:
        finding("download_manifest_incomplete")
    if all(header["source_identity"][key] == header["secondary_identity"][key]
           for key in ("volume_id", "root_id")):
        finding("replica_roots_not_distinct")

    assets = {}
    resources = records(observation.get("resources"))
    for asset in records(observation.get("assets")):
        key = identity(asset.get("asset_id"))
        if key in assets:
            raise Blocked("Duplicate observed asset identity")
        for field in ("asset_change_tag", "master_id", "master_change_tag"):
            identity(asset.get(field))
        indices = asset.get("resource_indices")
        if (not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(resources) for i in indices)
                or len(indices) != len(set(indices))):
            raise Blocked("Malformed resource membership indices")
        assets[key] = asset
    for item in records(observation.get("findings")):
        code = item.get("code")
        if not isinstance(code, str) or re.fullmatch(r"[a-z_]{1,80}", code) is None:
            raise Blocked("Malformed observation finding")
        finding("observation_finding", observation_code=code)

    by_resource, by_asset_field = defaultdict(list), defaultdict(list)
    invalid = set()
    for index, resource in enumerate(resources):
        key = resource_key(resource)
        by_resource[key].append(index)
        ids = resource.get("asset_ids")
        if not isinstance(ids, list) or len(ids) != 1 or ids[0] not in assets:
            finding("ambiguous_resource_owner", resource_index=index)
            invalid.add(index)
            continue
        asset = assets[ids[0]]
        by_asset_field[(ids[0], key[2])].append(index)
        expected = {"CPLAsset": (asset["asset_id"], asset["asset_change_tag"]),
                    "CPLMaster": (asset["master_id"], asset["master_change_tag"])}.get(key[0])
        if (expected is None or (key[1], resource.get("record_change_tag")) != expected
                or index not in asset["resource_indices"]):
            finding("resource_owner_mismatch", resource_index=index)
            invalid.add(index)
        if (key[2] not in RESOURCE_FIELDS or resource.get("role") == "unknown"
                or not positive(resource.get("size"))
                or not isinstance(resource.get("file_type"), str) or not resource["file_type"]
                or not isinstance(resource.get("provider_checksum_opaque"), str)
                or not resource["provider_checksum_opaque"]):
            finding("unsupported_resource_metadata", resource_index=index)
            invalid.add(index)
    for group in (*by_resource.values(), *by_asset_field.values()):
        if len(group) > 1:
            finding("ambiguous_resource_identity", resource_indices=group)
            invalid.update(group)
    expectations = object_value(observation.get("expected_fields", {}))
    for key, asset in assets.items():
        actual = {i for i, resource in enumerate(resources) if key in resource.get("asset_ids", [])}
        if set(asset["resource_indices"]) != actual:
            finding("resource_membership_mismatch", asset_id=key)
            invalid.update(actual)
        fields = {resources[i]["resource_field"] for i in actual}
        if "resOriginalRes" not in fields:
            finding("missing_original_resource", asset_id=key)
        expected = expectations.get(key, [])
        if (not isinstance(expected, list) or any(field not in RESOURCE_FIELDS for field in expected)
                or len(expected) != len(set(expected))):
            raise Blocked("Malformed expected resource fields")
        for field in sorted(set(expected) - fields):
            finding("missing_expected_resource", asset_id=key, resource_field=field)
    if expectations.keys() - assets.keys():
        raise Blocked("Resource expectations reference an unknown asset")

    archive_files, aliases = {}, defaultdict(list)
    for item in files:
        if (type(item.get("schema_version")) is not int or item["schema_version"] != 1
                or item.get("record_type") != "file" or item.get("archive_id") != archive):
            raise Blocked("Mixed or unsupported replica manifest records")
        path = relative(item.get("relative_path"))
        if path in archive_files:
            raise Blocked("Duplicate replica file path")
        sha256(item.get("source_sha256"))
        sha256(item.get("secondary_sha256"))
        archive_files[path] = item
        aliases[path_key(path)].append(path)
    unsafe_paths = set()
    for group in aliases.values():
        if len(group) > 1:
            unsafe_paths.update(group)
            finding("ambiguous_portable_path", paths=group)

    downloads = records(download.get("resources"))
    provenance, path_owners = defaultdict(list), defaultdict(list)
    for number, item in enumerate(downloads):
        provenance[download_key(item)].append(number)
        for field, prefix in (("copy_a", primary_prefix), ("copy_b", secondary_prefix)):
            path = relative(item.get(field))
            if not path.startswith(prefix + "/"):
                raise Blocked("Download path is outside its explicit copy prefix")
            path_owners[(field, path_key(path))].append(number)
    ambiguous = set()
    for group in (*provenance.values(), *path_owners.values()):
        if len(group) > 1:
            ambiguous.update(group)
            finding("ambiguous_download_binding", download_indices=group)
    used_downloads, used_paths = set(), set()
    for index, resource in enumerate(resources):
        candidates = provenance.get(resource_key(resource), [])
        if not candidates:
            finding("resource_not_downloaded", resource_index=index)
            continue
        if index in invalid or len(candidates) != 1:
            continue
        number = candidates[0]
        item = downloads[number]
        used_downloads.add(number)
        path = item["copy_a"][len(primary_prefix) + 1:]
        second_path = item["copy_b"][len(secondary_prefix) + 1:]
        if path != second_path:
            finding("replica_path_binding_mismatch", resource_index=index)
            continue
        used_paths.add(path)
        if number in ambiguous or path in unsafe_paths:
            continue
        asset = assets[resource["asset_ids"][0]]
        expected = {
            "account_id": account, "database": report_scope[0], "zone_id": report_scope[1], "album_id": report_scope[2],
            "asset_id": asset["asset_id"], "asset_change_tag": asset["asset_change_tag"],
            "master_id": asset["master_id"], "master_change_tag": asset["master_change_tag"],
            "resource_record_change_tag": resource["record_change_tag"], "resource_size": resource["size"],
            "resource_file_type": resource["file_type"], "provider_checksum_opaque": resource["provider_checksum_opaque"],
        }
        if (type(item.get("resource_size")) is not int
                or any(canonical(item.get(key)) != canonical(value) for key, value in expected.items()
                       if key != "account_id" or account is not None)):
            finding("download_metadata_mismatch", resource_index=index)
            continue
        identity(item.get("account_id"))
        if (item.get("download_complete") is not True or item.get("copy_verified") is not True
                or type(item.get("downloaded_bytes")) is not int or item["downloaded_bytes"] != resource["size"]):
            finding("resource_download_incomplete", resource_index=index)
            continue
        hashes = [sha256(item.get(key)) for key in ("response_sha256", "copy_a_sha256", "copy_b_sha256")]
        local = archive_files.get(path)
        if local is None:
            finding("download_not_in_replica", resource_index=index, relative_path=path)
            continue
        if (local.get("role") not in ("image", "video", "raw", "sidecar")
                or local.get("local_replica") != "LOCAL BYTES VERIFIED"
                or type(local.get("byte_count")) is not int or local["byte_count"] != resource["size"]
                or len(set((*hashes, local["source_sha256"], local["secondary_sha256"]))) != 1):
            finding("recorded_replica_bytes_mismatch", resource_index=index, relative_path=path)
            continue
        if account is not None and owner_bound and stable:
            matches.append({"asset_id": asset["asset_id"], "master_id": asset["master_id"],
                            "resource_record_type": resource["record_type"], "resource_record_id": resource["record_id"],
                            "resource_field": resource["resource_field"], "relative_path": path,
                            "byte_count": resource["size"], "recorded_sha256": hashes[0]})
    for number in sorted(set(range(len(downloads))) - used_downloads):
        finding("unmapped_download", download_index=number)
    for path in sorted(archive_files.keys() - used_paths):
        finding("unmapped_replica_file", relative_path=path)
    if not resources:
        finding("empty_resource_observation")
    return {
        "schema": 1, "mode": "RECORDED_EVIDENCE_ONLY", "archive_id": archive,
        "recorded_replica_run": header["run_id"], "matches": matches, "findings": findings,
        "summary": {"observed_assets": len(assets), "observed_resources": len(resources),
                    "downloaded_resources": len(downloads), "replica_files": len(files), "matched_resources": len(matches),
                    "recorded_comparison": "FINDINGS" if findings else "MATCHED RECORDED OBSERVATIONS",
                    "finding_counts": dict(sorted(Counter(item["code"] for item in findings).items()))},
        "limits": dict(LIMITS),
        "note": "Saved reports can be stale or edited. No media or provider was read; matching records do not authorize removal.",
    }


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Blocked("Duplicate JSON object key")
        result[key] = value
    return result


def reject_constant(_value):
    raise Blocked("Nonfinite JSON number")


def load_input(path, jsonl=False):
    path = safe_path(Path(path))
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Blocked("Evidence input must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Blocked("Evidence input must be a regular file")
        raw = stream.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(stream.fileno())
        with os.fdopen(os.open(safe_path(path), flags), "rb") as current:
            latest = os.fstat(current.fileno())
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, key) != getattr(item, key) for item in (after, latest) for key in fields):
            raise Blocked("Evidence input changed during reading")
    if len(raw) > MAX_INPUT_BYTES:
        raise Blocked("Evidence input exceeds the bounded research limit")
    parse = lambda value: json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if jsonl:
        lines = raw.decode("utf-8-sig").splitlines()
        if not lines or len(lines) > MAX_RECORDS or any(not line.strip() for line in lines):
            raise Blocked("Empty, oversized or interrupted JSONL manifest")
        value = [parse(line) for line in lines]
    else:
        # Native PowerShell redirection can add a UTF-8 BOM or use UTF-16.
        # json.loads detects JSON encodings; the seal still binds original bytes.
        value = parse(raw)
    return value, hashlib.sha256(raw).hexdigest()


def independent(first, second):
    """Only fresh OS topology can establish separate physical disks."""
    def disks(value):
        items = value.get('physical_disks')
        return (isinstance(items, list) and bool(items)
                and all(type(item) is int and item >= 0 for item in items)
                and len(items) == len(set(items)))
    return (first['volume_id'] != second['volume_id'] and disks(first) and disks(second)
            and set(first['physical_disks']).isdisjoint(second['physical_disks']))


def command_report(command, action):
    # Spool output so even a failing large inventory cannot exhaust memory.
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL, check=False)
        if completed.returncode != 0:
            raise Blocked(f"{action} failed; no saved manifest fallback")
        output.seek(0)
        raw = output.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise Blocked(f"{action} output exceeded its bound")
    return object_value(json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)), hashlib.sha256(raw).hexdigest()


def fresh_replica(args, previous):
    """Run the existing local verifier; never reuse a saved success on failure."""
    header = previous[0]
    executable = safe_path(args.iarchive)
    if not executable.is_file():
        raise Blocked("The iarchive executable is unavailable")
    state, first, second = [safe_path(path) for path in
                            (args.state_dir, args.primary_artifacts, args.secondary_artifacts)]
    safe_path(state / 'manifest.sqlite')
    roots = [safe_path(Path(identity(header.get(key)))) for key in ('source_root', 'secondary_root')]
    # Check state selection without reading media from a different imported cycle.
    preview, _ = command_report([str(executable), '--json', '--dry-run', '--state-dir', str(state),
                                 'status', '--cycle', header['archive_id']], 'State preview')
    if (preview.get('dry_run') is not True or preview.get('will_write_media') is not False
            or preview.get('cycle') != header['archive_id']
            or not os.path.samefile(safe_path(Path(identity(preview.get('source')))), roots[0])):
        raise Blocked("State source scope does not match the supplied replica")
    parents = [safe_path(state / 'replica'), first, second]
    existing = []
    for parent in parents:
        names = set()
        with os.scandir(parent) as entries:
            for entry in entries:
                names.add(path_key(entry.name))
                if len(names) > MAX_RECORDS:
                    raise Blocked("Too many previous artifact directories")
        existing.append(names)
    command = [str(executable), '--json', '--state-dir', str(state), 'verify', '--deep',
               '--cycle', header['archive_id'], '--secondary', str(roots[1]),
               '--primary-artifacts', str(first), '--secondary-artifacts', str(second)]
    if args.allow_unverified_devices:
        command.append('--allow-unverified-devices')
    report, output_hash = command_report(command, 'Deep verification')
    if not positive(report.get('run_id')) or report['run_id'] <= header['run_id']:
        raise Blocked("A new verification run is required")
    if (type(report.get('schema_version')) is not int or report['schema_version'] != 1
            or report.get('operation') != 'verify' or report.get('cycle') != header['archive_id']
            or any(report.get(key) != header[key] for key in ('source_identity', 'secondary_identity'))
            or any(not os.path.samefile(safe_path(Path(identity(report.get(key)))), root)
                   for key, root in zip(('source_root', 'secondary_root'), roots, strict=True))):
        raise Blocked("Deep verification scope does not match the supplied replica")
    count = len(previous) - 1
    counters = {'planned_files': count, 'verified_files': count, 'copied_files': 0,
                'missing_files': 0, 'extra_files': 0, 'hash_mismatches': 0, 'file_errors': 0}
    if (report.get('complete') is not True or report.get('dry_run') is not False
            or report.get('local_replica') != 'LOCAL BYTES VERIFIED' or report.get('issues') != []
            or any(type(report.get(key)) is not int or report[key] != value for key, value in counters.items())):
        raise Blocked("Deep verification completion is inconsistent")
    physical = independent(report['source_identity'], report['secondary_identity'])
    physical_status = 'VERIFIED BY OS TOPOLOGY' if physical else 'NOT VERIFIED'
    if report.get('independent_physical_drives') != physical_status or (not physical and not args.allow_unverified_devices):
        raise Blocked("Deep verification physical topology is inconsistent")
    bundles = report.get('artifact_bundles')
    if not isinstance(bundles, list) or len(bundles) != 3:
        raise Blocked("Three fresh artifact directories are required")
    expected_hash = sha256(report.get('manifest_sha256'))
    fresh = None
    for bundle, parent, names in zip(bundles, parents, existing, strict=True):
        path = safe_path(Path(identity(bundle)))
        if not path.is_dir() or not os.path.samefile(path.parent, parent) or path_key(path.name) in names:
            raise Blocked("A fresh artifact directory is required")
        value, digest = load_input(path / 'manifest.jsonl', jsonl=True)
        if digest != expected_hash:
            raise Blocked("Fresh manifest digest differs from verification")
        if fresh is None:
            fresh = value
    current = object_value(fresh[0])
    if (current.get('run_id') != report['run_id'] or current.get('archive_id') != report['cycle']
            or any(current.get(key) != report[key] for key in ('source_identity', 'secondary_identity', 'independent_physical_drives'))
            or any(not os.path.samefile(safe_path(Path(identity(current.get(key)))), root)
                   for key, root in zip(('source_root', 'secondary_root'), roots, strict=True))):
        raise Blocked("Fresh manifest verification scope changed")
    return fresh, expected_hash, {'run_id': report['run_id'], 'artifact_bundles': bundles,
                                  'independent_physical_drives': physical_status,
                                  'verifier_output_sha256': output_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-report", type=Path, required=True)
    parser.add_argument("--download-manifest", type=Path, required=True)
    parser.add_argument("--replica-manifest", type=Path, required=True)
    parser.add_argument("--primary-prefix", default="copy-a")
    parser.add_argument("--secondary-prefix", default="copy-b")
    parser.add_argument('--verify-local', action='store_true', help='Run iarchive verify --deep before reconciling')
    parser.add_argument('--iarchive', type=Path, help='Built iarchive executable for local verification')
    parser.add_argument('--state-dir', type=Path, help='Existing state directory for this replica cycle')
    parser.add_argument('--primary-artifacts', type=Path)
    parser.add_argument('--secondary-artifacts', type=Path)
    parser.add_argument('--allow-unverified-devices', action='store_true', help='Acknowledge byte-only verification')
    args = parser.parse_args()
    try:
        options = (args.iarchive, args.state_dir, args.primary_artifacts, args.secondary_artifacts)
        if ((args.verify_local and any(option is None for option in options))
                or (not args.verify_local and (any(option is not None for option in options) or args.allow_unverified_devices))):
            raise Blocked("Local verification options require --verify-local and all four paths")
        observation, report_hash = load_input(args.resource_report)
        download, download_hash = load_input(args.download_manifest)
        replica, replica_hash = load_input(args.replica_manifest, jsonl=True)
        result = reconcile(observation, download, replica, args.primary_prefix, args.secondary_prefix)
        result["input_sha256"] = {"resource_report": report_hash, "download_manifest": download_hash,
                                 "replica_manifest": replica_hash}
        if args.verify_local:
            result['mode'] = FRESH_MODE
            result['verification'] = {'attempted': False, 'reason': 'RECORDED EVIDENCE HAS FINDINGS'}
            if not result['findings']:
                fresh, fresh_hash, verification = fresh_replica(args, replica)
                # The child may run for hours. Recheck all inputs before accepting its snapshot.
                for path, expected, jsonl in ((args.resource_report, report_hash, False),
                                              (args.download_manifest, download_hash, False),
                                              (args.replica_manifest, replica_hash, True)):
                    if load_input(path, jsonl=jsonl)[1] != expected:
                        raise Blocked("Reconciliation input changed during verification")
                result = reconcile(observation, download, fresh, args.primary_prefix, args.secondary_prefix)
                result.update(mode=FRESH_MODE, verification={'attempted': True, **verification},
                              input_sha256={'resource_report': report_hash, 'download_manifest': download_hash,
                                            'previous_replica_manifest': replica_hash, 'replica_manifest': fresh_hash})
                if not result['findings']:
                    result['limits']['fresh_local_bytes'] = 'VERIFIED FOR MATCHED RESOURCES'
                    result['limits']['physical_drive_independence'] = verification['independent_physical_drives']
            result['note'] = ('Local verification is a byte snapshot, not approval for a later operation. '
                              'Cloud evidence is recorded only; no provider was read and removal remains unavailable.')
    except (Blocked, OSError, ValueError, TypeError, KeyError, RecursionError, KeyboardInterrupt):
        # Do not echo raw reports, paths, URLs, credentials, or parser exceptions.
        print(json.dumps({"schema": 1, "mode": FRESH_MODE if args.verify_local else "RECORDED_EVIDENCE_ONLY",
                          "error": "Evidence or local verification failed; no saved success was reused", "limits": LIMITS}))
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 2 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
