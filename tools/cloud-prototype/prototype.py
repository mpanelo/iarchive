"""Offline policy experiment. Only MockCloud can execute simulated operations.

Resource identities, relationships, checksums, and inventory generations below
are invented fixture evidence, not capabilities established for iCloud Photos.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any


class Blocked(Exception):
    """Missing, uncertain, or changed evidence prevents further simulation."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Scope:
    account: str
    database: str
    zone: str
    owner: str


@dataclass(frozen=True)
class Resource:
    id: str
    role: str
    version: str
    synthetic_sha256: str
    size: int


@dataclass(frozen=True)
class Asset:
    id: str  # Full deletion-target ID; never a filename-derived abbreviation.
    master_id: str
    asset_tag: str
    master_tag: str
    kind: str
    resources: tuple[Resource, ...]
    complete: bool = True
    relationships_proven: bool = True
    captured_at: str = "2001-01-01"  # Deliberately irrelevant to selection.


@dataclass(frozen=True)
class Snapshot:
    scope: Scope
    generation: int
    active: tuple[Asset, ...]
    recently_deleted: tuple[Asset, ...] = ()
    complete: bool = True


@dataclass(frozen=True)
class Binding:
    asset_id: str
    master_id: str
    resource_id: str
    version: str
    first: str
    second: str


def safe_path(path: Path) -> Path:
    """Reject links/reparse points in every existing ancestor, without resolving."""
    absolute = Path(os.path.abspath(path))
    for part in (*reversed(absolute.parents), absolute):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Blocked(f"Unsafe link or reparse point: {part}")
    return absolute


def media_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or parts.is_absolute()
        or any(p in ("", ".", "..") for p in relative.split("/"))
    ):
        raise Blocked(f"Unsafe relative path: {relative!r}")
    return safe_path(root.joinpath(*parts.parts))


def hash_file(path: Path) -> tuple[int, str]:
    """Always read bytes anew. Metadata alone never satisfies verification."""
    safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Blocked(f"Not a regular file: {path}")
        hasher = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
        after = os.fstat(stream.fileno())
    current = safe_path(path).stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, f) != getattr(item, f) for item in (after, current) for f in fields):
        raise Blocked(f"File changed while hashing: {path}")
    return after.st_size, hasher.hexdigest()


def file_set(root: Path) -> set[str]:
    result: set[str] = set()
    def failed(error: OSError) -> None:
        raise Blocked(f"Inventory could not be completed: {error}") from error

    for base, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for name in (*dirs, *files):
            safe_path(Path(base) / name)
        for name in files:
            path = Path(base) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise Blocked(f"Unknown filesystem entry: {path}")
            result.add(path.relative_to(root).as_posix())
    return result


def disjoint(paths: tuple[Path, ...]) -> None:
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise Blocked("Media and state directories must be disjoint")
            if os.path.samefile(left, right):
                raise Blocked("Directories alias the same location")


def validate_snapshot(snapshot: Snapshot, scope: Scope) -> None:
    if snapshot.scope != scope or not all(asdict(scope).values()):
        raise Blocked("Account, database, zone, or owner changed")
    if not snapshot.complete or snapshot.generation < 0:
        raise Blocked("Inventory completeness/generation is uncertain")
    all_assets = (*snapshot.active, *snapshot.recently_deleted)
    ids = [asset.id for asset in all_assets]
    if any(not key for key in ids) or len(ids) != len(set(ids)):
        raise Blocked("Remote inventory has missing or duplicate full IDs")


def validate_asset(asset: Asset) -> None:
    required = {
        "photo": {"original"},
        "live": {"original", "live_motion"},
        "raw_pair": {"raw_original", "jpeg_companion"},
    }
    known = {"original", "adjusted", "alternative", "live_motion", "sidecar",
             "raw_original", "jpeg_companion"}
    roles = {resource.role for resource in asset.resources}
    if (
        not asset.complete or not asset.relationships_proven
        or not all((asset.id, asset.master_id, asset.asset_tag, asset.master_tag))
        or asset.kind not in required
        or not required[asset.kind] <= roles or not roles <= known
    ):
        raise Blocked(f"Incomplete or unknown resource evidence: {asset.id}")
    keys = [(r.id, r.version) for r in asset.resources]
    if len(keys) != len(set(keys)):
        raise Blocked(f"Duplicate resource identities: {asset.id}")
    for resource in asset.resources:
        if (
            not resource.id or not resource.version or resource.size <= 0
            or len(resource.synthetic_sha256) != 64
            or any(c not in "0123456789abcdef" for c in resource.synthetic_sha256)
        ):
            raise Blocked(f"Unknown or partial resource: {asset.id}/{resource.id}")


