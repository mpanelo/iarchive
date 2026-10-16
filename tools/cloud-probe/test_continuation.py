"""Checkpoint provenance and resumed transport checks using synthetic reports."""

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import changes
import probe
import test_changes as fixtures
from changes import ChangesBlocked, collect_changes, load_continuation


def event(token, requested=None, phase="initial", more=True):
    return {"phase": phase, "requested_sync_token": requested, "sync_token": token,
            "more_coming": more, "records": [{**fixtures.record(), "deleted": False}], "complete": False}


def checkpoint(repo, name="cloud-probe-source", pages=None, parent=None):
    run = repo / ".smoke" / name
    run.mkdir(parents=True)
    context = {"account_id": fixtures.ACCOUNT, "database": "private", "zone_id": fixtures.ZONE,
               "complete": False, **({"continuation": parent["source"]} if parent else {})}
    pages = pages if pages is not None else [event("t1")]
    summary = {"schema": 1, "scope": "changes", "provider_version": probe.PIN,
               "authenticated": True, "library_discovery_completed": True, "phase": "change_feed",
               "change_feed_completed": False, "blocked": "ChangesBlocked",
               "finding": "Change-feed observation exceeded the collection bound",
               "request_counts": {"change_metadata_requests": len(pages)}}
    probe.save_new(run / "summary.json", summary)
    probe.save_new(run / "changes-context-private.json", context)
    for n, page in enumerate(pages, 1):
        probe.save_new(run / f"changes-page-{n}-private.json", page)
    return run


def edit(path, mutate):
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value))


def completed_checkpoint(repo, name="cloud-probe-finished", pages=None, parent=None):
    pages = pages if pages is not None else [event("t1", more=False), event("t2", "t1", "delta", False)]
    run = checkpoint(repo, name, pages, parent)
    if parent:
        edit(run / "changes-context-private.json", lambda v: v.update(delta_from=v.pop("continuation")))
    counts = dict(parent["record_counts"]) if parent else {"initial": 0, "delta": 0}
    local = {}
    for page in pages:
        counts[page["phase"]] += len(page["records"])
        local[page["phase"]] = local.get(page["phase"], 0) + len(page["records"])
    private = {"account_id": fixtures.ACCOUNT, "database": "private", "zone_id": fixtures.ZONE,
               "final_sync_token": pages[-1]["sync_token"], "chain_record_counts": counts,
               "phases": [{"phase": key, "record_count": value} for key, value in local.items()]}
    probe.save_new(run / "changes-private.json", private)
    def finish(value):
        value.pop("blocked")
        value.pop("finding")
        value.update(phase="completed", change_feed_completed=True, account_binding_recorded=True,
                     change_feed_pages=len(pages), change_feed_records=sum(local.values()),
                     chain_records_observed=sum(counts.values()), delta_records_observed=counts["delta"])
    edit(run / "summary.json", finish)
    return run


