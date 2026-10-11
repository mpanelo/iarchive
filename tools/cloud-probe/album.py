"""Bounded album metadata queries. No media bytes, locations, or captions."""

import base64
from collections import Counter
import hashlib
import json
import re

from resources import (RESOURCE_PREFIXES, RESOURCE_ROLES, build_resource_report,
                       is_resource_field, is_resource_metadata_field)


MAX_ASSETS = 1000
PAGE_SIZE = 100
PREFIXES = RESOURCE_PREFIXES
CORE_KEYS = {"recordName", "recordType", "recordChangeTag", "masterRef", "itemType",
             "dataClassType", "isDeleted", "isExpunged", "assetSubtype", "assetSubtypeV2",
             "adjustmentType", "adjustmentRenderType", "vidComplVisibilityState"}
RESOURCE_KEYS = {prefix + suffix for prefix in PREFIXES
                 for suffix in ("Res", "FileType", "Fingerprint", "Width", "Height")}
ALBUM_KEYS = {"albumNameEnc", "isDeleted", "recordName", "recordType", "recordChangeTag"}


class AlbumBlocked(Exception):
    """Requested album or observed evidence could not be unambiguously scoped."""

    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def query_kind(value, zone, album_id, batch=False):
    """Validate the query body, not merely the read-only URL."""
    if not zone or not isinstance(value, dict):
        return None
    if batch:
        if set(value) != {"batch"} or not isinstance(value["batch"], list) or len(value["batch"]) != 1:
            return None
        item = value["batch"][0]
        expected = {"resultsLimit": 1, "query": {"recordType": "HyperionIndexCountLookup",
                    "filterBy": {"fieldName": "indexCountID", "comparator": "IN",
                                 "fieldValue": {"type": "STRING_LIST", "value": [
                                     f"CPLContainerRelationNotDeletedByAssetDate:{album_id}"]}}},
                    "zoneWide": True, "zoneID": zone}
        return "asset_metadata_requests" if album_id and item == expected else None
    if (set(value) != {"query", "zoneID", "desiredKeys", "resultsLimit"}
            or value["zoneID"] != zone or type(value["resultsLimit"]) is not int
            or not 1 <= value["resultsLimit"] <= 200 or not isinstance(value["desiredKeys"], list)
            or any(not isinstance(key, str) for key in value["desiredKeys"])):
        return None
    query = value["query"]
    if not isinstance(query, dict):
        return None
    if query == {"recordType": "CPLAlbumByPositionLive"}:
        return "album_metadata_requests" if set(value["desiredKeys"]) == ALBUM_KEYS else None
    if (not album_id or set(query) != {"recordType", "filterBy"}
            or query["recordType"] != "CPLContainerRelationLiveByAssetDate"
            or not set(value["desiredKeys"]) <= CORE_KEYS | RESOURCE_KEYS
            or not {"recordName", "recordChangeTag", "masterRef"} <= set(value["desiredKeys"])):
        return None
    filters = query["filterBy"]
    if not isinstance(filters, list) or len(filters) != 3:
        return None
    by_name = {item.get("fieldName"): item for item in filters if isinstance(item, dict)}
    if set(by_name) != {"parentId", "startRank", "direction"}:
        return None
    rank = by_name["startRank"].get("fieldValue", {}).get("value")
    if type(rank) is not int or not 0 <= rank <= MAX_ASSETS:
        return None
    expected = [
        {"fieldName": "parentId", "comparator": "EQUALS", "fieldValue": {"type": "STRING", "value": album_id}},
        {"fieldName": "startRank", "comparator": "EQUALS", "fieldValue": {"type": "INT64", "value": rank}},
        {"fieldName": "direction", "comparator": "EQUALS", "fieldValue": {"type": "STRING", "value": "ASCENDING"}},
    ]
    return "asset_metadata_requests" if all(by_name[item["fieldName"]] == item for item in expected) else None


