"""Supervised cleanup wiring, weak-provider races and mirrored intent durability."""

from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from cleanup import (
    Blocked, Fixture, LIMITS, MODE, ReviewSession, UnconditionalCloud, confirmation,
    data_for, digest, hash_file, locations, main, prepare, simulate,
)
import cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for name in ("socket", "getaddrinfo"):
            guard = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def fixture(self, name="run"):
        return Fixture(self.root / name)

    def events(self, output):
        return [json.loads(p.read_text()) for p in sorted((output / "journal").iterdir())]

    def media(self, fixture):
        return {str(p): hash_file(p) for root in fixture.roots for p in root.rglob("*") if p.is_file()}

    def test_preview_and_mirrored_results(self):
        fixture = self.fixture()
        before = self.media(fixture)
        plan = prepare(fixture)
        outputs = locations(data_for(plan))
        html = (fixture.state / "review.html").read_text()
        self.assertIn("not decoded media", html)
        self.assertIn(confirmation(plan), html)
        self.assertIn("Content-Security-Policy", html)
        for asset in fixture.assets:
            self.assertIn(asset.id, html)
            for resource in asset.resources:
                self.assertIn(resource.role.replace('_', ' '), html)
        for root in fixture.roots:
            self.assertIn(str(root), html)
        self.assertEqual(len({(p / "plan.json").read_bytes() for p in outputs}), 1)
        self.assertTrue(all(not self.events(p) for p in outputs))
        provider = UnconditionalCloud(fixture.snapshot)
        result = simulate(plan, provider, confirmation(plan))
        self.assertEqual(provider.calls, [a["id"] for a in plan.data()["targets"]])
        self.assertEqual(set(provider.trash), {a.id for a in fixture.assets})
        self.assertEqual(provider.active, {})
        self.assertEqual(result["mode"], MODE)
        self.assertEqual(result["limits"], LIMITS)
        self.assertEqual(result["simulated_count"], 3)
        self.assertTrue(result["simulated_postconditions_checked"])
        self.assertEqual(len({(p / "result.json").read_bytes() for p in outputs}), 1)
        events = self.events(outputs[0])
        self.assertTrue(all(self.events(p) == events for p in outputs))
        self.assertEqual([e["status"] for e in events], ["started", *[s for _ in range(3)
                         for s in ("attempted", "confirmed")], "completed"])
        previous = None
        for number, event in enumerate(events, 1):
            self.assertEqual(event["sequence"], number)
            self.assertEqual(event["previous_sha256"], previous)
            self.assertEqual(event["plan_sha256"], plan.sha256)
            previous = digest(event)
        self.assertEqual(before, self.media(fixture))

    def test_confirmation_and_provider_boundaries(self):
        fixture = self.fixture()
        plan = prepare(fixture)
        provider = UnconditionalCloud(fixture.snapshot)
        for answer in ("", "SIMULATE CLEANUP 3", confirmation(plan).lower(),
                       confirmation(plan).replace("CLEANUP 3", "CLEANUP 2")):
            with self.subTest(answer=answer), self.assertRaisesRegex(Blocked, "must be confirmed"):
                simulate(plan, provider, answer)
        with self.assertRaisesRegex(Blocked, "in-memory unconditional mock"):
            simulate(plan, fixture.provider, confirmation(plan))
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.events(fixture.state), [])

    def test_unknown_resources_block_preparation(self):
        for case in ("missing_motion", "unproven", "partial"):
            with self.subTest(case=case):
                fixture = self.fixture(case)
                live = fixture.assets[1]
                if case == "missing_motion":
                    live = replace(live, resources=tuple(r for r in live.resources if r.role != "live_motion"))
                elif case == "unproven":
                    live = replace(live, relationships_proven=False)
                else:
                    live = replace(live, complete=False)
                fixture.snapshot = replace(fixture.snapshot, active=(fixture.assets[0], live, fixture.assets[2]))
                with self.assertRaisesRegex(Blocked, "Incomplete or unknown resource"):
                    prepare(fixture)
                self.assertFalse((fixture.state / "plan.json").exists())

    def test_changed_evidence_blocks_preflight(self):
        for case, message in (("backup", "Replica bytes differ"), ("plan", "saved review plan changed"),
                              ("upload", "Album membership"), ("scope", "Account, database"),
                              ("incomplete", "completeness")):
            with self.subTest(case=case):
                fixture = self.fixture(case)
                plan = prepare(fixture)
                provider = UnconditionalCloud(fixture.snapshot)
                if case == "backup":
                    (fixture.roots[1] / fixture.bindings[0].second).write_bytes(b"changed")
                elif case == "plan":
                    (locations(data_for(plan))[-1] / "plan.json").write_text("{}")
                elif case == "upload":
                    extra = replace(fixture.assets[0], id="new-old-capture-upload", captured_at="1990-01-01")
                    provider.active[extra.id] = extra
                elif case == "scope":
                    provider.scope = replace(provider.scope, owner="another-owner")
                else:
                    provider.complete = False
                with self.assertRaisesRegex(Blocked, message):
                    simulate(plan, provider, confirmation(plan))
                self.assertEqual(provider.calls, [])
                self.assertEqual(self.events(fixture.state), [])

    def test_postflight_detects_accepted_races(self):
        for case in ("target", "resource", "upload"):
            with self.subTest(case=case):
                fixture = self.fixture(case)
                plan = prepare(fixture)
                provider = UnconditionalCloud(fixture.snapshot)
                first = plan.data()["targets"][0]["id"]
                remove = provider.remove

                def race(key):
                    # This hook runs after the last preflight and durable intent.
                    self.assertEqual(self.events(fixture.state)[-1]["status"], "attempted")
                    original = provider.active[key]
                    if case == "target":
                        provider.active[key] = replace(original, asset_tag="changed-after-preflight")
                    elif case == "resource":
                        changed = replace(original.resources[0], version="new-resource-version")
                        provider.active[key] = replace(original, resources=(changed, *original.resources[1:]))
                        self.assertEqual(provider.active[key].asset_tag, original.asset_tag)
                    else:
                        extra = replace(original, id="new-old-capture-upload", captured_at="1990-01-01")
                        provider.active[extra.id] = extra
                    return remove(key)

                with patch.object(provider, "remove", side_effect=race), \
                        self.assertRaisesRegex(Blocked, "Album membership"):
                    simulate(plan, provider, confirmation(plan))
                # The operation was accepted: postflight stops later IDs, not the
                # raced write itself. A mock conditional rejection would hide this.
                self.assertEqual(provider.calls, [first])
                self.assertIn(first, provider.trash)
                self.assertEqual(len(provider.active), 3 if case == "upload" else 2)
                self.assertEqual([e["status"] for e in self.events(fixture.state)],
                                 ["started", "attempted", "unknown"])
                self.assertFalse((fixture.state / "result.json").exists())

    def test_partial_outcomes_stop_without_retry(self):
        scenarios = (("record_error", "failed"), ("timeout_before", "unknown"),
                     ("timeout_after", "unknown"), ("wrong_response", "unknown"),
                     ("interrupt_after", "unknown"))
        for fault, expected_status in scenarios:
            with self.subTest(fault=fault):
                fixture = self.fixture(fault)
                before = self.media(fixture)
                plan = prepare(fixture)
                provider = UnconditionalCloud(fixture.snapshot)
                first, second, last = [a["id"] for a in plan.data()["targets"]]
                provider.faults[second] = fault
                session = ReviewSession(plan, provider)
                with self.assertRaisesRegex(Blocked, "Mock"):
                    session.execute(confirmation(plan))
                self.assertEqual(session.phase, "stopped")
                with self.assertRaisesRegex(Blocked, "review is closed"):
                    session.execute(confirmation(plan))
                self.assertEqual(provider.calls, [first, second])
                self.assertIn(first, provider.trash)
                self.assertIn(last, provider.active)
                self.assertEqual(second in provider.trash,
                                 fault in ("timeout_after", "wrong_response", "interrupt_after"))
                events = self.events(fixture.state)
                self.assertEqual([e["status"] for e in events],
                                 ["started", "attempted", "confirmed", "attempted", expected_status])
                self.assertTrue(all(self.events(p) == events for p in locations(data_for(plan))))
                with self.assertRaisesRegex(Blocked, "already started"):
                    simulate(plan, provider, confirmation(plan))
                self.assertEqual(provider.calls, [first, second])
                self.assertEqual(before, self.media(fixture))

    def test_incomplete_journal_prevents_request(self):
        fixture = self.fixture()
        plan = prepare(fixture)
        provider = UnconditionalCloud(fixture.snapshot)
        failed = locations(data_for(plan))[-1] / "journal" / "000002.json"
        write = cleanup.write_new

        def fail(path, content):
            if path == failed:
                raise OSError("Simulated disconnected second drive")
            write(path, content)

        with patch.object(cleanup, "write_new", side_effect=fail), \
                self.assertRaisesRegex(Blocked, "persisted"):
            simulate(plan, provider, confirmation(plan))
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.events(fixture.state)[-1]["status"], "attempted")
        self.assertEqual(self.events(failed.parent.parent)[-1]["status"], "started")
        with self.assertRaisesRegex(Blocked, "already started"):
            simulate(plan, provider, confirmation(plan))

    def test_selection_preserves_kept_variants(self):
        fixture = self.fixture()
        before = self.media(fixture)
        plan = prepare(fixture)
        provider = UnconditionalCloud(fixture.snapshot)
        session = ReviewSession(plan, provider)
        kept = fixture.assets[1]  # All three Live Photo variants must stay together.
        selected = [a.id for a in fixture.assets if a.id != kept.id]
        state = session.draft(0, selected)
        self.assertEqual((state["count"], state["total"]), (2, 3))
        self.assertFalse(state["saved"])
        self.assertIsNone(state["confirmation"])
        with self.assertRaisesRegex(Blocked, "unsaved changes"):
            session.execute(confirmation(plan))
        with self.assertRaisesRegex(Blocked, "superseded"):
            simulate(plan, provider, confirmation(plan))
        saved = session.save(state["revision"])
        revised = session.current
        self.assertTrue(saved["saved"])
        self.assertNotEqual(revised.sha256, plan.sha256)
        self.assertEqual(saved["bytes"], sum(r.size for a in fixture.assets if a.id != kept.id for r in a.resources))
        self.assertEqual({a["id"] for a in revised.data()["targets"]}, set(selected))
        self.assertEqual(revised.data()["bytes"], saved["bytes"])
        self.assertEqual(revised.data()["parent_plan_sha256"], plan.sha256)
        self.assertEqual(len({(p / "plan.json").read_bytes() for p in locations(data_for(revised))}), 1)
        with self.assertRaisesRegex(Blocked, "latest saved selection"):
            session.execute(confirmation(plan))
        self.assertEqual(provider.calls, [])
        result = session.execute(saved["confirmation"])
        self.assertEqual(result["simulated_count"], 2)
        self.assertEqual(provider.active, {kept.id: kept})
        self.assertEqual(set(provider.calls), set(selected))
        self.assertEqual(before, self.media(fixture))
        with self.assertRaisesRegex(Blocked, "review is closed"):
            session.draft(saved["revision"], [kept.id])

    def test_drafts_reject_stale_and_invalid_selections(self):
        fixture = self.fixture()
        plan = prepare(fixture)
        provider = UnconditionalCloud(fixture.snapshot)
        session = ReviewSession(plan, provider)
        for selected in (["../escape"], [fixture.assets[0].id] * 2, [False], {"id": "anything"}):
            with self.subTest(selected=selected), self.assertRaisesRegex(Blocked, "unique full IDs"):
                session.draft(0, selected)
        self.assertEqual(session.current, plan)
        state = session.draft(0, [])
        with self.assertRaisesRegex(Blocked, "another request"):
            session.draft(0, [fixture.assets[0].id])
        with self.assertRaisesRegex(Blocked, "Nothing is selected"):
            session.save(state["revision"])
        with self.assertRaisesRegex(Blocked, "unsaved changes"):
            session.execute(confirmation(plan))
        # Restoring the original selection must still create a fresh plan.
        state = session.draft(state["revision"], [a.id for a in fixture.assets])
        saved = session.save(state["revision"])
        self.assertNotEqual(saved["confirmation"], confirmation(plan))
        self.assertEqual(provider.calls, [])

    def test_selection_save_fails_closed(self):
        for case in ("retire", "verify", "persist"):
            with self.subTest(case=case):
                fixture = self.fixture(case)
                plan = prepare(fixture)
                provider = UnconditionalCloud(fixture.snapshot)
                session = ReviewSession(plan, provider)
                selected = [fixture.assets[0].id]
                if case == "retire":
                    with patch.object(cleanup, "write_new", side_effect=OSError("Disconnected")), \
                            self.assertRaisesRegex(Blocked, "retire every plan"):
                        session.draft(0, selected)
                else:
                    state = session.draft(0, selected)
                    if case == "verify":
                        (fixture.roots[1] / fixture.bindings[0].second).write_bytes(b"drift")
                        with self.assertRaisesRegex(Blocked, "verified and saved"):
                            session.save(state["revision"])
                    else:
                        # Fail the final saved plan copy, after two copies exist.
                        original = cleanup.Plan.save
                        def fail(plan, path):
                            if "replica-b" in path.parts:
                                raise OSError("Disconnected")
                            original(plan, path)
                        with patch.object(cleanup.Plan, "save", fail), \
                                self.assertRaisesRegex(Blocked, "verified and saved"):
                            session.save(state["revision"])
                self.assertEqual(session.phase, "stopped")
                self.assertIsNone(session.current)
                self.assertIsNone(session.state()["confirmation"])
                self.assertEqual(provider.calls, [])
                with self.assertRaisesRegex(Blocked, "review is closed"):
                    session.execute(confirmation(plan))

    def test_guided_cli_and_cancellation(self):
        for case in ("preview", "cancel", "confirm", "mistype", "edit"):
            with self.subTest(case=case):
                output, errors = StringIO(), StringIO()
                saved = []
                sessions = []
                answers = []
                original = cleanup.prepare

                def capture(fixture):
                    plan = original(fixture)
                    saved.append(plan)
                    return plan

                def answer(_prompt):
                    answers.append(True)
                    if case == "edit":
                        session = sessions[0]
                        if len(answers) == 1:
                            session.draft(0, [a["id"] for a in saved[0].data()["targets"][:2]])
                            return confirmation(saved[0])  # Refuse the old phrase without closing review.
                        return session.save(session.revision)["confirmation"]
                    if case == "mistype":
                        return "wrong" if len(answers) == 1 else ""
                    return confirmation(saved[0]) if case == "confirm" else ""

                @contextmanager
                def local_review(session):
                    sessions.append(session)
                    yield "http://127.0.0.1:1234/#synthetic-token"

                args = ["cleanup.py"] + ([] if case == "preview" else ["--simulate"])
                with patch.object(sys, "argv", args), patch.object(cleanup, "prepare", side_effect=capture), \
                        patch.object(cleanup.tempfile, "mkdtemp", return_value=str(self.root / case)), \
                        patch("review.serve", side_effect=local_review), \
                        patch("builtins.input", side_effect=answer) as prompt, \
                        redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(main(), 0)
                self.assertEqual(prompt.call_count, 0 if case == "preview" else 2 if case in ("mistype", "edit") else 1)
                self.assertIn("Open the saved preview: file:" if case == "preview" else
                              "Open the review: http://127.0.0.1:", output.getvalue())
                state = self.root / case / "state"
                if case == "edit":
                    self.assertIn("Not confirmed:", output.getvalue())
                    state = Path(sessions[0].current.data()["outputs"][0])
                    self.assertEqual(len(sessions[0].provider.calls), 2)
                self.assertEqual(bool(self.events(state)), case in ("confirm", "edit"))
                self.assertEqual((state / "result.json").exists(), case in ("confirm", "edit"))


if __name__ == "__main__":
    unittest.main()
