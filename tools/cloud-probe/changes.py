"""Bounded, identity-only change-feed research; never an inventory or write gate."""

import json
import hashlib
import math
from email.utils import parsedate_to_datetime
import os
import re
import stat
import time


MAX_PAGES = 100
MAX_RECORDS = 10000
PAGE_SIZE = 100
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120
MIN_REQUEST_INTERVAL = 1.0
DESIRED_KEYS = ["recordName", "recordType", "recordChangeTag"]
MAX_CONTINUATION_RUNS = 20
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024


class ChangesBlocked(Exception):
    """Fixed diagnostics only: upstream bodies, identities and tokens stay private."""


class ChangesThrottled(ChangesBlocked):
    def __init__(self, delay):
        super().__init__("Cloud service requested a pause; stopped without retrying")
        self.retry_after_seconds = delay


def retry_seconds(value):
    if type(value) in (int, float) and 0 <= value <= 2**53 and math.isfinite(value):
        return math.ceil(value)
    return None


def check_throttle(status, headers, value):
    delays = []
    header = headers.get("Retry-After")
    if header is not None:
        try:
            delay = int(header) if header.isdigit() else parsedate_to_datetime(header).timestamp() - time.time()
            delay = retry_seconds(max(0, delay))
            if delay is not None:
                delays.append(delay)
        except (ValueError, TypeError, OverflowError):
            pass
    candidates = [value] if isinstance(value, dict) else []
    zones = value.get("zones") if isinstance(value, dict) else None
    if isinstance(zones, list):
        candidates.extend(item for item in zones if isinstance(item, dict))
    # Some services return per-record errors inside a zone result.
    for item in tuple(candidates):
        if isinstance(item.get("records"), list):
            candidates.extend(record for record in item["records"] if isinstance(record, dict))
    throttled = status in (429, 503) or header is not None
    for item in candidates:
        if item.get("serverErrorCode") in ("THROTTLED", "SERVICE_UNAVAILABLE", "TRY_AGAIN_LATER"):
            throttled = True
        if "retryAfter" in item:
            throttled = True
            delay = retry_seconds(item["retryAfter"])
            if delay is not None:
                delays.append(delay)
    if throttled:
        raise ChangesThrottled(max(delays) if delays else None)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def text(value, limit=2048):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def valid_zone(zone):
    return (isinstance(zone, dict) and text(zone.get("zoneName"))
            and text(zone.get("ownerRecordName"))
            and zone["zoneName"] != "_defaultZone" and len(canonical(zone)) <= 8192)


def select_zone(zones, name, owner):
    matches = [item["zone_id"] for item in zones.get("private", [])
               if not item.get("deleted") and item["zone_id"].get("zoneName") == name
               and item["zone_id"].get("ownerRecordName") == owner]
    if len(matches) != 1 or not valid_zone(matches[0]):
        raise ChangesBlocked("The selected private zone lacks an unambiguous full identity")
    # Freeze all returned identity fields, not just the two selection fields.
    return json.loads(canonical(matches[0]))


def request_body(zone, token):
    if not valid_zone(zone) or (token is not None and not text(token, 8192)):
        raise ChangesBlocked("Change-feed scope or token is malformed")
    item = {"zoneID": zone}
    if token is not None:
        item["syncToken"] = token
    return {"zones": [item], "desiredKeys": DESIRED_KEYS,
            "resultsLimit": PAGE_SIZE, "reverse": False}


def request_matches(value, zone):
    try:
        item = value["zones"][0]
        token = item.get("syncToken")
        return canonical(value) == canonical(request_body(zone, token))
    except (ChangesBlocked, ValueError, TypeError, KeyError, IndexError):
        return False


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ChangesBlocked("Change-feed JSON contains duplicate keys")
            result[key] = value
        return result

    def invalid(_):
        raise ChangesBlocked("Change-feed JSON contains nonfinite numbers")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ChangesBlocked("Change-feed JSON is malformed") from error


