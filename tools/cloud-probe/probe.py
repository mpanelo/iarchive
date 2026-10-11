"""Scoped cloud research: authentication, album metadata and two approved originals."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit


PIN = "1.32.3"
AUTH_REQUESTS = {
    ("POST", "idmsa.apple.com", "/appleauth/auth/signin/init"),
    ("POST", "idmsa.apple.com", "/appleauth/auth/signin/complete"),
    ("POST", "idmsa.apple.com", "/appleauth/auth/repair/complete"),
    ("POST", "idmsa.apple.com", "/appleauth/auth/verify/trusteddevice/securitycode"),
    ("PUT", "idmsa.apple.com", "/appleauth/auth/verify/trusteddevice/securitycode"),
    ("GET", "idmsa.apple.com", "/appleauth/auth"),
    ("PUT", "idmsa.apple.com", "/appleauth/auth/verify/phone"),
    ("POST", "idmsa.apple.com", "/appleauth/auth/verify/phone/securitycode"),
    ("GET", "idmsa.apple.com", "/appleauth/auth/2sv/trust"),
    ("POST", "setup.icloud.com", "/setup/ws/1/accountLogin"),
    ("POST", "setup.icloud.com", "/setup/ws/1/validate"),
}
ZONE_PATHS = {
    f"/database/1/com.apple.photos.cloud/production/{database}/zones/list"
    for database in ("private", "shared")
}


class ProbeBlocked(Exception):
    """Scope or evidence was insufficient. Do not expose upstream error text."""


def allowed_request(method: str, url: str, discover: bool, photo_root: str | None) -> bool:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        return False
    if (method.upper(), parsed.hostname, parsed.path) in AUTH_REQUESTS:
        return True
    if not discover or not photo_root:
        return False
    root = urlsplit(photo_root)
    # The Photos hostname must come from the authenticated service response.
    if (root.scheme != "https" or root.port not in (None, 443)
            or not root.hostname or not root.hostname.endswith(".icloud.com")
            or root.username or root.password or root.query or root.fragment):
        return False
    prefix = root.path.rstrip("/")
    return (method.upper() == "POST" and parsed.hostname == root.hostname
            and parsed.path in {prefix + path for path in ZONE_PATHS})


@contextmanager
def request_guard(discover: bool, album: bool = False, downloads: bool = False):
    from requests.adapters import HTTPAdapter

    send = HTTPAdapter.send
    state = {"photo_root": None, "approved_zone": None, "approved_album": None,
             "authentication_requests": 0, "zone_requests": 0,
             "album_metadata_requests": 0, "asset_metadata_requests": 0,
             "approved_download_urls": set(), "original_download_requests": 0}

    def guarded(adapter, request, **kwargs):
        kind = None
        try:
            parsed = urlsplit(request.url)
            if downloads and request.method.upper() == "GET" and request.url in state["approved_download_urls"]:
                from originals import valid_media_url
                if valid_media_url(request.url) and not request.body:
                    kind = "original_download_requests"
            elif allowed_request(request.method, request.url, discover, state["photo_root"]):
                kind = "authentication_requests" if (
                    request.method.upper(), parsed.hostname, parsed.path
                ) in AUTH_REQUESTS else "zone_requests"
            elif album and state["photo_root"]:
                from album import query_kind
                root = urlsplit(state["photo_root"])
                prefix = root.path.rstrip("/") + "/database/1/com.apple.photos.cloud/production/private"
                suffixes = {prefix + "/records/query": False,
                            prefix + "/internal/records/query/batch": True}
                # Reuse the zone route's origin/TLS checks, then check the exact body.
                origin_ok = allowed_request(request.method, parsed._replace(
                    path=prefix + "/zones/list").geturl(), True, state["photo_root"])
                if origin_ok and parsed.path in suffixes:
                    kind = query_kind(json.loads(request.body), state["approved_zone"],
                                      state["approved_album"], suffixes[parsed.path])
        except (ValueError, TypeError, KeyError, AttributeError):
            kind = None
        if not kwargs.get("verify", True) or kind is None:
            raise ProbeBlocked("A request outside the approved probe scope was blocked")
        state[kind] += 1
        if kind == "original_download_requests":
            state["approved_download_urls"].remove(request.url)
        # Guard adapter sends, including redirects, rather than only caller URLs.
        return send(adapter, request, **kwargs)

    with patch.object(HTTPAdapter, "send", guarded):
        yield state


def new_run(repo: Path) -> Path:
    base = repo / ".smoke"
    for path in (*reversed(base.parents), base):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ProbeBlocked("The output directory has a link or reparse ancestor")
    base.mkdir(exist_ok=True, mode=0o700)
    result = Path(tempfile.mkdtemp(prefix="cloud-probe-", dir=base))
    (result / "cookies").mkdir(mode=0o700)
    return result


def save_new(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def reuse_cookies(repo: Path, name: str) -> Path:
    if not re.fullmatch(r"cloud-probe-[a-z0-9_]+", name):
        raise ProbeBlocked("Invalid session run name")
    run = repo / ".smoke" / name
    cookies = run / "cookies"
    for path in (*reversed(cookies.parents), cookies, run / "summary.json"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ProbeBlocked("Session path contains a link or reparse point")
    for path in cookies.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ProbeBlocked("Session cache contains a link or reparse point")
    # Only inspect the sanitized summary, never cookie or session contents.
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    if (summary.get("schema") != 1 or summary.get("authenticated") is not True
            or summary.get("provider_version") != PIN):
        raise ProbeBlocked("Session lacks previous authentication evidence")
    return cookies


def read_baseline(repo: Path, name: str) -> dict:
    run = reuse_cookies(repo, name).parent
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    if (summary.get("scope") != "album" or summary.get("album_metadata_completed") is not True
            or summary.get("two_passes_match") is not True or summary.get("observed_assets") != 2):
        raise ProbeBlocked("Baseline is not the completed two-asset metadata experiment")
    path = run / "album-private.json"
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ProbeBlocked("Baseline metadata path contains a link or reparse point")
    value = json.loads(path.read_text(encoding="utf-8"))
    from originals import snapshot
    from album import canonical
    if len(value.get("passes", [])) != 2 or canonical(snapshot(value["passes"][0])) != canonical(snapshot(value["passes"][1])):
        raise ProbeBlocked("Baseline metadata passes are incomplete or inconsistent")
    return value


def discover_zones(service, photo_root: str) -> tuple[dict, list[str]]:
    zones = {}
    errors = []
    for database in ("private", "shared"):
        try:
            response = service.session.post(
                f"{photo_root.rstrip('/')}/database/1/com.apple.photos.cloud/production/{database}/zones/list",
                data="{}", headers={"Content-type": "text/plain"},
            )
            response.raise_for_status()
            value = response.json()
            if (not isinstance(value, dict) or value.get("serverErrorCode") or value.get("error")
                    or not isinstance(value.get("zones"), list)
                    or any(value.get(key) for key in ("moreComing", "continuationMarker", "continuationCursor"))):
                raise ProbeBlocked("Zone enumeration did not return a valid zone list")
            result = []
            seen = set()
            for zone in value["zones"]:
                identity = zone.get("zoneID")
                if (zone.get("serverErrorCode") or not isinstance(identity, dict)
                        or not isinstance(identity.get("zoneName"), str) or not identity["zoneName"]):
                    raise ProbeBlocked("Zone enumeration contained incomplete identities")
                # Preserve the complete returned identity; never infer an omitted owner.
                identity_key = json.dumps(identity, sort_keys=True)
                if identity_key in seen:
                    raise ProbeBlocked("Zone enumeration contained duplicate identities")
                seen.add(identity_key)
                result.append({"zone_id": identity, "deleted": zone.get("deleted")})
            zones[database] = result
        except Exception as error:
            # Do not serialize upstream messages, response bodies, or signed URLs.
            errors.append(f"{database}: {type(error).__name__}")
    return zones, errors


def verify_second_factor(service) -> None:
    from icloudpd import authentication

    # Reuse upstream delivery/validation, but hide codes and SMS choices locally.
    # It requests a trusted-device push and offers masked trusted phone numbers.
    with patch.object(authentication, "prompt_string",
                      lambda message: getpass.getpass(f"{message} (hidden, local only): ")):
        authentication.request_2fa(service, logging.getLogger("iarchive-cloud-probe"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=("auth", "libraries", "album", "originals"))
    parser.add_argument("--album", help="Exact name of the explicitly approved test album")
    parser.add_argument("--reuse-session", help="Authenticated cloud-probe run basename under .smoke")
    parser.add_argument("--baseline", help="Successful two-asset album metadata run under .smoke")
    args = parser.parse_args()
    if (args.scope in ("album", "originals")) != bool(args.album):
        parser.error("--album is required only for album and originals scopes")
    if (args.scope == "originals") != bool(args.baseline):
        parser.error("--baseline is required only for the originals scope")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Run this probe directly in your own interactive Terminal; credentials must stay local.")
        return 2
    if version("icloudpd") != PIN:
        print(f"This probe requires icloudpd {PIN}.")
        return 2
    phrase = "DOWNLOAD TWO ORIGINALS" if args.scope == "originals" else "READ ONLY " + args.scope.upper()
    if args.scope == "originals":
        print(f"Scope: download only the two baseline originals from {args.album!r} into a fresh .smoke run.")
        print("Create a second local copy and independently hash both. No Photos modifications or removal.")
    elif args.scope == "album":
        print(f"Scope: metadata only for the approved album {args.album!r}, at most 1000 assets.")
        print("Album-name discovery and two metadata passes. No media downloads or removal.")
    else:
        print(f"Scope: {args.scope}. No albums, photo records, media downloads, or removal requests.")
    if input(f"Type {phrase} to proceed: ").strip() != phrase:
        print("Scope confirmation did not match; no authentication attempted.")
        return 2
    # This helper deliberately bypasses keyring providers and uses upstream SRP/2FA.
    from pyicloud_ipd import PyiCloudService

    logging.disable(logging.CRITICAL)
    old_mask = os.umask(0o077)
    run = None
    transport = None
    report = {"schema": 1, "scope": args.scope, "provider_version": PIN,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "authenticated": False, "photos_service_available": False,
              "library_discovery_completed": False,
              "album_metadata_completed": False,
              "original_download_completed": False,
              "cloud_coverage": "NOT VERIFIED", "removal_feasibility": "NOT VERIFIED",
              "photo_inventory_requested": False, "media_download_requested": False}
    try:
        repo = Path(__file__).resolve().parents[2]
        run = new_run(repo)
        print(f"Private output directory: {run}")
        cookies = reuse_cookies(repo, args.reuse_session) if args.reuse_session else run / "cookies"
        baseline = read_baseline(repo, args.baseline) if args.baseline else None
        with request_guard(args.scope != "auth", args.scope in ("album", "originals"), args.scope == "originals") as transport:
            report["phase"] = "authentication"
            account = getpass.getpass("Apple account email (hidden, local only): ").strip()
            if not account:
                raise ProbeBlocked("Missing account")
            service = PyiCloudService(
                "com", account,
                lambda: getpass.getpass("Apple account password (local only): "),
                cookie_directory=str(cookies), with_family=False, http_timeout=30,
            )
            if service.requires_2fa:
                verify_second_factor(service)
            if service.requires_2sa:
                raise ProbeBlocked("Additional authentication is required")
            if not service.data.get("dsInfo"):
                raise ProbeBlocked("Authentication evidence is missing")
            report["authenticated"] = True
            if args.scope != "auth":
                report["phase"] = "library_discovery"
                photo_root = service._get_webservice_url("ckdatabasews")
                transport["photo_root"] = photo_root
                report["photos_service_advertised"] = True
                zones, errors = discover_zones(service, photo_root)
                report["photos_service_available"] = bool(zones)
                save_new(run / "zones-private.json", {"libraries": zones})
                report["library_counts"] = {key: len(value) for key, value in zones.items()}
                report["library_discovery_completed"] = not errors and set(zones) == {"private", "shared"}
                report["findings"] = errors
                report["owner_identity_returned"] = {
                    key: [bool(zone["zone_id"].get("ownerRecordName")) for zone in value]
                    for key, value in zones.items()
                }
            if args.scope in ("album", "originals"):
                from album import collect_album
                if not report["library_discovery_completed"]:
                    raise ProbeBlocked("Library discovery was incomplete")
                report["phase"] = "album_metadata"
                report["photo_inventory_requested"] = True
                def progress(pass_number, page_number, value):
                    save_new(run / f"album-pass-{pass_number}-page-{page_number}.json", value)
                original_urls = {} if args.scope == "originals" else None
                private, album_report = collect_album(service, photo_root, zones, args.album, transport, progress, original_urls)
                save_new(run / "album-private.json", private)
                report.update(album_report)
                if not report["two_passes_match"]:
                    raise ProbeBlocked("The two metadata passes differed")
                report["album_metadata_completed"] = True
            if args.scope == "originals":
                from originals import run_downloads, select_originals, snapshot
                from album import canonical, collect_pass
                identity = service.data["dsInfo"].get("dsid") or service.params.get("dsid")
                identity = str(identity) if isinstance(identity, (str, int)) and not isinstance(identity, bool) else None
                plans = select_originals(baseline, private, original_urls, identity)
                report["phase"] = "original_download"
                report["media_download_requested"] = True
                report.update(run_downloads(service.session, plans, run, transport, save_new))
                report["phase"] = "post_download_metadata"
                after = collect_pass(service, photo_root, private["zone_id"], private["album_id"],
                                     lambda page, value: progress(3, page, value))
                save_new(run / "post-download-private.json", after)
                if canonical(snapshot(after)) != canonical(snapshot(private["passes"][-1])):
                    raise ProbeBlocked("Album metadata changed during the original download test")
                report["post_download_metadata_matches"] = True
                report["original_download_completed"] = True
            report["phase"] = "completed"
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
    except (Exception, KeyboardInterrupt) as error:
        report["blocked"] = type(error).__name__
        from album import AlbumBlocked
        from originals import DownloadBlocked
        if isinstance(error, (ProbeBlocked, AlbumBlocked, DownloadBlocked)):
            report["finding"] = str(error)
        if isinstance(error, AlbumBlocked) and error.diagnostics:
            report["diagnostics"] = error.diagnostics
        print("Probe stopped. Upstream error details are suppressed to protect credentials.")
    finally:
        if run is not None:
            if transport is not None:
                report["request_counts"] = {key: transport[key] for key in (
                    "authentication_requests", "zone_requests", "album_metadata_requests", "asset_metadata_requests",
                    "original_download_requests")}
            save_new(run / "summary.json", report)
        os.umask(old_mask)
    print(json.dumps(report, indent=2))
    return int("blocked" in report or (args.scope != "auth" and not report["library_discovery_completed"]))


if __name__ == "__main__":
    sys.exit(main())
