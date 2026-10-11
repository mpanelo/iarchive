"""Download only a frozen two-asset test set and verify two checkout-local copies."""

import hashlib
import os
from pathlib import Path
import shutil
import stat
import time
from urllib.parse import urlsplit

from album import canonical


MAX_ORIGINAL_BYTES = 256 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_SECONDS = 180


class DownloadBlocked(Exception):
    """Fixed diagnostic text only, never a URL or upstream response message."""


def snapshot(value):
    return {key: value[key] for key in ("assets", "masters", "relations")}


def valid_media_url(url):
    if not isinstance(url, str) or not url.isascii() or any(char.isspace() or ord(char) < 32 for char in url):
        return False
    try:
        value = urlsplit(url)
        return (value.scheme == "https" and value.port in (None, 443)
                and not value.username and not value.password and not value.fragment
                and bool(value.path) and bool(value.hostname)
                and (value.hostname.endswith(".icloud.com")
                     or value.hostname.endswith(".icloud-content.com")
                     or value.hostname == "icloud-content.com"))
    except ValueError:
        return False


def select_originals(baseline, current, urls, account_id):
    if not isinstance(account_id, str) or not account_id:
        raise DownloadBlocked("Authenticated account identity is missing")
    if (baseline["zone_id"] != current["zone_id"] or baseline["album_id"] != current["album_id"]
            or canonical(snapshot(baseline["passes"][-1])) != canonical(snapshot(current["passes"][-1]))):
        raise DownloadBlocked("Approved identities or resource metadata changed since the baseline")
    now = current["passes"][-1]
    if len(now["assets"]) != 2 or len(now["masters"]) != 2:
        raise DownloadBlocked("This experiment requires exactly the two approved assets and masters")
    masters = {record["recordName"]: record for record in now["masters"]}
    plans = []
    seen_masters, seen_urls = set(), set()
    for asset in sorted(now["assets"], key=lambda record: record["recordName"]):
        master_id = asset["fields"].get("masterRef", {}).get("recordName")
        if master_id not in masters or master_id in seen_masters:
            raise DownloadBlocked("Asset/master mapping is missing or ambiguous")
        seen_masters.add(master_id)
        master = masters[master_id]
        candidates = [record for record in (asset, master) if "resOriginalRes" in record["fields"]]
        if len(candidates) != 1:
            raise DownloadBlocked("Original resource ownership is missing or ambiguous")
        owner = candidates[0]
        resource = owner["fields"]["resOriginalRes"]
        size = resource.get("size")
        if type(size) is not int or not 0 < size <= MAX_ORIGINAL_BYTES:
            raise DownloadBlocked("Original size is invalid or exceeds the bounded test limit")
        url = urls.get((owner["recordType"], owner["recordName"]))
        if not valid_media_url(url) or url in seen_urls:
            raise DownloadBlocked("An original resource URL is unavailable, unsafe or ambiguous")
        seen_urls.add(url)
        binding = {"account_id": account_id, "database": "private", "zone_id": current["zone_id"],
                   "album_id": current["album_id"], "asset_id": asset["recordName"],
                   "asset_change_tag": asset["recordChangeTag"], "master_id": master_id,
                   "master_change_tag": master["recordChangeTag"],
                   "resource_record_id": owner["recordName"], "resource_record_type": owner["recordType"],
                   "resource_field": "resOriginalRes", "resource_size": size,
                   "provider_checksum_opaque": resource["fileChecksum"],
                   "resource_file_type": owner["fields"].get("resOriginalFileType")}
        plans.append({"binding": binding, "url": url})
    return plans


def check_path(path):
    for item in (*reversed(path.parents), path):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise DownloadBlocked("Test storage contains a link or reparse point")


def open_new(path):
    check_path(path)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600), "wb")


def read_file(path):
    check_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise DownloadBlocked("Test resource is not a regular file")
    return os.fdopen(descriptor, "rb")


def hash_file(path):
    digest, count = hashlib.sha256(), 0
    with read_file(path) as stream:
        while chunk := stream.read(CHUNK_SIZE):
            count += len(chunk)
            digest.update(chunk)
    return count, digest.hexdigest()


