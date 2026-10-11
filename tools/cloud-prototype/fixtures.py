"""Generate every byte and identity used by this experiment from scratch."""

from dataclasses import asdict
import hashlib
from pathlib import Path

from prototype import Asset, Binding, MockCloud, Resource, Scope, Snapshot, build_plan


class Fixture:
    def __init__(self, directory: Path):
        self.directory = directory
        self.roots = (directory / "replica-a" / "media", directory / "replica-b" / "media")
        self.state = directory / "state"
        for path in (*self.roots, self.state):
            path.mkdir(parents=True)
        self.scope = Scope("synthetic-account", "private", "synthetic-zone", "synthetic-owner")
        self.bindings: list[Binding] = []
        self.assets: list[Asset] = []
        definitions = (
            ("asset-full-id-0001", "photo", ("original", "adjusted", "sidecar")),
            ("asset-full-id-0002", "live", ("original", "live_motion", "adjusted")),
            ("asset-full-id-0003", "raw_pair", ("raw_original", "jpeg_companion", "alternative")),
        )
        for key, kind, roles in definitions:
            resources = []
            master = f"master-{key}"
            for role in roles:
                # Plain invented bytes: no real media or valid decoder evidence.
                content = f"synthetic bytes: {key}: {role}\n".encode()
                resource_id = f"resource-{key}-{role}"
                resource = Resource(resource_id, role, "version-1",
                                    hashlib.sha256(content).hexdigest(), len(content))
                resources.append(resource)
                # Duplicate basenames are intentional. Names cannot identify assets.
                relative = f"{key}/IMG_0001.{role}"
                for root in self.roots:
                    target = root / relative
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(content)
                self.bindings.append(Binding(key, master, resource_id, "version-1", relative, relative))
            self.assets.append(Asset(key, master, "asset-tag-1", "master-tag-1", kind, tuple(resources)))
        self.snapshot = Snapshot(self.scope, 10, tuple(self.assets))
        self.provider = MockCloud(self.snapshot)

    def plan(self, selected: tuple[str, ...] | None = None):
        return build_plan(self.snapshot, self.scope,
                          selected if selected is not None else tuple(a.id for a in self.assets),
                          tuple(self.bindings), self.roots, self.state)

    def export_review(self, plan) -> tuple[Path, Path]:
        """Write fresh review bundles outside each replica's media directory."""
        from prototype import canonical, write_new

        outputs = []
        for root in self.roots:
            output = root.parent / "review"
            output.mkdir()
            plan.save(output / "plan.json")
            audit = self.state / "audit.jsonl"
            if audit.exists():
                write_new(output / "audit.jsonl", audit.read_text(encoding="utf-8").rstrip("\n"))
            write_new(output / "limitations.json", canonical({
                "mode": "OFFLINE_SIMULATION_ONLY",
                "physical_drive_independence": "NOT VERIFIED",
                "live_cloud_coverage": "NOT VERIFIED",
                "synthetic_scope": asdict(self.scope),
            }))
            outputs.append(output)
        return tuple(outputs)