def verify_replicas(
    assets: tuple[Asset, ...], bindings: tuple[Binding, ...], roots: tuple[Path, Path]
) -> tuple[dict[str, Any], ...]:
    roots = (safe_path(roots[0]), safe_path(roots[1]))
    disjoint(roots)
    expected: dict[tuple[str, str, str, str], Resource] = {}
    for asset in assets:
        validate_asset(asset)
        for resource in asset.resources:
            expected[(asset.id, asset.master_id, resource.id, resource.version)] = resource
    actual = [(b.asset_id, b.master_id, b.resource_id, b.version) for b in bindings]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise Blocked("Every resource needs exactly one explicit full-ID binding")
    for index, field in enumerate(("first", "second")):
        paths = [getattr(b, field) for b in bindings]
        if len(paths) != len(set(paths)) or file_set(roots[index]) != set(paths):
            raise Blocked("Unmapped, missing, or ambiguously reused replica files")
    manifest = []
    for binding in sorted(bindings, key=lambda b: (b.asset_id, b.resource_id, b.version)):
        key = (binding.asset_id, binding.master_id, binding.resource_id, binding.version)
        resource = expected[key]
        hashes = [hash_file(media_path(root, path)) for root, path in
                  zip(roots, (binding.first, binding.second), strict=True)]
        if any(result != (resource.size, resource.synthetic_sha256) for result in hashes):
            raise Blocked(f"Replica bytes differ from synthetic evidence: {key}")
        manifest.append({"binding": asdict(binding), "resource": asdict(resource),
                         "first_sha256": hashes[0][1], "second_sha256": hashes[1][1]})
    return tuple(manifest)


@dataclass(frozen=True)
class Plan:
    # A canonical JSON string keeps every nested value immutable.
    payload: str
    sha256: str

    def data(self) -> dict[str, Any]:
        value = json.loads(self.payload)
        if canonical(value) != self.payload or digest(value) != self.sha256:
            raise Blocked("Plan seal changed")
        return value

    def save(self, path: Path) -> None:
        self.data()
        write_new(path, canonical({"payload": self.payload, "sha256": self.sha256}))

    @classmethod
    def load(cls, path: Path) -> Plan:
        value = json.loads(safe_path(path).read_text(encoding="utf-8"))
        plan = cls(value["payload"], value["sha256"])
        plan.data()
        return plan