def read_query(service, root, body, batch=False):
    suffix = "/internal/records/query/batch" if batch else "/records/query"
    response = service.session.post(
        root.rstrip("/") + "/database/1/com.apple.photos.cloud/production/private" + suffix,
        params={**service.params, "remapEnums": True, "getCurrentSyncToken": True},
        data=canonical(body), headers={"Content-type": "text/plain"},
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("serverErrorCode") or value.get("error"):
        raise AlbumBlocked("Metadata query failed")
    return value


def records(value):
    result = value.get("records")
    if not isinstance(result, list) or any(not isinstance(r, dict) or r.get("serverErrorCode") for r in result):
        raise AlbumBlocked("Incomplete or erroneous record response")
    return result


def find_album(service, root, zone, name):
    body = {"query": {"recordType": "CPLAlbumByPositionLive"}, "zoneID": zone,
            "resultsLimit": 200, "desiredKeys": sorted(ALBUM_KEYS)}
    value = read_query(service, root, body)
    if any(value.get(key) for key in ("moreComing", "continuationMarker", "continuationCursor")):
        raise AlbumBlocked("Album list was paginated; uniqueness cannot be established")
    matches = []
    album_records = records(value)
    if len(album_records) >= 200:
        raise AlbumBlocked("Album list reached the limit; uniqueness cannot be established")
    for record in album_records:
        key = record.get("recordName")
        if key in ("----Root-Folder----", "----Project-Root-Folder----"):
            continue
        fields = record.get("fields", {})
        if fields.get("isDeleted", {}).get("value"):
            continue
        encoded = fields.get("albumNameEnc", {}).get("value")
        try:
            actual = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, TypeError, UnicodeError) as error:
            raise AlbumBlocked("An album name could not be decoded") from error
        if actual == name:
            if not isinstance(key, str) or not key:
                raise AlbumBlocked("Album identity was missing")
            matches.append(key)
    if len(matches) != 1:
        raise AlbumBlocked("Requested album was absent or ambiguous")
    return matches[0]


def album_count(service, root, zone, album_id):
    body = {"batch": [{"resultsLimit": 1, "query": {
        "recordType": "HyperionIndexCountLookup", "filterBy": {
            "fieldName": "indexCountID", "comparator": "IN", "fieldValue": {
                "type": "STRING_LIST", "value": [f"CPLContainerRelationNotDeletedByAssetDate:{album_id}"]}}},
        "zoneWide": True, "zoneID": zone}]}
    value = read_query(service, root, body, batch=True)
    batch = value.get("batch")
    if not isinstance(batch, list) or len(batch) != 1:
        raise AlbumBlocked("Album count response was incomplete")
    count_records = records(batch[0])
    if len(count_records) != 1:
        raise AlbumBlocked("Album count response was ambiguous")
    count = count_records[0].get("fields", {}).get("itemCount", {}).get("value")
    if type(count) is not int or not 0 <= count <= MAX_ASSETS:
        raise AlbumBlocked("Album count is unavailable or exceeds the bounded test size")
    return count


def list_body(zone, album_id, rank):
    return {"query": {"recordType": "CPLContainerRelationLiveByAssetDate", "filterBy": [
        {"fieldName": "startRank", "comparator": "EQUALS", "fieldValue": {"type": "INT64", "value": rank}},
        {"fieldName": "direction", "comparator": "EQUALS", "fieldValue": {"type": "STRING", "value": "ASCENDING"}},
        {"fieldName": "parentId", "comparator": "EQUALS", "fieldValue": {"type": "STRING", "value": album_id}}]},
        "zoneID": zone, "resultsLimit": PAGE_SIZE * 2, "desiredKeys": sorted(CORE_KEYS | RESOURCE_KEYS)}


def safe_record(record):
    """Never persist resource URLs, wrapping keys, captions or locations."""
    result = {key: record.get(key) for key in ("recordName", "recordType", "recordChangeTag")}
    if not all(isinstance(value, str) and value for value in result.values()):
        raise AlbumBlocked("A record identity or change tag was missing")
    result["fields"] = {}
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise AlbumBlocked("Record fields were not an object")
    for key, entry in fields.items():
        if isinstance(key, str) and key.startswith("res") and key.endswith("Res") and not is_resource_field(key):
            raise AlbumBlocked("Unexpected resource field label")
        if key not in CORE_KEYS | RESOURCE_KEYS and not is_resource_metadata_field(key):
            continue
        if not isinstance(entry, dict) or "value" not in entry:
            raise AlbumBlocked("Requested metadata field was malformed")
        value = entry.get("value")
        if key == "masterRef":
            if (not isinstance(value, dict) or not isinstance(value.get("recordName"), str)
                    or not value["recordName"]):
                raise AlbumBlocked("Asset master reference was invalid")
            value = {"recordName": value["recordName"]}
        elif key.endswith("Res"):
            if not isinstance(value, dict):
                raise AlbumBlocked("Resource metadata was not an object")
            value = {"size": value.get("size"), "fileChecksum": value.get("fileChecksum"),
                     "download_url_present": bool(value.get("downloadURL"))}
            if (type(value["size"]) is not int or value["size"] < 0
                    or not isinstance(value["fileChecksum"], str) or not value["fileChecksum"]):
                raise AlbumBlocked("Resource byte count or opaque checksum was missing")
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise AlbumBlocked("Unexpected structured metadata was observed")
        result["fields"][key] = value
    return result