def safe_record(record, zone):
    if (not isinstance(record, dict) or any(key in record for key in ("serverErrorCode", "error"))
            or not text(record.get("recordName"))
            or ("zoneID" in record and canonical(record["zoneID"]) != canonical(zone))
            or type(record.get("deleted", False)) is not bool):
        raise ChangesBlocked("A change record has an error or incomplete identity")
    deleted = record.get("deleted", False)
    for key in ("recordType", "recordChangeTag"):
        if (not deleted or key in record) and not text(record.get(key)):
            raise ChangesBlocked("A change record lacks its type or version")
    if "recordType" in record and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:]{0,127}", record["recordType"]):
        raise ChangesBlocked("A change record has an unrecognized schema label")
    # Retain unknown record types and tombstones as observations; never infer media.
    return {**{key: record[key] for key in DESIRED_KEYS if key in record}, "deleted": deleted}


def parse_page(value, zone, limit):
    if not isinstance(value, dict) or set(value) != {"zones"}:
        raise ChangesBlocked("Change-feed response is incomplete or unsupported")
    entries = value["zones"]
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ChangesBlocked("Change-feed response changed the selected zone scope")
    item = entries[0]
    if set(item) != {"zoneID", "syncToken", "moreComing", "records"}:
        # Includes expired tokens and unsupported endpoints: never reset or retry.
        raise ChangesBlocked("Change-feed zone returned an error or unsupported response")
    if canonical(item["zoneID"]) != canonical(zone):
        raise ChangesBlocked("Change-feed response changed the selected zone identity")
    records = item["records"]
    if (not text(item["syncToken"], 8192) or type(item["moreComing"]) is not bool
            or not isinstance(records, list) or len(records) > min(PAGE_SIZE, limit)):
        raise ChangesBlocked("Change-feed page is incomplete or exceeded the record bound")
    return {"sync_token": item["syncToken"], "more_coming": item["moreComing"],
            "records": [safe_record(record, zone) for record in records]}