def write_new(path: Path, content: str) -> None:
    safe_path(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def build_plan(
    snapshot: Snapshot, scope: Scope, selected: tuple[str, ...],
    bindings: tuple[Binding, ...], roots: tuple[Path, Path], state: Path,
) -> Plan:
    try:
        validate_snapshot(snapshot, scope)
        disjoint((safe_path(roots[0]), safe_path(roots[1]), safe_path(state)))
        if not selected or len(set(selected)) != len(selected):
            raise Blocked("Selection must contain unique full asset IDs")
        available = {asset.id: asset for asset in snapshot.active}
        if not set(selected) <= available.keys():
            raise Blocked("Selection contains unavailable or abbreviated asset IDs")
        # The experiment inventories all fixture bytes, including non-targets.
        manifest = verify_replicas(snapshot.active, bindings, roots)
        targets = [asdict(available[key]) for key in sorted(selected)]
        value = {
            "schema": 1, "mode": "OFFLINE_SIMULATION_ONLY", "scope": asdict(scope),
            "snapshot": asdict(snapshot), "targets": targets,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "count": len(targets),
            "bytes": sum(r.size for key in selected for r in available[key].resources),
            "manifest": manifest, "manifest_sha256": digest(manifest),
            "roots": [str(safe_path(root)) for root in roots],
            "state": str(safe_path(state)),
            "physical_drive_independence": "NOT VERIFIED",
            "live_cloud_coverage": "NOT VERIFIED",
        }
        return Plan(canonical(value), digest(value))
    except (OSError, ValueError) as error:
        raise Blocked(f"Evidence unavailable: {error}") from error


def approval_phrase(plan: Plan) -> str:
    return f"SIMULATE {plan.data()['count']} {plan.sha256}"


def asset_from(value: dict[str, Any]) -> Asset:
    return Asset(**{**value, "resources": tuple(Resource(**r) for r in value["resources"])})


class MockCloud:
    """Memory-only provider with stronger guarantees than established upstream."""

    def __init__(self, snapshot: Snapshot):
        self.scope = snapshot.scope
        self.generation = snapshot.generation
        self.active = {a.id: a for a in snapshot.active}
        self.recently_deleted = {a.id: a for a in snapshot.recently_deleted}
        self.complete = snapshot.complete
        self.authenticated = True
        self.faults: dict[str, str] = {}
        self.calls: list[str] = []

    def snapshot(self) -> Snapshot:
        if not self.authenticated:
            raise Blocked("Mock credentials expired")
        return Snapshot(self.scope, self.generation,
                        tuple(sorted(self.active.values(), key=lambda a: a.id)),
                        tuple(sorted(self.recently_deleted.values(), key=lambda a: a.id)),
                        self.complete)

    def simulate_move(self, expected: Asset, generation: int) -> dict[str, str]:
        # Generation and both tags/resources are checked in the same mock step.
        # An inventory read followed by an unconditional request is insufficient.
        if not self.authenticated:
            raise Blocked("Mock credentials expired")
        if self.generation != generation or self.active.get(expected.id) != expected:
            raise Blocked("Mock conditional operation rejected changed evidence")
        self.calls.append(expected.id)
        fault = self.faults.get(expected.id)
        if fault == "timeout_before":
            raise TimeoutError("Mock response lost before application")
        if fault == "record_error":
            return {"id": expected.id, "error": "CONFLICT"}
        if fault == "success_without_change":
            return {"id": expected.id, "status": "ok"}
        self.recently_deleted[expected.id] = self.active.pop(expected.id)
        self.generation += 1
        if fault == "timeout_after":
            raise TimeoutError("Mock response lost after application")
        if fault == "interrupt_after":
            raise KeyboardInterrupt("Mock interrupted after application")
        return {"id": "another-id" if fault == "wrong_response" else expected.id,
                "status": "ok"}


class Journal:
    def __init__(self, path: Path, plan: Plan, ids: set[str]):
        self.path, self.plan, self.ids = path, plan, ids
        self.events: list[dict[str, Any]] = []
        if path.exists():
            for line in safe_path(path).read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                    if event["plan_sha256"] != plan.sha256 or event["id"] not in ids:
                        raise Blocked("Audit belongs to a different plan or asset set")
                    previous = self.states().get(event["id"])
                    allowed = {None: {"attempted"}, "attempted": {"unknown", "failed", "confirmed"},
                               "unknown": {"confirmed"}, "failed": set(), "confirmed": set()}
                    if event["status"] not in allowed[previous]:
                        raise Blocked("Audit has an invalid state transition")
                    self.events.append(event)
                except (ValueError, KeyError) as error:
                    raise Blocked("Interrupted or malformed audit requires review") from error

    def states(self) -> dict[str, str]:
        return {event["id"]: event["status"] for event in self.events}

    def append(self, key: str, status: str, detail: str) -> None:
        event = {"plan_sha256": self.plan.sha256, "id": key, "status": status,
                 "detail": detail, "at": datetime.now(timezone.utc).isoformat()}
        safe_path(self.path.parent)
        if self.path.exists():
            safe_path(self.path)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)