def collect_pass(service, root, zone, album_id, progress=None, original_urls=None):
    before = album_count(service, root, zone, album_id)
    assets, masters, relations, pages, rank = {}, {}, {}, [], 0
    while True:
        value = read_query(service, root, list_body(zone, album_id, rank))
        page_records = records(value)
        page_masters = [r for r in page_records if r.get("recordType") == "CPLMaster"]
        if any(r.get("recordType") not in ("CPLAsset", "CPLMaster", "CPLContainerRelation") for r in page_records):
            # Schema labels only: no record IDs, URLs or field values in diagnostics.
            types = Counter(r.get("recordType") if isinstance(r.get("recordType"), str)
                            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", r["recordType"])
                            else "unrecognized_schema_label" for r in page_records)
            raise AlbumBlocked("Unexpected record types were returned by the album query",
                               {"record_type_counts": dict(types), "count_before": before})
        pages.append({"rank": rank, "record_count": len(page_records),
                      "sync_token_present": bool(value.get("syncToken")),
                      "sync_token_sha256": hashlib.sha256(str(value["syncToken"]).encode()).hexdigest()
                      if value.get("syncToken") else None})
        cleaned_records = [safe_record(record) for record in page_records]
        if original_urls is not None:
            for record in page_records:
                if record["recordType"] in ("CPLAsset", "CPLMaster"):
                    resource = record.get("fields", {}).get("resOriginalRes")
                    if resource:
                        original_urls[(record["recordType"], record["recordName"])] = resource["value"].get("downloadURL")
        if progress:
            progress(len(pages), {"complete": False, "page": pages[-1], "records": cleaned_records})
        if not page_masters:
            if page_records or any(value.get(key) for key in ("moreComing", "continuationMarker", "continuationCursor")):
                raise AlbumBlocked("Pagination ended with unresolved records or continuation")
            break
        for cleaned in cleaned_records:
            target = {"CPLAsset": assets, "CPLMaster": masters,
                      "CPLContainerRelation": relations}[cleaned["recordType"]]
            if cleaned["recordName"] in target:
                raise AlbumBlocked("Duplicate identities appeared during rank pagination")
            target[cleaned["recordName"]] = cleaned
        rank += len(page_masters)
        if rank > MAX_ASSETS or len(assets) > MAX_ASSETS:
            raise AlbumBlocked("Album exceeded the bounded test size during enumeration")
    after = album_count(service, root, zone, album_id)
    if before != after or len(assets) != before:
        raise AlbumBlocked("Album count or membership changed during enumeration")
    references = [a["fields"].get("masterRef", {}).get("recordName") for a in assets.values()]
    if set(references) != set(masters):
        raise AlbumBlocked("Asset/master relationships were incomplete")
    return {"assets": [assets[key] for key in sorted(assets)],
            "masters": [masters[key] for key in sorted(masters)],
            "relations": [relations[key] for key in sorted(relations)], "pages": pages,
            "count_before": before, "count_after": after}


def collect_album(service, root, zones, name, transport, progress=None, original_urls=None):
    primary = [zone["zone_id"] for zone in zones.get("private", [])
               if zone["zone_id"].get("zoneName") == "PrimarySync" and not zone["deleted"]]
    if len(primary) != 1:
        raise AlbumBlocked("Primary library identity could not be established")
    zone = primary[0]
    transport["approved_zone"] = zone
    album_id = find_album(service, root, zone, name)
    transport["approved_album"] = album_id
    first = collect_pass(service, root, zone, album_id,
                         (lambda page, value: progress(1, page, value)) if progress else None)
    second = collect_pass(service, root, zone, album_id,
                          (lambda page, value: progress(2, page, value)) if progress else None,
                          original_urls)
    comparison = lambda result: canonical({key: result[key] for key in ("assets", "masters", "relations")})
    resource_counts = {}
    missing_urls = 0
    unknown_resources = 0
    for record in (*second["assets"], *second["masters"]):
        for field, value in record["fields"].items():
            if is_resource_field(field):
                if field in RESOURCE_ROLES:
                    resource_counts[field] = resource_counts.get(field, 0) + 1
                else:
                    unknown_resources += 1
                missing_urls += not value["download_url_present"]
    resource_report = build_resource_report(second)
    private = {"zone_id": zone, "album_id": album_id, "passes": [first, second],
               "resource_report": resource_report}
    summary = {"observed_assets": len(second["assets"]), "observed_masters": len(second["masters"]),
               "observed_album_relations": len(second["relations"]),
               "resource_field_counts": resource_counts, "resources_without_urls": missing_urls,
               "unrecognized_resource_fields": unknown_resources,
               "two_passes_match": comparison(first) == comparison(second),
               "inventory_generation_guarantee": "NOT VERIFIED",
               "complete_resource_inventory": "NOT VERIFIED", "content_mapping": "NOT VERIFIED",
               "sync_token_returned": any(page["sync_token_present"] for result in (first, second) for page in result["pages"])}
    summary.update(resource_report["summary"])
    return private, summary
