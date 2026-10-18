"""Offline cleanup demo. No accounts, external requests, or real media inputs."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import uuid

# Share the existing offline evidence checks, not its conditional cloud mock.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud-prototype"))
from fixtures import Fixture
from prototype import (
    Blocked, Plan, Snapshot, Scope, asset_from, Binding, build_plan, canonical,
    digest, disjoint, hash_file, safe_path, validate_snapshot, verify_replicas, write_new,
)


MODE = "OFFLINE_SUPERVISED_CLEANUP"
LIMITS = {
    "live_execution": "UNAVAILABLE",
    "physical_drive_independence": "NOT VERIFIED",
    "cloud_coverage": "NOT VERIFIED",
    "server_version_condition": "NOT VERIFIED",
    "recovery_and_space_reclaimed": "NOT VERIFIED",
}


class UnconditionalCloud:
    """Memory only: accepts IDs without an atomic version or inventory condition."""

    def __init__(self, snapshot: Snapshot):
        self.scope = snapshot.scope
        self.active = {a.id: a for a in snapshot.active}
        self.trash = {a.id: a for a in snapshot.recently_deleted}
        self.complete = snapshot.complete
        self.authenticated = True
        self.calls: list[str] = []
        self.faults: dict[str, str] = {}

    def snapshot(self) -> Snapshot:
        if not self.authenticated:
            raise Blocked("Mock session is unavailable")
        return Snapshot(self.scope, 0, tuple(self.active.values()), tuple(self.trash.values()), self.complete)

    def remove(self, key: str) -> dict[str, str]:
        # Deliberately no version check. Preflight cannot close the write race.
        self.calls.append(key)
        fault = self.faults.get(key)
        if fault == "timeout_before":
            raise TimeoutError("Mock reply lost before application")
        if fault == "record_error":
            return {"id": key, "error": "CONFLICT"}
        if key not in self.active:
            return {"id": key, "error": "NOT_FOUND"}
        self.trash[key] = self.active.pop(key)
        if fault == "timeout_after":
            raise TimeoutError("Mock reply lost after application")
        if fault == "interrupt_after":
            raise KeyboardInterrupt
        return {"id": "wrong-id" if fault == "wrong_response" else key, "status": "ok"}


def data_for(plan: Plan) -> dict:
    data = plan.data()
    if data.get("mode") != MODE or data.get("schema") != 1 or data.get("limits") != LIMITS:
        raise Blocked("Only a supervised offline cleanup plan is supported")
    targets = data["targets"]
    ids = [a["id"] for a in targets]
    if not ids or len(ids) != len(set(ids)) or data["count"] != len(ids):
        raise Blocked("Plan target count or identities changed")
    active = {a["id"]: a for a in data["snapshot"]["active"]}
    if any(active.get(a["id"]) != a for a in targets):
        raise Blocked("Plan targets differ from their captured album records")
    if data["manifest_sha256"] != digest(data["manifest"]):
        raise Blocked("Plan manifest digest changed")
    return data


def locations(data: dict) -> tuple[Path, ...]:
    roots = tuple(safe_path(Path(p)) for p in data["roots"])
    outputs = tuple(safe_path(Path(p)) for p in data["outputs"])
    if len(roots) != 2 or len(outputs) != 3:
        raise Blocked("Two media roots and three journal locations are required")
    disjoint((*roots, *outputs))
    return outputs


def prepare(fixture: Fixture) -> Plan:
    """Freeze the fixture album and persist the plan beside both backups."""
    selected = tuple(a.id for a in fixture.snapshot.active)
    core = build_plan(fixture.snapshot, fixture.scope, selected,
                      tuple(fixture.bindings), fixture.roots, fixture.state).data()
    outputs = (fixture.state, *(root.parent / "cleanup" for root in fixture.roots))
    for path in outputs[1:]:
        path.mkdir()
    core.update(mode=MODE, album_id="synthetic-album", album_name="Synthetic cleanup album",
                outputs=[str(p) for p in outputs], limits=dict(LIMITS))
    plan = Plan(canonical(core), digest(core))
    for output in locations(data_for(plan)):
        plan.save(output / "plan.json")
        (output / "journal").mkdir()
    write_new(fixture.state / "review.html", preview(plan))
    return plan


def confirmation(plan: Plan) -> str:
    return f"SIMULATE CLEANUP {data_for(plan)['count']} {plan.sha256}"


def preview(plan: Plan) -> str:
    from review import render
    return render(data_for(plan), confirmation(plan))


class ReviewSession:
    """Serialize selection edits and Terminal execution against the current plan."""

    def __init__(self, plan: Plan, provider: UnconditionalCloud):
        self.base = plan
        self.current: Plan | None = plan
        self.provider = provider
        self.selected = tuple(a["id"] for a in data_for(plan)["targets"])
        self.revision = 0
        self.phase = "reviewing"
        self.lock = threading.RLock()

    def state(self) -> dict:
        from review import cards
        with self.lock:
            data = data_for(self.base)
            assets = data["snapshot"]["active"]
            return {"revision": self.revision, "selected": list(self.selected),
                    "count": len(self.selected), "total": len(assets),
                    "bytes": sum(r["size"] for a in assets if a["id"] in self.selected for r in a["resources"]),
                    "saved": self.current is not None, "phase": self.phase,
                    "confirmation": confirmation(self.current) if self.current is not None and self.phase == "reviewing" else None,
                    "cards": cards(data), "roots": data["roots"]}

    def editable(self, revision: int) -> None:
        if self.phase != "reviewing":
            raise Blocked("This review is closed; inspect the retained result and journal")
        if type(revision) is not int or revision != self.revision:
            raise Blocked("Selection changed in another request; refresh the page")

    def draft(self, revision: int, selected: list[str]) -> dict:
        with self.lock:
            self.editable(revision)
            allowed = {a["id"] for a in data_for(self.base)["snapshot"]["active"]}
            if (not isinstance(selected, list) or any(type(key) is not str for key in selected)
                    or len(selected) != len(set(selected)) or not set(selected) <= allowed):
                raise Blocked("Selection must contain unique full IDs from this review")
            selected = tuple(sorted(selected))
            if selected != tuple(sorted(self.selected)):
                previous, self.current = self.current, None
                # Retire the old confirmation before acknowledging any edit.
                try:
                    if previous is not None:
                        for output in locations(data_for(previous)):
                            if list(safe_path(output / "journal").iterdir()):
                                raise Blocked("A started plan cannot be edited")
                            write_new(output / "superseded.json", canonical({"plan_sha256": previous.sha256}))
                except (OSError, Blocked):
                    self.phase = "stopped"
                    raise Blocked("Could not retire every plan copy; review stopped")
                self.selected = selected
                self.revision += 1
            return self.state()

    def save(self, revision: int) -> dict:
        with self.lock:
            self.editable(revision)
            if not self.selected:
                raise Blocked("Nothing is selected. Keep all items and cancel in Terminal, or select an item")
            if self.current is None:
                data = data_for(self.base)
                try:
                    check_album(data, self.provider.snapshot(), set())
                    snapshot = Snapshot(Scope(**data["scope"]), data["snapshot"]["generation"],
                                        tuple(asset_from(a) for a in data["snapshot"]["active"]),
                                        tuple(asset_from(a) for a in data["snapshot"]["recently_deleted"]))
                    bindings = tuple(Binding(**r["binding"]) for r in data["manifest"])
                    revised = build_plan(snapshot, snapshot.scope, self.selected, bindings,
                                         tuple(Path(p) for p in data["roots"]), Path(data["state"])).data()
                    revision_name = uuid.uuid4().hex
                    outputs = []
                    for output in locations(data):
                        revisions = output / "revisions"
                        revisions.mkdir(exist_ok=True)
                        safe_path(revisions)
                        destination = revisions / revision_name
                        destination.mkdir()
                        outputs.append(str(destination))
                    revised.update(mode=MODE, album_id=data["album_id"], album_name=data["album_name"],
                                   outputs=outputs, limits=dict(LIMITS), parent_plan_sha256=self.base.sha256)
                    candidate = Plan(canonical(revised), digest(revised))
                    for output in locations(data_for(candidate)):
                        candidate.save(output / "plan.json")
                        (output / "journal").mkdir()
                    write_new(Path(outputs[0]) / "review.html", preview(candidate))
                except (OSError, ValueError, Blocked):
                    self.phase = "stopped"
                    raise Blocked("Selection could not be verified and saved at every location; review stopped")
                self.current = candidate
            return self.state()

    def execute(self, approval: str) -> dict:
        with self.lock:
            self.editable(self.revision)
            if self.current is None:
                raise Blocked("Selection has unsaved changes. Save it and copy the new confirmation")
            if approval != confirmation(self.current):
                raise Blocked("Confirmation does not match the latest saved selection")
            self.phase = "executing"
            try:
                result = simulate(self.current, self.provider, approval)
            except (Blocked, KeyboardInterrupt):
                self.phase = "stopped"
                raise
            self.phase = "completed"
            return result

    def close(self) -> None:
        with self.lock:
            if self.phase == "reviewing":
                self.phase = "cancelled"


def check_album(data: dict, snapshot: Snapshot, done: set[str]) -> None:
    validate_snapshot(snapshot, Scope(**data["scope"]))
    original = data["snapshot"]
    active = {a["id"]: a for a in original["active"] if a["id"] not in done}
    trash = {a["id"]: a for a in original["recently_deleted"]}
    trash.update({a["id"]: a for a in data["targets"] if a["id"] in done})
    if (canonical(active) != canonical({a.id: asdict(a) for a in snapshot.active})
            or canonical(trash) != canonical({a.id: asdict(a) for a in snapshot.recently_deleted})):
        raise Blocked("Album membership, versions or resource evidence changed")


def check_backups(data: dict) -> None:
    assets = tuple(asset_from(a) for a in data["snapshot"]["active"])
    bindings = tuple(Binding(**r["binding"]) for r in data["manifest"])
    manifest = verify_replicas(assets, bindings, tuple(Path(p) for p in data["roots"]))
    if digest(manifest) != data["manifest_sha256"]:
        raise Blocked("Fresh backup verification differs from the plan")


class Journal:
    """Identical immutable event files; all copies must flush before a request."""

    def __init__(self, outputs: tuple[Path, ...], plan: Plan):
        self.outputs, self.plan = outputs, plan
        self.number = 0
        self.previous = None

    def append(self, key: str | None, status: str, detail: str) -> None:
        event = {"sequence": self.number + 1, "previous_sha256": self.previous,
                 "plan_sha256": self.plan.sha256, "asset_id": key, "status": status,
                 "detail": detail, "at": datetime.now(timezone.utc).isoformat()}
        for output in self.outputs:
            directory = safe_path(output / "journal")
            write_new(directory / f"{event['sequence']:06d}.json", canonical(event))
        self.previous, self.number = digest(event), event["sequence"]


def simulate(plan: Plan, provider: UnconditionalCloud, approval: str) -> dict:
    try:
        return _simulate(plan, provider, approval)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Blocked("Offline cleanup evidence could not be read or persisted") from error


def _simulate(plan: Plan, provider: UnconditionalCloud, approval: str) -> dict:
    if type(provider) is not UnconditionalCloud:
        raise Blocked("Only the in-memory unconditional mock is supported")
    data = data_for(plan)
    if approval != confirmation(plan):
        raise Blocked("Exact simulation count and full plan digest must be confirmed")
    outputs = locations(data)
    saved = canonical({"payload": plan.payload, "sha256": plan.sha256}) + "\n"
    expected = (len(saved.encode()), hashlib.sha256(saved.encode()).hexdigest())
    for output in outputs:
        try:
            (output / "superseded.json").lstat()
        except FileNotFoundError:
            pass
        else:
            raise Blocked("This plan was superseded; use the latest saved selection")
        if hash_file(output / "plan.json") != expected:
            raise Blocked("A saved review plan changed")
        if list(safe_path(output / "journal").iterdir()):
            raise Blocked("This plan was already started; inspect its journal, never retry blindly")
    check_backups(data)
    check_album(data, provider.snapshot(), set())
    journal = Journal(outputs, plan)
    journal.append(None, "started", "Exact frozen plan confirmed; mock execution only")
    done: set[str] = set()
    for target in data["targets"]:
        key = target["id"]
        try:
            check_backups(data)
            check_album(data, provider.snapshot(), done)
        except (Blocked, OSError):
            journal.append(key, "blocked", "Preflight failed; no request for this target")
            raise
        journal.append(key, "attempted", "Intent flushed at every journal location before request")
        try:
            response = provider.remove(key)
        except (TimeoutError, OSError, Blocked, KeyboardInterrupt) as error:
            journal.append(key, "unknown", "Reply unavailable; inspect outcome, no retry or later request")
            raise Blocked("Mock outcome is unknown; execution stopped without retry") from error
        if response in ({"id": key, "error": "CONFLICT"}, {"id": key, "error": "NOT_FOUND"}):
            journal.append(key, "failed", "Exact target returned a per-record error")
            raise Blocked("Mock per-record error; no later target attempted")
        if response != {"id": key, "status": "ok"}:
            journal.append(key, "unknown", "Reply did not establish exact target success")
            raise Blocked("Mock response is uncertain; execution stopped without retry")
        try:
            check_album(data, provider.snapshot(), done | {key})
        except (Blocked, OSError):
            journal.append(key, "unknown", "Reply accepted but exact postcondition not established")
            raise
        journal.append(key, "confirmed", "Exact mock remaining and Recently Deleted records checked")
        done.add(key)
    # This is a simulated postcondition, not a provider recovery/storage promise.
    result = {"mode": MODE, "plan_sha256": plan.sha256, "simulated_count": len(done),
              "simulated_postconditions_checked": True, "limits": dict(LIMITS)}
    journal.append(None, "completed", "Every frozen mock target has a checked result")
    for output in outputs:
        write_new(output / "result.json", canonical(result))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true", help="Ask for confirmation after generating the preview")
    args = parser.parse_args()
    # No input paths or accounts: every byte comes from newly generated fixtures.
    directory = Path(tempfile.mkdtemp(prefix="iarchive-cleanup-", dir=Path(tempfile.gettempdir()).resolve()))
    print(f"Offline cleanup demo: {directory}")
    try:
        fixture = Fixture(directory)
        plan = prepare(fixture)
        print(f"Frozen selection: {plan.data()['count']} mock assets. Local copies will stay untouched.")
        if not args.simulate:
            print(f"Open the saved preview: {(fixture.state / 'review.html').as_uri()}")
            print("Preview only. No mock removal requested.")
            return 0
        from review import serve
        session = ReviewSession(plan, UnconditionalCloud(fixture.snapshot))
        try:
            with serve(session) as url:
                print(f"Open the review: {url}")
                print("Uncheck items to keep, save your selection, then copy its confirmation here. Blank cancels.")
                while True:
                    answer = input("> ")
                    if not answer:
                        print("Cancelled. No further mock removal requested.")
                        return 0
                    try:
                        result = session.execute(answer)
                    except Blocked as error:
                        if session.phase != "reviewing":
                            raise
                        print(f"Not confirmed: {error}. Review stays open; copy its current confirmation or press Enter to cancel.")
                        continue
                    break
        finally:
            session.close()
        print(json.dumps(result, indent=2))
        return 0
    except (Blocked, OSError, ValueError, EOFError) as error:
        print(f"Stopped: {error}. Inspect the retained plan and journal.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled. Inspect the retained plan and journal before another attempt.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