def load_continuation(repo, name, *, completed=False):
    """Validate the complete local checkpoint chain without authentication or media."""
    from probe import PIN, normalize_account_id

    runs, visited = [], set()
    total_bytes = 0
    expected_digest = None
    expected_completed = completed
    if name is None:
        raise ChangesBlocked("A checkpoint run name is required")

    def read(path, limit):
        nonlocal total_bytes
        try:
            for ancestor in (*reversed(path.parents), path):
                info = ancestor.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ChangesBlocked("Checkpoint path contains a link or reparse point")
            if not stat.S_ISREG(info.st_mode):
                raise ChangesBlocked("Checkpoint input must be a regular file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise ChangesBlocked("Checkpoint input exceeded its size bound")
                raw = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
            signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            if signature(info) != signature(before) or signature(before) != signature(after):
                raise ChangesBlocked("Checkpoint changed while being read")
            total_bytes += len(raw)
            if len(raw) > limit or total_bytes > MAX_CHECKPOINT_BYTES:
                raise ChangesBlocked("Checkpoint history exceeded its size bound")
            value = strict_json(raw)
            if not isinstance(value, dict):
                raise ChangesBlocked("Checkpoint input is malformed")
            return value, hashlib.sha256(raw).hexdigest()
        except OSError as error:
            raise ChangesBlocked("Checkpoint input is missing or unreadable") from error

    while name is not None:
        if (not isinstance(name, str) or not re.fullmatch(r"cloud-probe-[a-z0-9_]+", name)
                or name in visited or len(runs) >= MAX_CONTINUATION_RUNS - 1):
            raise ChangesBlocked("Checkpoint history has an unsafe name, cycle or too many runs")
        visited.add(name)
        directory = repo / ".smoke" / name
        summary, summary_hash = read(directory / "summary.json", 65536)
        context, context_hash = read(directory / "changes-context-private.json", 65536)
        common_valid = (type(summary.get("schema")) is int and summary["schema"] == 1
                and summary.get("scope") == "changes" and summary.get("provider_version") == PIN
                and summary.get("authenticated") is True and summary.get("library_discovery_completed") is True
                and not summary.get("rate_limited"))
        finished = (summary.get("change_feed_completed") is True and summary.get("phase") == "completed"
                    and "blocked" not in summary and summary.get("account_binding_recorded") is True)
        budget_stopped = (summary.get("phase") == "change_feed" and summary.get("change_feed_completed") is False
                and summary.get("blocked") == "ChangesBlocked" and summary.get("finding") in (
                    "Change-feed observation exceeded the collection bound",
                    "Change-feed observation exceeded the time bound"))
        if expected_completed and not (common_valid and finished):
            raise ChangesBlocked("A completed change-feed checkpoint is required for a new delta")
        if not expected_completed and not (common_valid and budget_stopped):
            raise ChangesBlocked("Only a safely budget-stopped change-feed run can continue")
        account = context.get("account_id")
        if (not isinstance(account, str) or normalize_account_id(account) != account
                or context.get("database") != "private" or context.get("complete") is not False
                or not valid_zone(context.get("zone_id"))):
            raise ChangesBlocked("Checkpoint account or full zone binding is incomplete")
        try:
            names = [path.name for path in directory.iterdir() if path.name.startswith("changes-page-")]
        except OSError as error:
            raise ChangesBlocked("Checkpoint pages are unreadable") from error
        if (not 1 <= len(names) <= MAX_PAGES or set(names) != {
                f"changes-page-{i}-private.json" for i in range(1, len(names) + 1)}):
            raise ChangesBlocked("Checkpoint pages are missing or noncontiguous")
        hashes = {"summary.json": summary_hash, "changes-context-private.json": context_hash}
        private = None
        if finished:
            private, hashes["changes-private.json"] = read(directory / "changes-private.json", 65536)
        pages = []
        for i in range(1, len(names) + 1):
            filename = f"changes-page-{i}-private.json"
            value, digest = read(directory / filename, 4 * 1024 * 1024)
            pages.append(value)
            hashes[filename] = digest
        request_counts = summary.get("request_counts")
        requests = request_counts.get("change_metadata_requests") if isinstance(request_counts, dict) else None
        if type(requests) is not int or not len(pages) <= requests <= len(pages) + 1:
            raise ChangesBlocked("Checkpoint request and page counts disagree")
        if finished and requests != len(pages):
            raise ChangesBlocked("Completed checkpoint has unresolved requests")
        digest = hashlib.sha256(canonical(hashes).encode()).hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise ChangesBlocked("An earlier checkpoint changed after continuation")
        runs.append({"name": name, "context": context, "pages": pages, "digest": digest,
                     "finished": finished, "private": private, "summary": summary})
        if "continuation" in context and "delta_from" in context:
            raise ChangesBlocked("Checkpoint has ambiguous parent bindings")
        parent = context.get("continuation", context.get("delta_from"))
        if parent is None:
            name = None
        elif (not isinstance(parent, dict) or set(parent) != {"run", "sha256"}
              or not isinstance(parent["run"], str) or not re.fullmatch(r"cloud-probe-[a-z0-9_]+", parent["run"])
              or not isinstance(parent["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", parent["sha256"])):
            raise ChangesBlocked("Checkpoint parent binding is malformed")
        else:
            name, expected_digest = parent["run"], parent["sha256"]
            expected_completed = "delta_from" in context

    newest = runs[0]
    scope = {key: newest["context"][key] for key in ("account_id", "database", "zone_id")}
    token, phase, seen = None, "initial", set()
    counts = {"initial": 0, "delta": 0}
    for run in reversed(runs):
        if canonical({key: run["context"].get(key) for key in scope}) != canonical(scope):
            raise ChangesBlocked("Checkpoint history changed account or zone")
        if "delta_from" in run["context"]:
            if phase != "finished":
                raise ChangesBlocked("A delta checkpoint must follow a completed capture")
            phase = "delta"
        local_counts = {}
        for page in run["pages"]:
            if (set(page) != {"phase", "requested_sync_token", "sync_token", "more_coming", "records", "complete"}
                    or page["phase"] != phase or page["requested_sync_token"] != token
                    or page["complete"] is not False):
                raise ChangesBlocked("Checkpoint phase or token chain is broken")
            parsed = parse_page({"zones": [{"zoneID": scope["zone_id"], "syncToken": page["sync_token"],
                                           "moreComing": page["more_coming"], "records": page["records"]}]},
                                scope["zone_id"], PAGE_SIZE)
            if canonical(parsed["records"]) != canonical(page["records"]):
                raise ChangesBlocked("Checkpoint contains unsupported record data")
            next_token = parsed["sync_token"]
            if next_token in seen and not (next_token == token and not parsed["more_coming"] and not parsed["records"]):
                raise ChangesBlocked("Checkpoint history repeats a token without progress")
            token = next_token
            seen.add(token)
            counts[phase] += len(parsed["records"])
            local_counts[phase] = local_counts.get(phase, 0) + len(parsed["records"])
            if not parsed["more_coming"]:
                phase = "delta" if phase == "initial" else "finished"
        if run["finished"]:
            private, summary = run["private"], run["summary"]
            if (phase != "finished" or private.get("final_sync_token") != token
                    or canonical({key: private.get(key) for key in scope}) != canonical(scope)
                    or canonical(private.get("chain_record_counts")) != canonical(counts)
                    or canonical(private.get("phases")) != canonical([{"phase": key, "record_count": value}
                                                                     for key, value in local_counts.items()])
                    or any(type(summary.get(key)) is not int or summary[key] != value for key, value in {
                        "change_feed_pages": len(run["pages"]), "change_feed_records": sum(local_counts.values()),
                        "chain_records_observed": sum(counts.values()), "delta_records_observed": counts["delta"],
                    }.items())):
                raise ChangesBlocked("Completed checkpoint disagrees with its saved pages")
        elif phase == "finished":
            raise ChangesBlocked("The checkpoint delta has already finished")
    return {**scope, "sync_token": token, "phase": "delta" if completed else phase, "seen_tokens": sorted(seen),
            "record_counts": counts, "chain_runs": len(runs),
            "source": {"run": newest["name"], "sha256": newest["digest"]}}


def collect_changes(service, root, zone, transport, progress, *, expected_account, continuation=None):
    """Drain an initial feed, then one delta, sharing one fixed resource budget."""
    from requests import Request
    from probe import authenticated_account

    request_body(zone, None)
    transport["approved_zone"] = zone
    started = time.monotonic()
    page_count = record_count = byte_count = 0
    token = None
    start_phase = "initial"
    counts = {"initial": 0, "delta": 0}
    phases = []
    seen = set()
    last_request = None
    if continuation is not None:
        if (continuation["account_id"] != expected_account or continuation["database"] != "private"
                or canonical(continuation["zone_id"]) != canonical(zone)):
            raise ChangesBlocked("Checkpoint belongs to a different account or full zone")
        token, start_phase = continuation["sync_token"], continuation["phase"]
        request_body(zone, token)
        if start_phase not in ("initial", "delta") or token not in continuation["seen_tokens"]:
            raise ChangesBlocked("Checkpoint continuation state is malformed")
        counts = dict(continuation["record_counts"])
        seen.update(continuation["seen_tokens"])

    def check_time():
        if time.monotonic() - started >= MAX_SECONDS:
            raise ChangesBlocked("Change-feed observation exceeded the time bound")

    try:
        for phase in (("initial", "delta") if start_phase == "initial" else ("delta",)):
            phase_records = 0
            while True:
                check_time()
                if authenticated_account(service) != expected_account:
                    raise ChangesBlocked("Authenticated account changed during change-feed capture")
                if page_count >= MAX_PAGES or record_count >= MAX_RECORDS or byte_count >= MAX_TOTAL_BYTES:
                    raise ChangesBlocked("Change-feed observation exceeded the collection bound")
                if last_request is not None:
                    while (remaining := MIN_REQUEST_INTERVAL - (time.monotonic() - last_request)) > 0:
                        check_time()
                        if time.monotonic() - started + remaining >= MAX_SECONDS:
                            raise ChangesBlocked("Change-feed observation exceeded the time bound")
                        time.sleep(remaining)
                check_time()
                body = canonical(request_body(zone, token))
                transport["change_request"] = body
                # Upstream prebuffers JSON; Requests also consumes redirect
                # bodies while preparing its optional next request. Prepare with
                # the authenticated session, then use the guarded adapter itself
                # so neither layer reads bytes before our bound. Never redirect.
                prepared = service.session.prepare_request(Request("POST",
                    root.rstrip("/") + "/database/1/com.apple.photos.cloud/production/private/changes/zone",
                    params=service.params, data=body, headers={"Content-type": "application/json"},
                ))
                settings = service.session.merge_environment_settings(prepared.url, {}, True, True, None)
                last_request = time.monotonic()
                response = service.session.get_adapter(prepared.url).send(
                    prepared, timeout=(10, 30), **settings)
                try:
                    if 300 <= response.status_code < 400:
                        raise ChangesBlocked("Change-feed request failed; no automatic retry")
                    raw = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        check_time()
                        byte_count += len(chunk)
                        if len(raw) + len(chunk) > MAX_PAGE_BYTES or byte_count > MAX_TOTAL_BYTES:
                            raise ChangesBlocked("Change-feed response exceeded the byte bound")
                        raw.extend(chunk)
                    check_time()
                    try:
                        value = strict_json(raw)
                    except ChangesBlocked:
                        check_throttle(response.status_code, response.headers, None)
                        raise
                    check_throttle(response.status_code, response.headers, value)
                    if not 200 <= response.status_code < 300:
                        raise ChangesBlocked("Change-feed request failed; no automatic retry")
                    page = parse_page(value, zone, MAX_RECORDS - record_count)
                    if authenticated_account(service) != expected_account:
                        raise ChangesBlocked("Authenticated account changed during change-feed capture")
                finally:
                    response.close()
                    transport["change_request"] = None
                next_token = page["sync_token"]
                if next_token in seen and not (
                    next_token == token and not page["more_coming"] and not page["records"]
                ):
                    raise ChangesBlocked("Change-feed pagination repeated a token without progress")
                seen.add(next_token)
                page_count += 1
                record_count += len(page["records"])
                phase_records += len(page["records"])
                counts[phase] += len(page["records"])
                progress(page_count, {"phase": phase, "requested_sync_token": token,
                                      **page, "complete": False})
                token = next_token
                if not page["more_coming"]:
                    break
            phases.append({"phase": phase, "record_count": phase_records})
    finally:
        transport["change_request"] = None
    private = {"zone_id": zone, "database": "private", "final_sync_token": token,
               "phases": phases, "chain_record_counts": counts}
    summary = {"change_feed_completed": True, "change_feed_pages": page_count,
               "change_feed_records": record_count, "change_feed_response_bytes": byte_count,
               "delta_records_observed": counts["delta"],
               "delta_records_in_run": sum(p["record_count"] for p in phases if p["phase"] == "delta"),
               "chain_records_observed": sum(counts.values()),
               "inventory_generation_guarantee": "NOT VERIFIED",
               "complete_resource_inventory": "NOT VERIFIED",
               "atomic_mutation_guard": "NOT VERIFIED"}
    return private, summary