def stream_original(session, url, path, expected_size, fetch=None, progress=None):
    if fetch is None:
        from pyicloud_ipd.services.photos import download_asset
        fetch = download_asset
    count, digest, completed = 0, hashlib.sha256(), False
    started = time.monotonic()
    response = None
    try:
        # The pinned provider opens the streamed GET. The outer guard permits
        # this exact original URL once, and blocks redirects to other URLs.
        response = fetch(session, url, 0)
        response.raise_for_status()
        if response.status_code not in (200, 206):
            raise DownloadBlocked("Original download did not return a complete byte response")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type in ("text/html", "application/json", "application/xml", "text/xml"):
            raise DownloadBlocked("Original download returned a document rather than media bytes")
        if response.status_code == 206 and response.headers.get("Content-Range") != f"bytes 0-{expected_size - 1}/{expected_size}":
            raise DownloadBlocked("Original download returned an incomplete or mismatched byte range")
        with open_new(path) as stream:
            try:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if time.monotonic() - started > DOWNLOAD_SECONDS:
                        raise DownloadBlocked("Original download exceeded the bounded test duration")
                    if not chunk:
                        continue
                    if count + len(chunk) > expected_size:
                        raise DownloadBlocked("Original download exceeded its declared byte count")
                    stream.write(chunk)
                    count += len(chunk)
                    digest.update(chunk)
                if count != expected_size:
                    raise DownloadBlocked("Original download was shorter than its declared byte count")
                completed = True
            finally:
                stream.flush()
                os.fsync(stream.fileno())
        return digest.hexdigest()
    finally:
        if response is not None:
            response.close()
        if progress:
            progress({"download_complete": completed, "downloaded_bytes": count,
                      "response_sha256": digest.hexdigest() if completed else None})


def run_downloads(session, plans, run, transport, write_json, fetch=None):
    check_path(run)
    first, second = run / "copy-a", run / "copy-b"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    total = sum(plan["binding"]["resource_size"] for plan in plans)
    if shutil.disk_usage(run).free < total * 2 + CHUNK_SIZE:
        raise DownloadBlocked("Insufficient space for both bounded test copies")
    manifest = {"schema": 1, "complete": False, "resources": [plan["binding"] for plan in plans]}
    write_json(run / "download-plan-private.json", manifest)
    if fetch is None:
        from requests import Request
        # Match Requests' URL normalization without making any network call.
        transport["approved_download_urls"] = {Request("GET", plan["url"]).prepare().url for plan in plans}
    else:
        transport["approved_download_urls"] = {plan["url"] for plan in plans}
    results = []
    for number, plan in enumerate(plans, 1):
        source, copy = first / f"original-{number:02d}.bin", second / f"original-{number:02d}.bin"
        result = {**plan["binding"], "copy_a": source.relative_to(run).as_posix(),
                  "copy_b": copy.relative_to(run).as_posix(), "copy_verified": False}
        def progress(value):
            result.update(value)
            write_json(run / f"download-{number:02d}-progress-private.json", result)
        response_hash = stream_original(session, plan["url"], source, plan["binding"]["resource_size"],
                                        fetch, progress)
        with read_file(source) as source_stream, open_new(copy) as copy_stream:
            shutil.copyfileobj(source_stream, copy_stream, length=CHUNK_SIZE)
            copy_stream.flush()
            os.fsync(copy_stream.fileno())
        # Reopen and read the full bytes of each copy independently.
        source_size, source_hash = hash_file(source)
        copy_size, copy_hash = hash_file(copy)
        if (source_size != plan["binding"]["resource_size"] or copy_size != source_size
                or response_hash != source_hash or source_hash != copy_hash):
            raise DownloadBlocked("Downloaded bytes and independently read copies did not match")
        result.update(copy_verified=True, copy_a_sha256=source_hash, copy_b_sha256=copy_hash)
        write_json(run / f"download-{number:02d}-verified-private.json", result)
        results.append(result)
    write_json(run / "download-manifest-private.json", {"schema": 1, "complete": True, "resources": results,
               "complete_cloud_backup": "NOT VERIFIED", "physical_drive_independence": "NOT VERIFIED"})
    return {"downloaded_originals": len(results), "downloaded_original_bytes": total,
            "independent_local_hashes_match": True, "original_byte_binding": "VERIFIED FOR TEST SET",
            "physical_drive_independence": "NOT VERIFIED", "provider_checksum_algorithm": "NOT VERIFIED"}