class ContinuationTests(unittest.TestCase):
    setUp = fixtures.ChangesTests.setUp
    transport = fixtures.ChangesTests.transport

    def test_history_preserves_phase_and_tokens(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repo = Path(directory)
            first = checkpoint(repo)
            initial = load_continuation(repo, first.name)
            second = checkpoint(repo, "cloud-probe-second", [event("t2", "t1", more=False)], initial)
            delta = load_continuation(repo, second.name)
            self.assertEqual(delta["phase"], "delta")
            third = checkpoint(repo, "cloud-probe-third", [event("t3", "t2", "delta")], delta)
            resumed = load_continuation(repo, third.name)
            self.assertEqual(resumed["chain_runs"], 3)
            self.assertEqual(resumed["seen_tokens"], ["t1", "t2", "t3"])
            with self.transport([fixtures.page("t4", [fixtures.record()])]) as data:
                service, state, calls, _ = data
                private, summary = collect_changes(service, fixtures.ROOT, fixtures.ZONE, state,
                                                   lambda n, p: None, expected_account=fixtures.ACCOUNT,
                                                   continuation=resumed)
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(calls[0].body)["zones"][0]["syncToken"], "t3")
            self.assertEqual(private["chain_record_counts"], {"initial": 2, "delta": 2})
            self.assertEqual(summary["delta_records_observed"], 2)
            self.assertEqual(summary["chain_records_observed"], 4)
            self.assertEqual(summary["complete_resource_inventory"], "NOT VERIFIED")
            # Parent digests must bind exact saved bytes, including older ancestors.
            edit(first / "changes-page-1-private.json", lambda p: p.update(sync_token="edited"))
            with self.assertRaisesRegex(ChangesBlocked, "earlier checkpoint changed"):
                load_continuation(repo, third.name)

    def test_damaged_checkpoints_are_rejected(self):
        cases = [
            ("summary.json", lambda v: v.update(rate_limited=True), "safely budget-stopped"),
            ("summary.json", lambda v: v.update(blocked="TimeoutError"), "safely budget-stopped"),
            ("summary.json", lambda v: v.update(change_feed_completed=True), "safely budget-stopped"),
            ("summary.json", lambda v: v.update(request_counts={}), "page counts disagree"),
            ("changes-context-private.json", lambda v: v.pop("account_id"), "binding is incomplete"),
            ("changes-context-private.json", lambda v: v["zone_id"].pop("ownerRecordName"), "binding is incomplete"),
            ("changes-context-private.json", lambda v: v.update(continuation={
                "run": "cloud-probe-source", "sha256": "0" * 64}), "cycle or too many runs"),
            ("changes-page-1-private.json", lambda v: v.update(requested_sync_token="gap"), "token chain is broken"),
            ("changes-page-1-private.json", lambda v: v.update(phase="delta"), "token chain is broken"),
            ("changes-page-1-private.json", lambda v: v["records"][0].update(fields={"captionEnc": "SECRET"}),
             "unsupported record data"),
        ]
        for filename, mutate, message in cases:
            with self.subTest(filename=filename, message=message), tempfile.TemporaryDirectory(
                    dir=Path(tempfile.gettempdir()).resolve()) as directory:
                repo = Path(directory)
                run = checkpoint(repo)
                edit(run / filename, mutate)
                with self.assertRaisesRegex(ChangesBlocked, message):
                    load_continuation(repo, run.name)
        for pages, message in (([event("t1"), event("t1", "t1")], "repeats a token"),
                               ([event("t1", more=False), event("t2", "t1", "delta", False)],
                                "delta has already finished")):
            with self.subTest(message=message), tempfile.TemporaryDirectory(
                    dir=Path(tempfile.gettempdir()).resolve()) as directory:
                repo = Path(directory)
                run = checkpoint(repo, pages=pages)
                with self.assertRaisesRegex(ChangesBlocked, message):
                    load_continuation(repo, run.name)
        for mutate in (lambda v: v.update(account_id="other-account"),
                       lambda v: v["zone_id"].update(identityVersion=True)):
            with self.subTest(scope=mutate), tempfile.TemporaryDirectory(
                    dir=Path(tempfile.gettempdir()).resolve()) as directory:
                repo = Path(directory)
                parent = checkpoint(repo)
                state = load_continuation(repo, parent.name)
                child = checkpoint(repo, "cloud-probe-child", [event("t2", "t1")], state)
                edit(child / "changes-context-private.json", mutate)
                with self.assertRaisesRegex(ChangesBlocked, "history changed account or zone"):
                    load_continuation(repo, child.name)

    def test_checkpoint_paths_and_bounds(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repo = Path(directory)
            run = checkpoint(repo)
            for name in (None, "../outside", str(run), "cloud-probe-missing"):
                with self.subTest(name=name), self.assertRaises(ChangesBlocked):
                    load_continuation(repo, name)
            with patch.object(changes, "MAX_CHECKPOINT_BYTES", 2), \
                    self.assertRaisesRegex(ChangesBlocked, "history exceeded its size bound"):
                load_continuation(repo, run.name)
            with patch.object(changes, "MAX_CONTINUATION_RUNS", 1), \
                    self.assertRaisesRegex(ChangesBlocked, "too many runs"):
                load_continuation(repo, run.name)
            original = Path.lstat
            def reparse(path):
                info = original(path)
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == run else info
            with patch.object(Path, "lstat", reparse), self.assertRaisesRegex(ChangesBlocked, "reparse point"):
                load_continuation(repo, run.name)
            (run / "changes-page-1-private.json").rename(run / "changes-page-2-private.json")
            with self.assertRaisesRegex(ChangesBlocked, "noncontiguous"):
                load_continuation(repo, run.name)

    def test_completed_tokens_start_new_deltas(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repo = Path(directory)
            run = completed_checkpoint(repo)
            with self.assertRaisesRegex(ChangesBlocked, "safely budget-stopped"):
                load_continuation(repo, run.name)
            seed = load_continuation(repo, run.name, completed=True)
            progress = []
            with self.transport([fixtures.page("t3", [fixtures.record(tag="v2")])]) as data:
                service, state, calls, _ = data
                _, summary = collect_changes(service, fixtures.ROOT, fixtures.ZONE, state,
                                             lambda n, p: progress.append(p),
                                             expected_account=fixtures.ACCOUNT, continuation=seed)
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(calls[0].body)["zones"][0]["syncToken"], "t2")
            self.assertEqual(summary["delta_records_in_run"], 1)
            self.assertEqual(summary["delta_records_observed"], 2)
            child = completed_checkpoint(repo, "cloud-probe-next", progress, seed)
            next_seed = load_continuation(repo, child.name, completed=True)
            self.assertEqual(next_seed["sync_token"], "t3")
            self.assertEqual(next_seed["record_counts"], {"initial": 1, "delta": 2})
            partial = checkpoint(repo, "cloud-probe-partial", [event("t4", "t3", "delta")], next_seed)
            edit(partial / "changes-context-private.json", lambda v: v.update(delta_from=v.pop("continuation")))
            resumed = load_continuation(repo, partial.name)
            self.assertEqual(resumed["phase"], "delta")
            self.assertEqual(resumed["sync_token"], "t4")
            with self.assertRaisesRegex(ChangesBlocked, "completed change-feed checkpoint"):
                load_continuation(repo, partial.name, completed=True)

    def test_completed_reports_must_match_pages(self):
        cases = [("changes-private.json", lambda v: v.update(final_sync_token="edited")),
                 ("changes-private.json", lambda v: v.update(account_id="other-account")),
                 ("changes-private.json", lambda v: v["chain_record_counts"].update(initial=True)),
                 ("changes-private.json", lambda v: v["phases"][0].update(record_count=9)),
                 ("summary.json", lambda v: v.update(change_feed_records=99)),
                 ("changes-page-2-private.json", lambda v: v.update(more_coming=True))]
        for filename, mutate in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(
                    dir=Path(tempfile.gettempdir()).resolve()) as directory:
                repo = Path(directory)
                run = completed_checkpoint(repo)
                edit(run / filename, mutate)
                with self.assertRaisesRegex(ChangesBlocked, "Completed checkpoint disagrees"):
                    load_continuation(repo, run.name, completed=True)

    def test_cli_continues_only_bound_scope(self):
        for outcome in ("complete", "wrong-account", "wrong-zone", "expired", "unsafe", "since"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(
                    dir=Path(tempfile.gettempdir()).resolve()) as directory:
                repo = Path(directory)
                source = completed_checkpoint(repo) if outcome == "since" else checkpoint(repo)
                if outcome == "unsafe":
                    edit(source / "summary.json", lambda v: v.update(rate_limited=True))
                source_digest = hashlib.sha256((source / "changes-page-1-private.json").read_bytes()).hexdigest()
                run = repo / "new-output"
                run.mkdir()
                values = ([{"zones": [{"zoneID": fixtures.ZONE, "serverErrorCode": "CHANGE_TOKEN_EXPIRED"}]}]
                          if outcome == "expired" else [fixtures.page("t3", [fixtures.record(tag="v2")])]
                          if outcome == "since" else [fixtures.page("t2"), fixtures.page("t2")])
                with self.transport(values, guarded=False) as data:
                    service, _, calls, _ = data
                    if outcome == "wrong-account":
                        service.data["dsInfo"]["dsid"] = service.params["dsid"] = "other-account"
                    zone = {**fixtures.ZONE, "identityVersion": 2} if outcome == "wrong-zone" else fixtures.ZONE
                    output = io.StringIO()
                    authenticate = Mock(return_value=service)
                    with patch.dict(sys.modules, {"pyicloud_ipd": SimpleNamespace(
                            PyiCloudService=authenticate)}), \
                            patch("probe.__file__", str(repo / "tools/cloud-probe/probe.py")), \
                            patch.object(sys, "argv", ["probe.py", "changes", "--since-run" if outcome == "since"
                                                       else "--continue-from", source.name]), \
                            patch("probe.version", return_value=probe.PIN), \
                            patch("probe.sys.stdin.isatty", return_value=True), \
                            patch("probe.sys.stderr.isatty", return_value=True), \
                            patch("builtins.input", return_value="READ ONLY CHANGES"), \
                            patch("probe.getpass.getpass", return_value="fake@example.invalid"), \
                            patch("probe.new_run", return_value=run), patch("probe.logging.disable"), \
                            patch("probe.discover_zones", return_value=({"private": [
                                {"zone_id": zone, "deleted": False}], "shared": []}, [])) as discovery, \
                            redirect_stdout(output):
                        self.assertEqual(probe.main(), int(outcome not in ("complete", "since")))
                    self.assertEqual(len(calls), 2 if outcome == "complete" else 1 if outcome in ("expired", "since") else 0)
                    if outcome == "wrong-account":
                        discovery.assert_not_called()
                    if outcome == "unsafe":
                        authenticate.assert_not_called()
                    if calls:
                        self.assertEqual(json.loads(calls[0].body)["zones"][0]["syncToken"],
                                         "t2" if outcome == "since" else "t1")
                    summary = json.loads((run / "summary.json").read_text())
                    self.assertEqual(summary["change_feed_completed"], outcome in ("complete", "since"))
                    self.assertEqual(summary["cloud_coverage"], "NOT VERIFIED")
                    expected_findings = {
                        "wrong-account": "Checkpoint belongs to a different authenticated account",
                        "wrong-zone": "Checkpoint full zone identity changed",
                        "expired": "Change-feed zone returned an error or unsupported response",
                        "unsafe": "Only a safely budget-stopped change-feed run can continue",
                    }
                    if outcome in expected_findings:
                        self.assertEqual(summary["finding"], expected_findings[outcome])
                    if outcome == "complete":
                        context = json.loads((run / "changes-context-private.json").read_text())
                        self.assertEqual(context["continuation"]["run"], source.name)
                        self.assertEqual(summary["chain_records_observed"], 1)
                    elif outcome == "since":
                        context = json.loads((run / "changes-context-private.json").read_text())
                        self.assertEqual(context["delta_from"]["run"], source.name)
                        self.assertEqual(summary["delta_records_in_run"], 1)
                    self.assertEqual(hashlib.sha256((source / "changes-page-1-private.json").read_bytes()).hexdigest(),
                                     source_digest)
                    for secret in (fixtures.ACCOUNT, fixtures.ZONE["ownerRecordName"], '"t1"', "fake@example.invalid"):
                        self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