def check_remote(plan: dict[str, Any], snapshot: Snapshot, done: set[str]) -> None:
    validate_snapshot(snapshot, Scope(**plan["scope"]))
    baseline = plan["snapshot"]
    expected_active = {a["id"]: a for a in baseline["active"] if a["id"] not in done}
    expected_deleted = {a["id"]: a for a in baseline["recently_deleted"]}
    expected_deleted.update({a["id"]: a for a in plan["targets"] if a["id"] in done})
    # asdict tuples become lists after JSON round-trip; compare canonically.
    if (
        snapshot.generation != baseline["generation"] + len(done)
        or canonical({a.id: asdict(a) for a in snapshot.active}) != canonical(expected_active)
        or canonical({a.id: asdict(a) for a in snapshot.recently_deleted}) != canonical(expected_deleted)
    ):
        raise Blocked("Remote inventory or resources changed; create a new plan")


def execute_simulation(plan: Plan, provider: MockCloud, approval: str) -> dict[str, Any]:
    try:
        return _execute_simulation(plan, provider, approval)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Blocked(f"Simulation evidence unavailable: {error}") from error


def _execute_simulation(plan: Plan, provider: MockCloud, approval: str) -> dict[str, Any]:
    if type(provider) is not MockCloud:
        raise Blocked("Only the in-memory mock is supported")
    data = plan.data()
    if data["mode"] != "OFFLINE_SIMULATION_ONLY" or approval != approval_phrase(plan):
        raise Blocked("Exact simulation count and plan digest must be approved")
    roots = tuple(Path(path) for path in data["roots"])
    state = Path(data["state"])
    disjoint((*[safe_path(root) for root in roots], safe_path(state)))
    assets = tuple(asset_from(a) for a in data["snapshot"]["active"])
    bindings = tuple(Binding(**entry["binding"]) for entry in data["manifest"])
    journal = Journal(state / "audit.jsonl", plan, {a["id"] for a in data["targets"]})
    done = {key for key, status in journal.states().items() if status == "confirmed"}
    # Re-read all replica bytes at every start/resume, even with preserved mtimes.
    manifest = verify_replicas(assets, bindings, roots)
    if digest(manifest) != data["manifest_sha256"]:
        raise Blocked("Replica manifest changed")
    for key, status in journal.states().items():
        if status in ("attempted", "unknown"):
            # Prove the precise expected postcondition before resolving a lost reply.
            check_remote(data, provider.snapshot(), done | {key})
            journal.append(key, "confirmed", "Reconciled exact mock Recently Deleted record")
            done.add(key)
        elif status != "confirmed":
            raise Blocked("Failed audit entry requires review and a new plan")
    check_remote(data, provider.snapshot(), done)
    for target in data["targets"]:
        key = target["id"]
        if key in done:
            continue
        # Rehash and re-list immediately before each conditional mock operation.
        if digest(verify_replicas(assets, bindings, roots)) != data["manifest_sha256"]:
            raise Blocked("Replica manifest changed")
        snapshot = provider.snapshot()
        check_remote(data, snapshot, done)
        journal.append(key, "attempted", "Intent persisted before mock operation")
        try:
            response = provider.simulate_move(asset_from(target), snapshot.generation)
        except (TimeoutError, Blocked) as error:
            journal.append(key, "unknown", str(error))
            raise Blocked("Mock outcome uncertain; reconcile, never retry blindly") from error
        if response.get("error"):
            journal.append(key, "failed", response["error"])
            raise Blocked("Mock per-record failure; no later target was attempted")
        if response != {"id": key, "status": "ok"}:
            journal.append(key, "unknown", "Missing or mismatched per-record response")
            raise Blocked("Mock response cannot prove the target outcome")
        try:
            check_remote(data, provider.snapshot(), done | {key})
        except Blocked:
            journal.append(key, "unknown", "Exact postcondition could not be verified")
            raise
        journal.append(key, "confirmed", "Exact remaining and Recently Deleted inventories checked")
        done.add(key)
    check_remote(data, provider.snapshot(), done)
    return {"mode": data["mode"], "simulated_count": len(done),
            "plan_sha256": plan.sha256, "simulated_postconditions_checked": True,
            "physical_drive_independence": "NOT VERIFIED", "live_cloud_coverage": "NOT VERIFIED"}
