"""Inventory observed resource fields without claiming completeness or pairing."""

from collections import Counter, defaultdict
import re


# Descriptive labels for fields requested by the pinned provider. These are not
# an authoritative Apple schema or proof of the contents of a resource.
RESOURCE_ROLES = {
    "resOriginalRes": "original",
    "resOriginalAltRes": "alternate_original",
    "resOriginalVidComplRes": "original_video_companion",
    "resJPEGFullRes": "full_image",
    "resVidFullRes": "full_video",
    "resSidecarRes": "sidecar",
    "resJPEGLargeRes": "preview_image",
    "resJPEGMedRes": "preview_image",
    "resJPEGThumbRes": "preview_image",
    "resVidMedRes": "preview_video",
    "resVidSmallRes": "preview_video",
}
RESOURCE_PREFIXES = tuple(field.removesuffix("Res") for field in RESOURCE_ROLES)


class ResourceReportBlocked(ValueError):
    """Malformed observations or expectations cannot support a report."""


def is_resource_field(field):
    return isinstance(field, str) and re.fullmatch(r"res[A-Za-z][A-Za-z0-9_]{0,80}Res", field) is not None


def is_resource_metadata_field(field):
    return isinstance(field, str) and any(
        field.endswith(suffix) and is_resource_field(field.removesuffix(suffix) + "Res")
        for suffix in ("Res", "FileType", "Fingerprint", "Width", "Height")
    )


def record_index(records, record_type):
    result = {}
    if not isinstance(records, list):
        raise ResourceReportBlocked("Resource observation records must be a list")
    for record in records:
        if (not isinstance(record, dict) or record.get("recordType") != record_type
                or not isinstance(record.get("fields"), dict)
                or any(not isinstance(record.get(key), str) or not record[key]
                       for key in ("recordName", "recordChangeTag"))):
            raise ResourceReportBlocked("A resource observation identity or tag was missing")
        identity = record["recordName"]
        if identity in result:
            raise ResourceReportBlocked("Resource observation identities were duplicated")
        result[identity] = record
    return result


