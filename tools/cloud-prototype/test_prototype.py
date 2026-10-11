"""Policy regressions using only fresh synthetic directories and an offline mock."""

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixtures import Fixture
from prototype import (Blocked, MockCloud, Plan,
                       approval_phrase, canonical, execute_simulation, hash_file)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="iarchive-cloud-test-",
                                                dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(Path(self.temp.name))

    def run_plan(self, plan=None):
        plan = plan or self.fixture.plan()
        return execute_simulation(plan, self.fixture.provider, approval_phrase(plan))

    def replace_first(self, asset):
        self.fixture.assets[0] = asset
        self.fixture.snapshot = replace(self.fixture.snapshot, active=tuple(self.fixture.assets))
        self.fixture.provider = MockCloud(self.fixture.snapshot)

    def test_exact_variants_and_duplicate_names_survive_simulation(self):
        before = {str(p): hash_file(p) for root in self.fixture.roots for p in root.rglob("*") if p.is_file()}
        plan = self.fixture.plan()
        self.assertEqual(len(plan.data()["manifest"]), 9)
        result = self.run_plan(plan)
        self.assertEqual(result["simulated_count"], 3)
        self.assertEqual(result["live_cloud_coverage"], "NOT VERIFIED")
        self.assertEqual(result["physical_drive_independence"], "NOT VERIFIED")
        self.assertEqual(len(self.fixture.provider.recently_deleted), 3)
        self.assertEqual(self.fixture.provider.active, {})
        after = {str(p): hash_file(p) for root in self.fixture.roots for p in root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_only_explicit_allowlist_is_simulated_and_resume_is_idempotent(self):
        key = self.fixture.assets[1].id
        plan = self.fixture.plan((key,))
        self.run_plan(plan)
        self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [key])
        self.assertEqual(set(self.fixture.provider.active), {self.fixture.assets[0].id, self.fixture.assets[2].id})

    def test_incomplete_inventory_blocks_plan(self):
        self.fixture.snapshot = replace(self.fixture.snapshot, complete=False)
        with self.assertRaises(Blocked):
            self.fixture.plan()

    def test_duplicate_full_ids_block_plan(self):
        self.fixture.snapshot = replace(self.fixture.snapshot,
                                        active=(self.fixture.assets[0], self.fixture.assets[0]))
        with self.assertRaises(Blocked):
            self.fixture.plan()

    def test_abbreviations_names_dates_and_empty_selection_are_not_identity(self):
        for selected in (("asset-f",), ("IMG_0001.original",), ("2001-01-01",), (),
                         (self.fixture.assets[0].id, self.fixture.assets[0].id)):
            with self.subTest(selected=selected), self.assertRaises(Blocked):
                self.fixture.plan(selected)

    def test_unknown_or_unproven_resource_evidence_blocks_plan(self):
        asset = self.fixture.assets[0]
        changes = (replace(asset, complete=False), replace(asset, relationships_proven=False),
                   replace(asset, master_id=""), replace(asset, master_tag=""),
                   replace(asset, resources=asset.resources[1:]),
                   replace(asset, resources=asset.resources + (replace(asset.resources[0], id="unknown", role="unknown"),)),
                   replace(asset, resources=asset.resources + (asset.resources[0],)),
                   replace(asset, resources=(replace(asset.resources[0], size=0), *asset.resources[1:])),
                   replace(asset, resources=(replace(asset.resources[0], synthetic_sha256="opaque-checksum"), *asset.resources[1:])))
        for changed in changes:
            self.replace_first(changed)
            with self.subTest(asset=changed), self.assertRaises(Blocked):
                self.fixture.plan()
        self.assertEqual(self.fixture.provider.calls, [])

    def test_missing_live_motion_and_raw_companion_block_plan(self):
        for index, role in ((1, "live_motion"), (2, "jpeg_companion")):
            original = self.fixture.assets[index]
            changed = replace(original, resources=tuple(r for r in original.resources if r.role != role))
            assets = list(self.fixture.assets)
            assets[index] = changed
            self.fixture.snapshot = replace(self.fixture.snapshot, active=tuple(assets))
            with self.subTest(role=role), self.assertRaises(Blocked):
                self.fixture.plan()

    def test_full_master_and_resource_version_bindings_are_required(self):
        original = self.fixture.bindings[0]
        for change in (replace(original, master_id="different-master"),
                       replace(original, version="stale-version"),
                       replace(original, resource_id="short-id")):
            self.fixture.bindings[0] = change
            with self.subTest(binding=change), self.assertRaises(Blocked):
                self.fixture.plan()

    def test_duplicate_or_missing_bindings_are_rejected(self):
        original = list(self.fixture.bindings)
        for values in (original[1:], original + [original[0]],
                       [replace(original[1], first=original[0].first), *original[1:]]):
            self.fixture.bindings = values
            with self.subTest(values=values), self.assertRaises(Blocked):
                self.fixture.plan()

    def test_same_name_size_and_mtime_with_different_bytes_blocks_execution(self):
        plan = self.fixture.plan()
        path = self.fixture.roots[1] / self.fixture.bindings[0].second
        original = path.stat()
        path.write_bytes(b"x" * original.st_size)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_missing_replica_is_a_finding(self):
        path = self.fixture.roots[1] / self.fixture.bindings[0].second
        path.unlink()
        with self.assertRaises(Blocked):
            self.fixture.plan()

    def test_disconnected_replica_blocks_execution(self):
        plan = self.fixture.plan()
        root = self.fixture.roots[1]
        root.rename(root.parent / "disconnected-synthetic")
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_unmapped_sidecar_or_partial_file_blocks_plan(self):
        for name in ("missing-sidecar.xmp", "download.partial"):
            path = self.fixture.roots[0] / name
            path.write_text("synthetic extra variant")
            with self.subTest(name=name), self.assertRaises(Blocked):
                self.fixture.plan()
            path.unlink()

    def test_unreadable_replica_blocks_plan(self):
        with patch("prototype.hash_file", side_effect=PermissionError("synthetic unreadable")):
            with self.assertRaises(Blocked):
                self.fixture.plan()

    def test_traversal_and_windows_absolute_paths_are_rejected(self):
        original = self.fixture.bindings[0]
        for name in ("../outside", "/outside", "C:/outside", "folder\\outside", "a/./b", "a//b"):
            self.fixture.bindings[0] = replace(original, first=name)
            with self.subTest(path=name), self.assertRaises(Blocked):
                self.fixture.plan()

    def test_symlink_to_generated_file_is_rejected(self):
        target = self.fixture.directory / "outside-synthetic"
        target.write_text("fixture only")
        path = self.fixture.roots[0] / "link"
        try:
            path.symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlink privilege unavailable: {error}")
        with self.assertRaises(Blocked):
            self.fixture.plan()

    def test_windows_reparse_attribute_is_rejected(self):
        from prototype import safe_path

        original = Path.lstat
        root = self.fixture.roots[0]
        class ReparseStat:
            st_mode = 0o40755
            st_file_attributes = 0x400

        def mocked(path):
            return ReparseStat() if path == root else original(path)

        with patch.object(Path, "lstat", mocked), self.assertRaises(Blocked):
            safe_path(root)

    def test_roots_and_state_must_be_disjoint(self):
        original = self.fixture.state
        self.fixture.state = self.fixture.roots[0]
        with self.assertRaises(Blocked):
            self.fixture.plan()
        self.fixture.state = original
        self.fixture.roots = (self.fixture.roots[0], self.fixture.roots[0])
        with self.assertRaises(Blocked):
            self.fixture.plan()

    def test_sealed_plan_round_trips_and_rejects_edited_payload(self):
        plan = self.fixture.plan()
        path = self.fixture.state / "plan.json"
        plan.save(path)
        self.assertEqual(Plan.load(path), plan)
        data = plan.data()
        data["count"] += 1
        path.write_text(canonical({"payload": canonical(data), "sha256": plan.sha256}))
        with self.assertRaises(Blocked):
            Plan.load(path)

    def test_confirmation_binds_count_and_digest(self):
        plan = self.fixture.plan()
        for approval in ("yes", "SIMULATE 3", approval_phrase(plan).replace("SIMULATE 3", "SIMULATE 2")):
            with self.subTest(approval=approval), self.assertRaises(Blocked):
                execute_simulation(plan, self.fixture.provider, approval)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_wrong_account_database_zone_and_owner_stop_before_calls(self):
        plan = self.fixture.plan()
        for field in ("account", "database", "zone", "owner"):
            self.fixture.provider.scope = replace(self.fixture.scope, **{field: "wrong"})
            with self.subTest(field=field), self.assertRaises(Blocked):
                self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_stale_credentials_stop_before_calls(self):
        plan = self.fixture.plan()
        self.fixture.provider.authenticated = False
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_new_old_capture_upload_cannot_enter_approved_plan(self):
        plan = self.fixture.plan()
        new = replace(self.fixture.assets[0], id="newly-uploaded-full-id", captured_at="1990-01-01")
        self.fixture.provider.active[new.id] = new
        self.fixture.provider.generation += 1
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])
        self.assertIn(new.id, self.fixture.provider.active)

    def test_missing_paginated_asset_blocks_execution(self):
        plan = self.fixture.plan()
        self.fixture.provider.active.pop(self.fixture.assets[1].id)
        # Even a lying complete flag and unchanged generation cannot hide drift.
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_master_tag_or_resource_change_blocks_execution(self):
        plan = self.fixture.plan()
        key = self.fixture.assets[0].id
        original = self.fixture.assets[0]
        for changed in (replace(original, master_tag="changed"),
                        replace(original, asset_tag="changed"),
                        replace(original, resources=(replace(original.resources[0], version="new"), *original.resources[1:]))):
            self.fixture.provider.active[key] = changed
            with self.subTest(asset=changed), self.assertRaises(Blocked):
                self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_generation_drift_even_if_inventory_returns_to_same_contents(self):
        plan = self.fixture.plan()
        self.fixture.provider.generation += 2
        with self.assertRaises(Blocked):
            self.run_plan(plan)

    def test_race_between_inventory_and_operation_is_conditionally_rejected(self):
        plan = self.fixture.plan()
        provider = self.fixture.provider
        original = provider.simulate_move

        def raced(expected, generation):
            provider.generation += 1
            return original(expected, generation)

        provider.simulate_move = raced
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(provider.calls, [])

    def test_partial_success_and_per_record_error_are_audited(self):
        plan = self.fixture.plan()
        first, second, third = [a.id for a in self.fixture.assets]
        self.fixture.provider.faults[second] = "record_error"
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [first, second])
        self.assertIn(third, self.fixture.provider.active)
        audit = [json.loads(line) for line in (self.fixture.state / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["status"] for e in audit], ["attempted", "confirmed", "attempted", "failed"])
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [first, second])

    def test_timeout_after_application_reconciles_without_retrying_asset(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "timeout_after"
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        result = self.run_plan(Plan.load(self.save_plan(plan)))
        self.assertEqual(result["simulated_count"], 3)
        self.assertEqual(self.fixture.provider.calls.count(first), 1)

    def save_plan(self, plan):
        path = self.fixture.state / "plan.json"
        plan.save(path)
        return path

    def test_timeout_before_application_remains_unknown_without_blind_retry(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "timeout_before"
        for _ in range(2):
            with self.assertRaises(Blocked):
                self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [first])
        self.assertEqual(len(self.fixture.provider.active), 3)

    def test_interruption_after_application_preserves_intent_for_resume(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "interrupt_after"
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan(plan)
        self.assertIn('"status":"attempted"', (self.fixture.state / "audit.jsonl").read_text())
        self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls.count(first), 1)

    def test_bad_response_requires_reconciliation_of_exact_record(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "wrong_response"
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls.count(first), 1)

    def test_success_response_without_postcondition_is_not_success(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "success_without_change"
        for _ in range(2):
            with self.assertRaises(Blocked):
                self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [first])

    def test_recently_deleted_record_changes_block_reconciliation(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "timeout_after"
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        original = self.fixture.provider.recently_deleted[first]
        self.fixture.provider.recently_deleted[first] = replace(original, master_id="wrong-master")
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [first])

    def test_changed_replicas_block_resume_after_partial_success(self):
        plan = self.fixture.plan()
        first = self.fixture.assets[0].id
        self.fixture.provider.faults[first] = "timeout_after"
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        (self.fixture.roots[1] / self.fixture.bindings[0].second).write_text("changed fixture")
        with self.assertRaises(Blocked):
            self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [first])

    def test_invalid_or_other_plan_audit_blocks_resume(self):
        plan = self.fixture.plan()
        path = self.fixture.state / "audit.jsonl"
        for content in ('{"interrupted":', canonical({"plan_sha256": "wrong", "id": self.fixture.assets[0].id}),
                        canonical({"plan_sha256": plan.sha256, "id": self.fixture.assets[0].id, "status": "confirmed"})):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(Blocked):
                self.run_plan(plan)
        self.assertEqual(self.fixture.provider.calls, [])

    def test_nonmock_provider_is_refused(self):
        plan = self.fixture.plan()
        class OtherProvider:
            def snapshot(self):
                raise AssertionError("Should never be called")
        with self.assertRaises(Blocked):
            execute_simulation(plan, OtherProvider(), approval_phrase(plan))

    def test_review_bundles_identical_and_outside_media(self):
        plan = self.fixture.plan()
        self.run_plan(plan)
        first, second = self.fixture.export_review(plan)
        for name in ("plan.json", "audit.jsonl", "limitations.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        for root in self.fixture.roots:
            self.assertNotIn(root, first.parents)
            self.assertNotIn(root, second.parents)

    def test_demo_requires_exact_confirmation_and_leaves_review_bundles(self):
        script = Path(__file__).parent / "demo.py"
        env = {**os.environ, "TMPDIR": str(self.fixture.directory),
               "TMP": str(self.fixture.directory), "TEMP": str(self.fixture.directory)}
        with subprocess.Popen([sys.executable, "-u", str(script), "--simulate"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) as process:
            prefix = []
            while True:
                line = process.stdout.readline()
                self.assertTrue(line, "Demo stopped before its confirmation prompt")
                prefix.append(line)
                if line.startswith("Type exactly: "):
                    phrase = line.removeprefix("Type exactly: ").strip()
                    break
            output, errors = process.communicate(phrase + "\n", timeout=10)
        self.assertEqual(process.returncode, 0, errors)
        result = json.loads(output[output.index("{"):])
        self.assertEqual(result["simulated_count"], 3)
        self.assertTrue(result["fixture_bytes_unchanged"])
        for path in result["review_bundles"]:
            self.assertTrue((Path(path) / "plan.json").is_file())
            self.assertTrue((Path(path) / "audit.jsonl").is_file())
        refused = subprocess.run([sys.executable, str(script), "--simulate"],
                                 input="yes\n", capture_output=True, text=True, env=env,
                                 timeout=10, check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('"blocked"', refused.stdout)
        self.assertIn('"fixture_bytes_unchanged": true', refused.stdout)


if __name__ == "__main__":
    unittest.main()