def build_resource_report(observation, expected_fields=None):
    """Compare sanitized observations with optional exact, caller-supplied fields.

    Expectations describe disposable fixtures, not inferred Apple requirements.
    Resource indices refer only to this report; full owner identities and tags
    remain attached to every entry. Never merge entries by name or checksum.
    """
    assets = record_index(observation.get("assets"), "CPLAsset")
    masters = record_index(observation.get("masters"), "CPLMaster")
    if expected_fields is not None:
        if not isinstance(expected_fields, dict) or not expected_fields:
            raise ResourceReportBlocked("Resource expectations must be a nonempty object")
        for identity, fields in expected_fields.items():
            if (identity not in assets or not isinstance(fields, list) or not fields
                    or any(not is_resource_field(field) for field in fields)
                    or len(fields) != len(set(fields))):
                raise ResourceReportBlocked("Resource expectations require full observed IDs and unique fields")
    expected_fields = expected_fields or {}
    findings, resources, asset_reports = [], [], []
    owners, references = defaultdict(list), defaultdict(list)

    def finding(code, asset_id=None, **details):
        value = {"code": code, **details}
        if asset_id is not None:
            value["asset_id"] = asset_id
        findings.append(value)

    for identity, asset in sorted(assets.items()):
        reference = asset["fields"].get("masterRef", {})
        master_id = reference.get("recordName") if isinstance(reference, dict) else None
        if master_id in masters:
            references[master_id].append(identity)
        else:
            finding("missing_master_reference", identity)

    for record in (*[assets[key] for key in sorted(assets)], *[masters[key] for key in sorted(masters)]):
        record_type, identity = record["recordType"], record["recordName"]
        associated = [identity] if record_type == "CPLAsset" else references[identity]
        if record_type == "CPLMaster" and not associated:
            finding("unreferenced_master", record_id=identity)
        incomplete = {field.removesuffix(suffix) + "Res"
                      for field in record["fields"]
                      for suffix in ("FileType", "Fingerprint", "Width", "Height")
                      if field.endswith(suffix) and is_resource_metadata_field(field)
                      and field.removesuffix(suffix) + "Res" not in record["fields"]}
        for field in sorted(incomplete):
            for asset_id in associated or [None]:
                finding("metadata_without_resource", asset_id, record_id=identity, resource_field=field)
        for field, value in sorted(record["fields"].items()):
            if not is_resource_field(field):
                continue
            if not isinstance(value, dict):
                raise ResourceReportBlocked("Resource observations must contain sanitized metadata")
            prefix = field.removesuffix("Res")
            item = {"record_type": record_type, "record_id": identity,
                    "record_change_tag": record["recordChangeTag"], "asset_ids": list(associated),
                    "resource_field": field, "role": RESOURCE_ROLES.get(field, "unknown"),
                    "size": value.get("size"), "provider_checksum_opaque": value.get("fileChecksum"),
                    "download_url_present": value.get("download_url_present") is True,
                    "file_type": record["fields"].get(prefix + "FileType")}
            for suffix in ("Fingerprint", "Width", "Height"):
                if prefix + suffix in record["fields"]:
                    item[suffix.lower()] = record["fields"][prefix + suffix]
            index = len(resources)
            resources.append(item)
            owners[(record_type, identity)].append(index)
            codes = []
            if item["role"] == "unknown":
                codes.append("unknown_resource_field")
            if type(item["size"]) is not int or item["size"] < 0:
                codes.append("invalid_resource_size")
            elif item["size"] == 0:
                codes.append("empty_resource")
            if not isinstance(item["provider_checksum_opaque"], str) or not item["provider_checksum_opaque"]:
                codes.append("missing_opaque_checksum")
            if not item["download_url_present"]:
                codes.append("resource_url_unavailable")
            if not isinstance(item["file_type"], str) or not item["file_type"]:
                codes.append("missing_file_type")
            for code in codes:
                for asset_id in associated or [None]:
                    finding(code, asset_id, resource_index=index)

    for identity, asset in sorted(assets.items()):
        reference = asset["fields"].get("masterRef", {})
        master_id = reference.get("recordName") if isinstance(reference, dict) else None
        indices = [*owners[("CPLAsset", identity)], *owners[("CPLMaster", master_id)]]
        counts = Counter(resources[index]["resource_field"] for index in indices)
        if not counts["resOriginalRes"]:
            finding("missing_original_resource", identity)
        if master_id in masters and len(references[master_id]) > 1:
            finding("shared_master", identity, master_id=master_id)
        for field, count in sorted(counts.items()):
            if count > 1:
                finding("ambiguous_resource_ownership", identity, resource_field=field)
        if identity in expected_fields:
            expected, observed = set(expected_fields[identity]), set(counts)
            for field in sorted(expected - observed):
                finding("missing_expected_resource", identity, resource_field=field)
            for field in sorted(observed - expected):
                finding("unexpected_resource", identity, resource_field=field)
        asset_reports.append({"asset_id": identity, "asset_change_tag": asset["recordChangeTag"],
                              "master_id": master_id,
                              "master_change_tag": masters[master_id]["recordChangeTag"] if master_id in masters else None,
                              "resource_indices": indices, "pair_confidence": "unknown"})

    status = "NOT PROVIDED"
    if expected_fields:
        status = "FINDINGS" if any(item.get("asset_id") in expected_fields for item in findings) else "MATCHED OBSERVED FIELDS"
    summary = {"observed_resources": len(resources),
               "resource_role_counts": dict(sorted(Counter(item["role"] for item in resources).items())),
               "resource_finding_counts": dict(sorted(Counter(item["code"] for item in findings).items())),
               "assets_with_resource_findings": len({item["asset_id"] for item in findings if "asset_id" in item}),
               "expectation_asset_count": len(expected_fields), "expectation_status": status,
               "complete_resource_inventory": "NOT VERIFIED", "content_mapping": "NOT VERIFIED",
               "provider_checksum_algorithm": "NOT VERIFIED"}
    return {"schema": 1, "resources": resources, "assets": asset_reports,
            "expected_fields": expected_fields, "findings": findings, "summary": summary}
