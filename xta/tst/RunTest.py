#!/usr/bin/env python3
# LLMDirector Self-Test Suite
import sys
import argparse
import subprocess, shlex
import unittest
import json
import re
import os
import shutil
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

DAT = Path(__file__).parent / "dat"
sys.path.append(str(Path(__file__).parent.parent.parent / "web"))

import app as _app_module
from app import (RunState, parse_fails, preflight_hooks,
                 release_if_last_run, _all_runs_terminal,
                 validate_flow_config, _reachable_from, fmt_ts,
                 load_flow_config, get_run_progress, entry_topics_ordered)

_real_dispatch = _app_module.dispatch


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _load_json(name):
    with open(DAT / name) as f: return json.load(f)

def _setup_config(tmpdir=None, flow_name="valid_flow.json"):
    import app
    conductor = _load_json("valid_conductor.json")
    flow      = _load_json(flow_name)
    app._config = {
        "logDir":    str(tmpdir or "/tmp/llm_logs"),
        "eventDir":  str(tmpdir or "/tmp/llm_events"),
        "limits":    {"loopCap": 5, "maxTurns": 40, "turnTimeoutSec": 1200},
        "conductorUrl": "http://localhost:0",
        "conductorJsonPath": "/tmp/conductor.json",
        "Hook": str(Path(tmpdir or "/tmp") / "batch/LLMHookEvent.sh"),
        "HumanNotifyScript": "~/batch/LLMWaiting.bat"
    }
    app._conductor_config_cache = conductor
    app._flow_config       = flow
    app._flow_config_error = None
    app._run_states = {}
    Path(app._config["logDir"]).mkdir(exist_ok=True, parents=True)
    Path(app._config["eventDir"]).mkdir(exist_ok=True)
    # Write conductor JSON for any code that reads the path directly
    with open("/tmp/conductor.json", "w") as f: json.dump(conductor, f)


# ── Fixture loading ────────────────────────────────────────────────────────────

class TestFixtures(unittest.TestCase):
    def test_valid_conductor_loads(self):
        d = _load_json("valid_conductor.json")
        self.assertIn("Topic", d)
        self.assertIn("02: Critique_Spec", d["Topic"])
        self.assertEqual(d["02: Critique_Spec"]["Role"], "Developer")

    def test_valid_flow_loads(self):
        d = _load_json("valid_flow.json")
        self.assertEqual(d["startTopic"], "02: Critique_Spec")
        self.assertEqual(d["endTopic"],   "11: Ready_To_Commit")
        self.assertIn("05: Validate_Implementation", d["topics"])

    def test_run_state_compat_loads(self):
        d = _load_json("run_state_compat.json")
        self.assertEqual(d["node"], "05: Validate_Implementation")
        self.assertIn("started_at", d)
        self.assertIn("roles_initialized", d)
        self.assertIn("pending_workflow_node", d)
        self.assertEqual(d["counters"]["Validate"], 1)


# ── Timestamp format ───────────────────────────────────────────────────────────

class TestTimestampFormat(unittest.TestCase):
    def test_fmt_ts_format(self):
        ts = fmt_ts(datetime(2026, 6, 2, 14, 30, 15))
        self.assertEqual(ts, "2026-0602-14:30:15")

    def test_fmt_ts_matches_pattern(self):
        ts = fmt_ts()
        self.assertRegex(ts, r"^\d{4}-\d{4}-\d{2}:\d{2}:\d{2}$")

    def test_log_decision_uses_new_format(self):
        _setup_config()
        run = RunState("P", "/tmp", "A", "D")
        _app_module.log_decision(run, "test")
        last = run.history[-1]
        # Format: YYYY-MMDD-HH:mm:ss | message
        self.assertRegex(last, r"^\d{4}-\d{4}-\d{2}:\d{2}:\d{2} \| ")

    def test_fmt_ts_converts_aware_utc_to_local(self):
        utc_dt = datetime(2026, 6, 2, 14, 30, 15, tzinfo=timezone.utc)
        expected = utc_dt.astimezone().strftime("%Y-%m%d-%H:%M:%S")
        self.assertEqual(fmt_ts(utc_dt), expected)


# ── Flow config validation ─────────────────────────────────────────────────────

class TestFlowConfigValidation(unittest.TestCase):
    def setUp(self):
        self.conductor = _load_json("valid_conductor.json")

    def test_valid_flow_passes(self):
        flow = _load_json("valid_flow.json")
        errs = validate_flow_config(flow, self.conductor)
        self.assertEqual(errs, [], f"Unexpected errors: {errs}")

    def test_missing_start_topic(self):
        flow = _load_json("valid_flow.json")
        del flow["startTopic"]
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("startTopic" in e for e in errs))

    def test_bad_destination_topic(self):
        flow = _load_json("flow_bad_dest.json")
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("99: Nonexistent_Topic" in e or "not in LLMConductor" in e for e in errs))

    def test_unreachable_end_topic(self):
        flow = _load_json("flow_unreachable_end.json")
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("unreachable" in e.lower() or "11: Ready_To_Commit" in e for e in errs))

    def test_unreachable_configured_topic(self):
        flow = _load_json("flow_unreachable_topic.json")
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("04: Implement_Spec" in e for e in errs))

    def test_unknown_action_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["topics"]["02: Critique_Spec"]["action"] = "dance"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("unknown action" in e for e in errs))

    def test_unknown_escalate_kind_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["topics"]["03: Answer_Update_Spec"]["escalateKind"] = "PARTY"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("escalateKind" in e for e in errs))

    def test_absolute_sentinel_path_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["topics"]["02: Critique_Spec"]["backIf"] = "/etc/passwd"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("safe relative" in e for e in errs))

    def test_path_traversal_sentinel_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["topics"]["02: Critique_Spec"]["backIf"] = "../outside.md"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("safe relative" in e for e in errs))

    def test_sentinel_not_in_mesg_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["topics"]["02: Critique_Spec"]["backIf"] = "NotMentioned.md"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("not mentioned in topic Mesg" in e for e in errs))

    def test_sentinel_substring_case_sensitive(self):
        # lowercase version not in Mesg
        flow = _load_json("valid_flow.json")
        flow["topics"]["02: Critique_Spec"]["backIf"] = "temptbd_questions.md"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("not mentioned in topic Mesg" in e for e in errs))

    def test_validate_action_requires_loop_counter(self):
        flow = _load_json("valid_flow.json")
        del flow["topics"]["05: Validate_Implementation"]["loopCounter"]
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("loopCounter" in e for e in errs))

    def test_escalate_if_without_kind_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["topics"]["03: Answer_Update_Spec"].pop("escalateKind")
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("escalateKind" in e for e in errs))

    def test_reachable_topics_must_have_entry(self):
        flow = _load_json("valid_flow.json")
        del flow["topics"]["03: Answer_Update_Spec"]
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("03: Answer_Update_Spec" in e and "no entry" in e for e in errs))

    def test_questions_back_to_must_have_companion_entry(self):
        """questionsBackTo pointing to a conductor topic absent from companion topics is rejected."""
        flow = _load_json("valid_flow.json")
        # questionsBackTo = 03: Answer_Update_Spec; remove its companion entry
        del flow["topics"]["03: Answer_Update_Spec"]
        # Also break the backTo/backIf references that use it so they don't create
        # additional errors that would obscure the specific one we're testing
        flow["topics"]["02: Critique_Spec"].pop("backIf", None)
        flow["topics"]["02: Critique_Spec"].pop("backTo", None)
        flow["topics"]["04: Implement_Spec"].pop("backIf", None)
        flow["topics"]["04: Implement_Spec"].pop("backTo", None)
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(
            any("questionsBackTo" in e and "no entry in companion topics map" in e for e in errs),
            f"Expected questionsBackTo companion-entry error, got: {errs}"
        )

    def test_questions_back_to_in_conductor_still_required(self):
        """questionsBackTo must still exist in LLMConductor.json topic list."""
        flow = _load_json("valid_flow.json")
        flow["questionsBackTo"] = "99: NonExistent"
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("questionsBackTo" in e and "not in LLMConductor" in e for e in errs))


# ── Reachability ───────────────────────────────────────────────────────────────

class TestReachability(unittest.TestCase):
    def setUp(self):
        self.flow = _load_json("valid_flow.json")

    def test_all_topics_reachable(self):
        reachable = _reachable_from(self.flow, self.flow["startTopic"])
        for tid in self.flow["topics"]:
            self.assertIn(tid, reachable, f"Topic '{tid}' should be reachable")

    def test_end_topic_reachable(self):
        reachable = _reachable_from(self.flow, self.flow["startTopic"])
        self.assertIn(self.flow["endTopic"], reachable)

    def test_loop_does_not_hang(self):
        # failTo self-loop (05 → 05) must not cause infinite recursion
        reachable = _reachable_from(self.flow, "05: Validate_Implementation")
        self.assertIn("06: Review_Implementation", reachable)


# ── Validation parsing ─────────────────────────────────────────────────────────

class TestValidationParsing(unittest.TestCase):
    def test_simple_pass(self):
        self.assertEqual(parse_fails("Summary\nFAIL : 0\n"), 0)
    def test_simple_fail(self):
        self.assertEqual(parse_fails("Summary\nFAIL : 3\n"), 3)
    def test_last_match_wins(self):
        self.assertEqual(parse_fails("FAIL : 1\nSomething happened\nFAIL : 2\n"), 2)
    def test_ansi_stripped(self):
        self.assertEqual(parse_fails("\x1B[31mFAIL : 5\x1B[0m\n"), 5)
    def test_no_fail_line_returns_minus_one(self):
        self.assertEqual(parse_fails("No summary here\n"), -1)
    def test_fail_line_mid_block(self):
        out = "Summary\n  Report  : x.html\n  SUCCESS : 4\n  FAIL    : 0\n  Total   : 4\n  Elapsed : 1s\n"
        self.assertEqual(parse_fails(out), 0)


# ── FSM transitions (config-driven) ──────────────────────────────────────────

class TestFSMTransitions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)
        (Path(self.tmpdir) / "xta" / "tst").mkdir(parents=True, exist_ok=True)
        # Default stub RunTest.py — always passes
        (Path(self.tmpdir) / "xta" / "tst" / "RunTest.py").write_text(
            'import sys, argparse\nparser=argparse.ArgumentParser()\nparser.add_argument("-a","--all",action="store_true")\nargs=parser.parse_args()\nif not args.all: sys.exit(1)\nprint("FAIL : 0")\nsys.exit(0)\n'
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self):
        with patch("app.dispatch"): return RunState("P", self.tmpdir, "ARCH", "DEV")

    def _transition_mocked(self, node):
        """Return a run at node with dispatch mocked, after transition fires."""
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = node
        with patch("app.dispatch") as mock_d:
            _app_module.transition(run)
            return run, mock_d

    def test_critique_to_implement_clean(self):
        run, _ = self._transition_mocked("02: Critique_Spec")
        self.assertEqual(run.node, "04: Implement_Spec")

    def test_critique_to_answer_via_questions(self):
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("02: Critique_Spec")
        self.assertEqual(run.node, "03: Answer_Update_Spec")

    def test_answer_update_escalates_on_questions(self):
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("03: Answer_Update_Spec")
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "QUESTION")

    def test_implement_to_validate_clean(self):
        run, _ = self._transition_mocked("04: Implement_Spec")
        self.assertEqual(run.node, "05: Validate_Implementation")

    def test_implement_to_answer_via_questions(self):
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("04: Implement_Spec")
        self.assertEqual(run.node, "03: Answer_Update_Spec")

    def test_validate_pass_to_review(self):
        run, _ = self._transition_mocked("05: Validate_Implementation")
        self.assertEqual(run.node, "06: Review_Implementation")

    def test_validate_fail_increments_counter(self):
        (Path(self.tmpdir) / "xta" / "tst" / "RunTest.py").write_text(
            'import sys,argparse\np=argparse.ArgumentParser()\np.add_argument("-a","--all",action="store_true")\na=p.parse_args()\nif not a.all:sys.exit(1)\nprint("FAIL : 2")\nsys.exit(1)\n'
        )
        run, _ = self._transition_mocked("05: Validate_Implementation")
        self.assertEqual(run.counters.get("Validate", 0), 1)
        self.assertEqual(run.node, "05: Validate_Implementation")

    def test_validate_loop_cap_escalates(self):
        (Path(self.tmpdir) / "xta" / "tst" / "RunTest.py").write_text(
            'import sys,argparse\np=argparse.ArgumentParser()\np.add_argument("-a","--all",action="store_true")\na=p.parse_args()\nif not a.all:sys.exit(1)\nprint("FAIL : 1")\nsys.exit(1)\n'
        )
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "05: Validate_Implementation"
        run.counters["Validate"] = 4
        with patch("app.dispatch"): _app_module.transition(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "LOOP_CAP")

    def test_review_findings_route_to_critique_review(self):
        Path(self.tmpdir, "TempTBD_Review.md").write_text("findings")
        run, _ = self._transition_mocked("06: Review_Implementation")
        self.assertEqual(run.node, "07: Critique_Review")

    def test_review_clean_to_update_docs(self):
        run, _ = self._transition_mocked("06: Review_Implementation")
        self.assertEqual(run.node, "10: Update_Docs_And_Goldens")

    def test_review_questions_escalates(self):
        # topic 06 is in questionsEscalateTopics
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("06: Review_Implementation")
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "QUESTION")

    def test_critique_review_review_questions_to_clarify(self):
        Path(self.tmpdir, "TempTBD_ReviewQuestions.md").write_text("rq")
        run, _ = self._transition_mocked("07: Critique_Review")
        self.assertEqual(run.node, "08: Clarify_Review")
        self.assertEqual(run.counters.get("CritiqueReview", 0), 0)  # no increment on forward edge

    def test_critique_review_clean_to_address_review(self):
        run, _ = self._transition_mocked("07: Critique_Review")
        self.assertEqual(run.node, "09: Address_Review")

    def test_critique_review_questions_route_to_answer(self):
        # topic 07 is NOT in questionsEscalateTopics — routes via questionsBackTo
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("07: Critique_Review")
        self.assertEqual(run.node, "03: Answer_Update_Spec")

    def test_clarify_review_review_questions_back_to_critique(self):
        Path(self.tmpdir, "TempTBD_ReviewQuestions.md").write_text("rq")
        run, _ = self._transition_mocked("08: Clarify_Review")
        self.assertEqual(run.node, "07: Critique_Review")
        self.assertEqual(run.counters.get("CritiqueReview", 0), 1)  # increment on back edge

    def test_clarify_review_loop_cap_escalates(self):
        Path(self.tmpdir, "TempTBD_ReviewQuestions.md").write_text("rq")
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "08: Clarify_Review"
        run.counters["CritiqueReview"] = 4
        with patch("app.dispatch"): _app_module.transition(run)
        self.assertEqual(run.counters["CritiqueReview"], 5)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "LOOP_CAP")

    def test_clarify_review_clean_to_address_review(self):
        run, _ = self._transition_mocked("08: Clarify_Review")
        self.assertEqual(run.node, "09: Address_Review")

    def test_clarify_review_questions_escalates(self):
        # topic 08 is in questionsEscalateTopics
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("08: Clarify_Review")
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "QUESTION")

    def test_address_review_clean_to_validate(self):
        run, _ = self._transition_mocked("09: Address_Review")
        self.assertEqual(run.node, "05: Validate_Implementation")

    def test_address_review_questions_auto_route(self):
        # topic 09 is NOT in questionsEscalateTopics — routes via questionsBackTo
        Path(self.tmpdir, "TempTBD_Questions.md").write_text("q")
        run, _ = self._transition_mocked("09: Address_Review")
        self.assertEqual(run.node, "03: Answer_Update_Spec")

    def test_update_docs_to_ready_to_commit(self):
        run, _ = self._transition_mocked("10: Update_Docs_And_Goldens")
        self.assertEqual(run.node, "11: Ready_To_Commit")

    def test_ready_to_commit_pauses_for_commit_approval(self):
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "11: Ready_To_Commit"
        _app_module._run_states[self.tmpdir] = run
        with patch("app.dispatch"):
            _app_module.transition(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "COMMIT_APPROVAL")

    def test_max_turns_escalates(self):
        import app; app._config['limits']['maxTurns'] = 1
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "02: Critique_Spec"
        run.total_turns = 1  # already at cap
        with patch("app.dispatch"): _app_module.transition(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "MAX_TURNS")
        import app; app._config['limits']['maxTurns'] = 40  # restore


# ── Resume dispatch behavior (F1 fix) ─────────────────────────────────────────

class TestResumeDispatch(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def _paused_run(self, kind, cwd="/tmp/resume_dispatch"):
        import app
        run = RunState("P", cwd, "ARCH", "DEV")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = kind
        run.dispatch_marker = {"ts": "2026-06-01T10:00:00Z", "offset": 0, "target": "DEV"}
        app._run_states[cwd] = run
        return run

    def _resume(self, cwd, app_module):
        with app_module.app.test_client() as client:
            return client.post('/api/resume', json={"cwd": cwd})

    def test_loop_cap_resume_dispatches(self):
        import app
        run = self._paused_run("LOOP_CAP", "/tmp/rd_lc")
        with patch("app.dispatch") as mock_d:
            self._resume("/tmp/rd_lc", app)
        mock_d.assert_called_once_with(run)
        self.assertEqual(run.status, "RUNNING")

    def test_loop_cap_resume_clears_counters(self):
        import app
        run = self._paused_run("LOOP_CAP", "/tmp/rd_lc2")
        run.counters["Validate"] = 5; run.counters["CritiqueReview"] = 3
        with patch("app.dispatch"):
            self._resume("/tmp/rd_lc2", app)
        self.assertEqual(run.counters["Validate"], 0)
        self.assertEqual(run.counters["CritiqueReview"], 0)

    def test_max_turns_resume_resets_total_turns(self):
        import app
        run = self._paused_run("MAX_TURNS", "/tmp/rd_mt")
        run.total_turns = 40
        with patch("app.dispatch"):
            self._resume("/tmp/rd_mt", app)
        self.assertEqual(run.total_turns, 0)

    def test_max_turns_resume_dispatches(self):
        import app
        run = self._paused_run("MAX_TURNS", "/tmp/rd_mt2")
        with patch("app.dispatch") as mock_d:
            self._resume("/tmp/rd_mt2", app)
        mock_d.assert_called_once_with(run)

    def test_stagnation_resume_clears_hash_and_dispatches(self):
        import app
        run = self._paused_run("STAGNATION", "/tmp/rd_stag")
        run.stagnation_hashes["02: Critique_Spec"] = "oldhash"
        run.node = "02: Critique_Spec"
        with patch("app.dispatch") as mock_d:
            self._resume("/tmp/rd_stag", app)
        mock_d.assert_called_once_with(run)
        self.assertNotIn("02: Critique_Spec", run.stagnation_hashes)

    def test_error_resume_dispatches(self):
        import app
        run = self._paused_run("ERROR", "/tmp/rd_err")
        with patch("app.dispatch") as mock_d:
            self._resume("/tmp/rd_err", app)
        mock_d.assert_called_once_with(run)

    def test_paused_by_user_retails_if_marker_set(self):
        """PAUSED_BY_USER with a marker should re-tail (existing behavior)."""
        import app
        run = RunState("P", "/tmp/rd_pu", "ARCH", "DEV")
        run.status = "PAUSED_BY_USER"; run.escalation_kind = None
        run.dispatch_marker = {"ts": "2026-06-01T10:00:00Z", "offset": 0, "target": "DEV"}
        app._run_states["/tmp/rd_pu"] = run
        with patch("app.dispatch") as mock_d:
            with app.app.test_client() as client:
                client.post('/api/resume', json={"cwd": "/tmp/rd_pu"})
        mock_d.assert_not_called()
        self.assertEqual(run.status, "DISPATCHED_AWAITING_EVENT")


# ── Commit approval → DONE ─────────────────────────────────────────────────────

class TestCommitApproval(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def test_commit_approval_resume_sets_done(self):
        import app
        run = RunState("P", "/tmp/commit_test", "ARCH", "DEV")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "COMMIT_APPROVAL"
        app._run_states["/tmp/commit_test"] = run
        with patch("app.release_if_last_run"):
            with app.app.test_client() as client:
                res = client.post('/api/resume', json={"cwd": "/tmp/commit_test"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(run.status, "DONE")

    def test_commit_approval_lock_released_if_last(self):
        import app
        run = RunState("P", "/tmp/commit_lock", "ARCH", "DEV")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "COMMIT_APPROVAL"
        run.controller_token = "tok"
        app._run_states["/tmp/commit_lock"] = run
        with patch("app.release_token") as mock_rel:
            with app.app.test_client() as client:
                client.post('/api/resume', json={"cwd": "/tmp/commit_lock"})
        mock_rel.assert_called_once_with(run)


# ── Abort persistence ──────────────────────────────────────────────────────────

class TestAbortPersistence(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def test_abort_marks_aborted_and_saves(self):
        import app
        run = RunState("P", "/tmp/abort_test", "ARCH", "DEV")
        app._run_states["/tmp/abort_test"] = run
        with patch.object(run, 'save') as mock_save:
            with app.app.test_client() as client:
                res = client.post('/api/abort', json={"cwd": "/tmp/abort_test"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(run.status, "ABORTED")
        mock_save.assert_called()

    def test_abort_removes_from_run_states(self):
        import app
        run = RunState("P", "/tmp/abort_remove", "ARCH", "DEV")
        app._run_states["/tmp/abort_remove"] = run
        with patch("app.release_if_last_run"):
            with app.app.test_client() as client:
                client.post('/api/abort', json={"cwd": "/tmp/abort_remove"})
        self.assertNotIn("/tmp/abort_remove", app._run_states)

    def test_abort_releases_lock_when_last(self):
        import app
        run = RunState("P", "/tmp/abort_lock", "ARCH", "DEV")
        run.controller_token = "tok"
        app._run_states["/tmp/abort_lock"] = run
        with patch("app.release_token") as mock_rel:
            with app.app.test_client() as client:
                client.post('/api/abort', json={"cwd": "/tmp/abort_lock"})
        mock_rel.assert_called_once_with(run)

    def test_abort_releases_lock(self):
        import app
        run = RunState("P", "/tmp/abort_hooks", "ARCH", "DEV")
        app._run_states["/tmp/abort_hooks"] = run
        with patch("app.release_if_last_run") as mock_cleanup:
            with app.app.test_client() as client:
                client.post('/api/abort', json={"cwd": "/tmp/abort_hooks"})
        mock_cleanup.assert_called_once_with(run)


class TestConclude(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def _terminal_run(self, cwd, status):
        import app
        run = RunState("P", cwd, "ARCH", "DEV")
        run.status = status
        app._run_states[cwd] = run
        return run

    def test_conclude_done_removes_from_states(self):
        import app
        self._terminal_run("/tmp/conclude_done", "DONE")
        with app.app.test_client() as client:
            res = client.post('/api/conclude', json={"cwd": "/tmp/conclude_done"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("/tmp/conclude_done", app._run_states)

    def test_conclude_error_removes_from_states(self):
        import app
        self._terminal_run("/tmp/conclude_err", "ERROR")
        with app.app.test_client() as client:
            res = client.post('/api/conclude', json={"cwd": "/tmp/conclude_err"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("/tmp/conclude_err", app._run_states)

    def test_conclude_non_terminal_rejected(self):
        import app
        self._terminal_run("/tmp/conclude_running", "RUNNING")
        with app.app.test_client() as client:
            res = client.post('/api/conclude', json={"cwd": "/tmp/conclude_running"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("/tmp/conclude_running", app._run_states)


# ── Turn timeout ───────────────────────────────────────────────────────────────

class TestTurnTimeout(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def _make_waiting_run(self, status, elapsed_sec=2000):
        run = RunState("P", "/tmp/timeout_test", "ARCH", "DEV")
        run.status = status
        # Set dispatch_marker to a time far enough in the past
        past = datetime.now(timezone.utc) - timedelta(seconds=elapsed_sec)
        ts = past.isoformat().replace("+00:00", "Z")
        run.dispatch_marker = {"ts": ts, "offset": 0, "target": "DEV"}
        return run

    def test_timeout_escalates_dispatched(self):
        import app
        app._config['limits']['turnTimeoutSec'] = 0  # expire immediately
        run = self._make_waiting_run("DISPATCHED_AWAITING_EVENT")
        _app_module.check_timeout(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "TURN_TIMEOUT")

    def test_timeout_escalates_human_answer(self):
        import app
        app._config['limits']['turnTimeoutSec'] = 0
        run = self._make_waiting_run("HUMAN_ANSWER_SENT_AWAITING_EVENT")
        _app_module.check_timeout(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "TURN_TIMEOUT")


    def test_no_timeout_within_limit(self):
        import app
        app._config['limits']['turnTimeoutSec'] = 9999
        run = self._make_waiting_run("DISPATCHED_AWAITING_EVENT")
        _app_module.check_timeout(run)
        self.assertEqual(run.status, "DISPATCHED_AWAITING_EVENT")

    def test_timeout_does_not_increment_total_turns(self):
        import app
        app._config['limits']['turnTimeoutSec'] = 0
        run = self._make_waiting_run("DISPATCHED_AWAITING_EVENT")
        turns_before = run.total_turns
        _app_module.check_timeout(run)
        self.assertEqual(run.total_turns, turns_before)


# ── Hook cleanup ───────────────────────────────────────────────────────────────


# ── Conductor lock lifecycle ──────────────────────────────────────────────────

class TestConductorLockLifecycle(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def test_release_called_when_last_run_done(self):
        import app
        run = RunState("P", "/tmp/done_test", "ARCH", "DEV")
        run.controller_token = "tok-abc"
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "COMMIT_APPROVAL"
        app._run_states["/tmp/done_test"] = run
        with patch("app.release_token") as mock_rel:
            with app.app.test_client() as client:
                client.post('/api/resume', json={"cwd": "/tmp/done_test"})
        mock_rel.assert_called_once_with(run)

    def test_release_not_called_when_peer_run_still_active(self):
        import app
        peer = RunState("PEER", "/tmp/peer", "ARCH", "DEV"); peer.status = "DISPATCHED_AWAITING_EVENT"
        app._run_states["/tmp/peer"] = peer
        run = RunState("P", "/tmp/done_peer", "ARCH", "DEV")
        run.controller_token = "tok-abc"
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "COMMIT_APPROVAL"
        app._run_states["/tmp/done_peer"] = run
        with patch("app.release_token") as mock_rel:
            with app.app.test_client() as client:
                client.post('/api/resume', json={"cwd": "/tmp/done_peer"})
        mock_rel.assert_not_called()

    def test_release_on_abort_last_run(self):
        import app
        run = RunState("P", "/tmp/abort_last", "ARCH", "DEV")
        run.controller_token = "tok-xyz"; run.status = "ABORTED"
        app._run_states = {}
        with patch("app.release_token") as mock_rel:
            release_if_last_run(run)
        mock_rel.assert_called_once_with(run)

    def test_no_release_on_abort_with_other_runs(self):
        import app
        peer = RunState("PEER", "/tmp/peer2", "ARCH", "DEV"); peer.status = "RUNNING"
        app._run_states["/tmp/peer2"] = peer
        run = RunState("P", "/tmp/abort_with_peer", "ARCH", "DEV"); run.status = "ABORTED"
        app._run_states["/tmp/abort_with_peer"] = run
        with patch("app.release_token") as mock_rel:
            release_if_last_run(run)
        mock_rel.assert_not_called()

    def test_all_runs_terminal_empty(self):
        import app; app._run_states = {}
        self.assertTrue(_all_runs_terminal())

    def test_all_runs_terminal_mixed(self):
        import app
        r1 = RunState("A","/a","X","Y"); r1.status="DONE"
        r2 = RunState("B","/b","X","Y"); r2.status="RUNNING"
        app._run_states = {"/a":r1,"/b":r2}
        self.assertFalse(_all_runs_terminal())

    def test_all_runs_terminal_all_done(self):
        import app
        r1 = RunState("A","/a","X","Y"); r1.status="DONE"
        r2 = RunState("B","/b","X","Y"); r2.status="ABORTED"
        app._run_states = {"/a":r1,"/b":r2}
        self.assertTrue(_all_runs_terminal())


# ── Human-answer dispatch ─────────────────────────────────────────────────────

class TestHumanAnswerDispatch(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def _mock_dispatch_result(self):
        from dataclasses import dataclass
        @dataclass
        class R: ok=True; error=""; session=None; composed=None
        return R()

    def test_answer_sets_human_answer_sent_status(self):
        run = RunState("P", "/tmp/ans_status", "ARCH", "DEV")
        run.controller_token = "tok"
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Developer"), \
             patch("app.conductor_core.dispatch", return_value=self._mock_dispatch_result()):
            _real_dispatch(run, "my answer")
        self.assertEqual(run.status, "HUMAN_ANSWER_SENT_AWAITING_EVENT")

    def test_answer_compose_includes_original_mesg(self):
        import app
        run = RunState("P", "/tmp/ans_compose", "ARCH", "DEV")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "QUESTION"
        run.node = "02: Critique_Spec"
        app._run_states["/tmp/ans_compose"] = run
        composed_msg = []
        def capture_dispatch(r, message_override=None):
            composed_msg.append(message_override)
            r.status = "HUMAN_ANSWER_SENT_AWAITING_EVENT"
        with patch("app.dispatch", side_effect=capture_dispatch):
            with app.app.test_client() as client:
                client.post('/api/answer', json={"cwd": "/tmp/ans_compose", "answer": "my reply"})
        self.assertEqual(len(composed_msg), 1)
        self.assertIn("my reply", composed_msg[0])
        self.assertIn("TempTBD_Questions.md", composed_msg[0])
        self.assertIn("Original topic instructions", composed_msg[0])

    def test_answer_compose_uses_configured_sentinel(self):
        """Composed prompt names the configured questionsSentinel, not a literal."""
        import app
        app._flow_config = dict(app._flow_config)
        app._flow_config['questionsSentinel'] = 'MyQuestions.md'
        run = RunState("P", "/tmp/ans_sentinel", "ARCH", "DEV")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "QUESTION"
        run.node = "02: Critique_Spec"
        app._run_states["/tmp/ans_sentinel"] = run
        composed_msg = []
        def capture_dispatch(r, message_override=None):
            composed_msg.append(message_override)
            r.status = "HUMAN_ANSWER_SENT_AWAITING_EVENT"
        with patch("app.dispatch", side_effect=capture_dispatch):
            with app.app.test_client() as client:
                client.post('/api/answer', json={"cwd": "/tmp/ans_sentinel", "answer": "reply"})
        self.assertEqual(len(composed_msg), 1)
        # The sentinel label line must name the configured sentinel
        self.assertIn("Human response to the pending MyQuestions.md questions:", composed_msg[0])
        # The default sentinel name must not appear in the label line
        self.assertNotIn("Human response to the pending TempTBD_Questions.md questions:", composed_msg[0])
        # Restore
        app._flow_config['questionsSentinel'] = 'TempTBD_Questions.md'

    def test_answer_guard_wrong_escalation(self):
        import app
        run = RunState("P", "/tmp/ans_guard", "ARCH", "DEV")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "LOOP_CAP"
        app._run_states["/tmp/ans_guard"] = run
        with app.app.test_client() as client:
            res = client.post('/api/answer', json={"cwd": "/tmp/ans_guard", "answer": "x"})
        self.assertEqual(res.status_code, 400)


# ── Pre-prompt initialization ─────────────────────────────────────────────────

class TestPrePromptInit(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def _mock_result(self):
        from dataclasses import dataclass
        @dataclass
        class R: ok=True; error=""; session=None; composed=None
        return R()

    def test_pre_prompt_bundled_into_first_dispatch(self):
        """First dispatch for a role bundles the Pre-Prompt into the topic turn,
        dispatched as a normal DISPATCHED_AWAITING_EVENT (no separate pre-prompt turn)."""
        run = RunState("P", "/tmp/pp_test", "ARCH", "DEV")
        run.roles_initialized = {}  # neither role initialized
        run.node = "02: Critique_Spec"
        dispatched_overrides = []
        def capture(cfg, project, target, topic, override=None):
            dispatched_overrides.append(override)
            return self._mock_result()
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Developer"), \
             patch("app.conductor_core.topic_message", return_value="TOPIC_BODY_42"), \
             patch("app.conductor_core.dispatch", side_effect=capture):
            _real_dispatch(run)
        self.assertEqual(run.status, "DISPATCHED_AWAITING_EVENT")
        self.assertTrue(run.roles_initialized.get("Developer"))
        msg = dispatched_overrides[0]
        self.assertIn("test agent", msg)       # Pre-Prompt (from valid_conductor.json)
        self.assertIn("TOPIC_BODY_42", msg)    # the topic body, same turn
        self.assertIn("run this exact command once", msg)  # appended completion line

    def test_no_pre_prompt_when_already_initialized(self):
        """Second dispatch for an initialized role goes directly to workflow topic."""
        run = RunState("P", "/tmp/pp_skip", "ARCH", "DEV")
        run.roles_initialized = {"Developer": True}
        run.node = "02: Critique_Spec"
        dispatched_overrides = []
        def capture(cfg, project, target, topic, override=None):
            dispatched_overrides.append(override)
            return self._mock_result()
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Developer"), \
             patch("app.conductor_core.topic_message", return_value="Read Task_FSD.md. Write TempTBD_Questions.md if questions exist."), \
             patch("app.conductor_core.dispatch", side_effect=capture):
            _real_dispatch(run)
        self.assertEqual(run.status, "DISPATCHED_AWAITING_EVENT")
        # In script mode (default), it's augmented, not None
        self.assertIn("Read Task_FSD.md", dispatched_overrides[0])
        self.assertIn("run this exact command once", dispatched_overrides[0])

    def test_pre_prompt_does_not_increment_total_turns(self):
        """Pre-prompt dispatch must not call transition() so total_turns stays 0."""
        run = RunState("P", "/tmp/pp_turns", "ARCH", "DEV")
        run.roles_initialized = {}
        run.node = "02: Critique_Spec"
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Developer"), \
             patch("app.conductor_core.dispatch", return_value=self._mock_result()):
            _real_dispatch(run)
        self.assertEqual(run.total_turns, 0)

    def test_check_for_event_advances_first_turn(self):
        """A turn-end event on the role's first (bundled) turn advances the FSM
        via transition() — there is no separate pre-prompt turn."""
        import app
        _setup_config()
        app._config["dispatchEventPrompt"] = {"initialWaitSec": 0}
        evdir = Path(app._config["eventDir"])
        cwd = "/tmp/pp_event_test"
        run = RunState("P", cwd, "ARCH_T", "DEV_T")
        run.status = "DISPATCHED_AWAITING_EVENT"
        run.roles_initialized = {"Developer": True}
        run.node = "02: Critique_Spec"
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run.dispatch_marker = {"ts": now_iso, "offset": 0, "target": "DEV_T", "dispatch_id": "test-id"}
        app._run_states[cwd] = run

        ev_file = evdir / (cwd.replace("/","_").strip("_") + ".ndjson")
        ev = {"ts": now_iso, "event": "Stop", "target": "DEV_T", "cwd": cwd, "dispatch_id": run.dispatch_marker.get("dispatch_id", "test-id")}
        ev_file.write_text(json.dumps(ev) + "\n")

        transitioned = []
        with patch("app.transition", side_effect=lambda r: transitioned.append(r.node)):
            _app_module.check_for_event(run)
        self.assertEqual(transitioned, ["02: Critique_Spec"])


# ── Persistence and backward compatibility ────────────────────────────────────

class TestPersistenceCompat(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def test_run_state_round_trips(self):
        run = RunState("P", "/tmp/resume_test", "ARCH", "DEV")
        run.status = "DISPATCHED_AWAITING_EVENT"
        run.dispatch_marker = {"ts": "2026-06-02T12:00:00Z", "offset": 42, "target": "DEV"}
        run.started_at = "2026-06-02T10:00:00Z"
        run.roles_initialized = {"Developer": True}
        run.save()
        loaded = RunState.load("/tmp/resume_test")
        self.assertEqual(loaded.status, "DISPATCHED_AWAITING_EVENT")
        self.assertEqual(loaded.dispatch_marker["target"], "DEV")
        self.assertEqual(loaded.started_at, "2026-06-02T10:00:00Z")
        self.assertTrue(loaded.roles_initialized.get("Developer"))

    def test_old_state_backfills_new_fields(self):
        """State file without new fields must load without error; new fields default."""
        old_state = {
            "project":"P","cwd":"/tmp/old","arch":"A","dev":"D",
            "node":"04: Implement_Spec","status":"RUNNING",
            "total_turns":2,"counters":{"Validate":1},"history":[],
            "watermark":0,"dispatch_marker":{"ts":"","offset":0,"target":""},
            "controller_token":None,"escalation_kind":None,"stagnation_hashes":{}
        }
        import app
        path = Path(app._config["logDir"]) / "tmp_old.state.json"
        with open(path,"w") as f: json.dump(old_state, f)
        run = RunState.load("/tmp/old")
        self.assertIsNotNone(run)
        self.assertIsNotNone(run.started_at)
        self.assertEqual(run.roles_initialized, {})
        self.assertIsNone(run.pending_workflow_node)

    def test_run_state_compat_fixture(self):
        """Fixture file with all new fields loads without error."""
        d = _load_json("run_state_compat.json")
        import app
        path = Path(app._config["logDir"]) / "tmp_tst_project.state.json"
        with open(path,"w") as f: json.dump(d, f)
        run = RunState.load("/tmp/tst_project")
        self.assertIsNotNone(run)
        self.assertEqual(run.started_at, "2026-06-02T10:00:00Z")
        self.assertTrue(run.roles_initialized.get("Developer"))
        self.assertEqual(run.counters["Validate"], 1)

    def test_state_started_at_format(self):
        """started_at stored as ISO; fmt_ts converts for display."""
        run = RunState("P", "/tmp/ts_test", "A", "D")
        dt = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
        display = fmt_ts(dt)
        self.assertEqual(display, dt.astimezone().strftime("%Y-%m%d-%H:%M:%S"))

    def test_canonical_status_enum_strings(self):
        expected = {
            "RUNNING","DISPATCHED_AWAITING_EVENT","PAUSED_FOR_HUMAN","PAUSED_BY_USER",
            "HUMAN_ANSWER_SENT_AWAITING_EVENT",
            "DONE","ABORTED","ERROR"
        }
        used = set()
        for s in expected:
            r = RunState("P","/tmp","A","D"); r.status = s; used.add(r.status)
        self.assertTrue(expected.issubset(used))

    def test_run_state_json_schema_fields(self):
        run = RunState("P","/tmp/schema","A","D"); run.controller_token="tok"
        data = run.to_dict()
        required = {"project","cwd","arch","dev","node","status","counters","history",
                    "watermark","dispatch_marker","controller_token","started_at",
                    "roles_initialized","pending_workflow_node"}
        self.assertTrue(required.issubset(set(data.keys())),
                        f"Missing fields: {required - set(data.keys())}")

    def test_startup_reloads_live_states_but_skips_terminal(self):
        import app
        with tempfile.TemporaryDirectory() as td:
            _setup_config(td)
            app._run_states.clear()
            live_cwd = os.path.join(td, "live")
            done_cwd = os.path.join(td, "done")
            os.makedirs(live_cwd, exist_ok=True)
            os.makedirs(done_cwd, exist_ok=True)

            live = RunState("P", live_cwd, "ARCH", "DEV")
            live.status = "DISPATCHED_AWAITING_EVENT"
            live.save()
            done = RunState("P", done_cwd, "ARCH", "DEV")
            done.status = "DONE"
            done.save()

            app.reload_scheduled_runs()

            self.assertIn(live_cwd, app._run_states)
            self.assertNotIn(done_cwd, app._run_states)


# ── Progress computation ───────────────────────────────────────────────────────

class TestProgressComputation(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def test_progress_on_shortest_path(self):
        run = RunState("P", "/tmp/prog", "A", "D")
        run.node = "02: Critique_Spec"; run.total_turns = 0
        p = get_run_progress(run)
        self.assertEqual(p["topicStep"], 0)
        self.assertGreater(p["topicTotal"], 0)
        self.assertFalse(p["offPath"])

    def test_progress_off_path_anchors_to_latest_mainline_step(self):
        run = RunState("P", "/tmp/prog2", "A", "D")
        run.node = "08: Clarify_Review"  # not on shortest path
        p = get_run_progress(run)
        self.assertTrue(p["offPath"])
        self.assertEqual(p["topicStep"], 3)  # anchor at 06: Review_Implementation

    def test_elapsed_time_positive(self):
        run = RunState("P", "/tmp/prog3", "A", "D")
        import time; time.sleep(0.01)
        p = get_run_progress(run)
        self.assertGreaterEqual(p["elapsedSec"], 0)

    def test_loop_counter_shown(self):
        run = RunState("P", "/tmp/prog4", "A", "D")
        run.node = "05: Validate_Implementation"
        run.counters["Validate"] = 2
        p = get_run_progress(run)
        self.assertEqual(p["loopCounter"], "Validate")
        self.assertEqual(p["loopVal"], 2)

    def test_no_loop_counter_outside_loop(self):
        run = RunState("P", "/tmp/prog5", "A", "D")
        run.node = "04: Implement_Spec"  # no loopCounter in config
        p = get_run_progress(run)
        self.assertIsNone(p["loopCounter"])
        self.assertIsNone(p["loopVal"])

    def test_questions_sentinel_in_progress(self):
        """questionsSentinel from flow config is exposed via get_run_progress."""
        import app
        run = RunState("P", "/tmp/prog_qs", "A", "D")
        p = get_run_progress(run)
        self.assertEqual(p["questionsSentinel"], "TempTBD_Questions.md")

    def test_custom_questions_sentinel_in_progress(self):
        """Custom questionsSentinel is returned correctly."""
        import app
        app._flow_config = dict(app._flow_config)
        app._flow_config['questionsSentinel'] = 'MyQuestions.md'
        run = RunState("P", "/tmp/prog_cqs", "A", "D")
        p = get_run_progress(run)
        self.assertEqual(p["questionsSentinel"], "MyQuestions.md")
        # Restore
        app._flow_config['questionsSentinel'] = 'TempTBD_Questions.md'


# ── In-process conductor core ──────────────────────────────────────────────────

class TestInProcessConductor(unittest.TestCase):
    def setUp(self):
        _setup_config()
        self.patcher = patch("app.conductor_core")
        self.mock_cc = self.patcher.start()
        self.lock_patcher = patch("app.core_lock")
        self.mock_lock = self.lock_patcher.start()
        self.mock_cc.topic_role.return_value = "Developer"
        import app
        app._conductor_config_cache = _load_json("valid_conductor.json")

    def tearDown(self):
        self.patcher.stop(); self.lock_patcher.stop()

    def test_acquire_token_reconciles(self):
        import app
        run = RunState("P","/tmp","A","D"); run.controller_token = "stale"
        self.mock_lock.take.return_value = "fresh"
        tok = app.acquire_token(run)
        self.assertEqual(tok, "fresh"); self.assertEqual(run.controller_token, "fresh")

    def test_acquire_token_returns_none_on_fail(self):
        import app
        run = RunState("P","/tmp","A","D")
        self.mock_lock.take.return_value = None
        self.assertIsNone(app.acquire_token(run))

    def test_dispatch_resolves_role_and_calls_core(self):
        import app
        run = RunState("P","/tmp","ARCH_T","DEV_T")
        run.node = "02: Critique_Spec"
        run.roles_initialized = {"Developer": True}  # skip pre-prompt
        self.mock_lock.take.return_value = "tok"
        class R: ok=True; error=""; session=None; composed=None
        self.mock_cc.dispatch.return_value = R()
        _real_dispatch(run)
        self.mock_cc.dispatch.assert_called_once()
        self.assertEqual(run.status, "DISPATCHED_AWAITING_EVENT")

    def test_unknown_topic_escalates(self):
        import app
        run = RunState("P","/tmp","A","D"); run.node = "bad"
        run.roles_initialized = {"Developer": True}
        self.mock_lock.take.return_value = "tok"
        class UT(KeyError): pass
        app.conductor_core.UnknownTopic = UT
        self.mock_cc.topic_role.side_effect = UT("bad")
        with patch("app.notify_operator"): _real_dispatch(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "ERROR")

    def test_dispatch_failure_escalates(self):
        import app
        run = RunState("P","/tmp","A","D")
        run.roles_initialized = {"Developer": True}
        self.mock_lock.take.return_value = "tok"
        class R: ok=False; error="tmux missing"; session=None; composed=None
        self.mock_cc.dispatch.return_value = R()
        with patch("app.notify_operator"): _real_dispatch(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "ERROR")


# ── Hook preflight ─────────────────────────────────────────────────────────────


# ── Config error surfacing ─────────────────────────────────────────────────────

class TestConfigErrorSurfacing(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_flow_config_error_in_conductor_endpoint(self):
        import app; app._flow_config_error = "Test error"
        with app.app.test_client() as client:
            res = client.get('/api/conductor-config')
        self.assertEqual(res.status_code, 200, res.get_json())
        data = json.loads(res.data)
        self.assertEqual(data.get("flowConfigError"), "Test error")
        app._flow_config_error = None  # restore

    def test_start_run_blocked_on_config_error(self):
        import app; app._flow_config_error = "bad config"
        with app.app.test_client() as client:
            res = client.post('/api/start', json={"project":"P","arch":"A","dev":"D"})
        self.assertEqual(res.status_code, 503)
        app._flow_config_error = None

    def test_conductor_config_exception_returns_200_with_error(self):
        """F3: conductor config exception must return HTTP 200 so UI can parse and render error."""
        import app
        with patch("app.get_conductor_config", side_effect=Exception("bad JSON")):
            with app.app.test_client() as client:
                res = client.get('/api/conductor-config')
        self.assertEqual(res.status_code, 200, res.get_json())
        data = json.loads(res.data)
        self.assertIn("error", data)
        self.assertIn("bad JSON", data["error"])

    def test_questions_sentinel_configurable(self):
        """F2: questionsSentinel from flow config, not hardcoded."""
        import app
        # Override flow config with custom questionsSentinel
        app._flow_config = dict(app._flow_config)
        app._flow_config['questionsSentinel'] = 'MyQuestions.md'
        app._flow_config['questionsBackTo'] = '03: Answer_Update_Spec'
        # Create the custom sentinel file
        Path(self.tmpdir, "MyQuestions.md").write_text("custom q")
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "04: Implement_Spec"  # topic with no per-topic questions config
        with patch("app.dispatch") as mock_d:
            _app_module.transition(run)
        self.assertEqual(run.node, "03: Answer_Update_Spec")

    def test_invalid_questions_sentinel_rejected(self):
        """F2: questionsSentinel path traversal must be rejected at validation time."""
        conductor = _load_json("valid_conductor.json")
        flow = _load_json("valid_flow.json")
        flow['questionsSentinel'] = '../escape.md'
        errs = validate_flow_config(flow, conductor)
        self.assertTrue(any("questionsSentinel" in e and "safe relative" in e for e in errs))


# ── Per-run start topic selection (entry topics) ───────────────────────────────

class TestEntryTopics(unittest.TestCase):
    def setUp(self):
        self.conductor = _load_json("valid_conductor.json")

    def test_valid_flow_with_entry_topics_passes(self):
        flow = _load_json("valid_flow.json")
        errs = validate_flow_config(flow, self.conductor)
        self.assertEqual(errs, [], f"Unexpected errors: {errs}")

    def test_entry_topic_not_in_conductor_rejected(self):
        flow = _load_json("valid_flow.json")
        flow["entryTopics"] = ["99: Fake_Topic"]
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("entryTopics" in e and "99: Fake_Topic" in e for e in errs))

    def test_entry_topic_unreachable_end_rejected(self):
        flow = _load_json("valid_flow.json")
        # Break 10's forward edge so endTopic is no longer reachable from it.
        flow["topics"]["10: Update_Docs_And_Goldens"]["nextTo"] = "09: Address_Review"
        flow["entryTopics"] = ["10: Update_Docs_And_Goldens"]
        errs = validate_flow_config(flow, self.conductor)
        self.assertTrue(any("entryTopics" in e and "unreachable" in e.lower() for e in errs))

    def test_entry_topics_ordered_defaults_to_start_only(self):
        flow = _load_json("valid_flow.json")
        del flow["entryTopics"]
        self.assertEqual(entry_topics_ordered(flow), [flow["startTopic"]])

    def test_entry_topics_ordered_preserves_author_order(self):
        flow = _load_json("valid_flow.json")
        self.assertEqual(entry_topics_ordered(flow), [
            "02: Critique_Spec", "04: Implement_Spec", "06: Review_Implementation",
            "09: Address_Review", "10: Update_Docs_And_Goldens"])

    def test_entry_topics_ordered_prepends_start_if_absent(self):
        flow = _load_json("valid_flow.json")
        flow["entryTopics"] = ["06: Review_Implementation"]
        menu = entry_topics_ordered(flow)
        self.assertEqual(menu[0], flow["startTopic"])
        self.assertIn("06: Review_Implementation", menu)


class TestPerRunStartTopic(unittest.TestCase):
    def setUp(self):
        _setup_config()

    def test_start_node_sets_initial_node(self):
        run = RunState("P", "/tmp/sn1", "A", "D", start_node="06: Review_Implementation")
        self.assertEqual(run.start_node, "06: Review_Implementation")
        self.assertEqual(run.node, "06: Review_Implementation")

    def test_default_start_node_is_flow_start(self):
        run = RunState("P", "/tmp/sn2", "A", "D")
        self.assertEqual(run.start_node, "02: Critique_Spec")
        self.assertEqual(run.node, "02: Critique_Spec")

    def test_start_node_in_schema(self):
        run = RunState("P", "/tmp/sn3", "A", "D", start_node="04: Implement_Spec")
        self.assertIn("start_node", run.to_dict())

    def test_start_node_round_trips(self):
        run = RunState("P", "/tmp/sn_rt", "A", "D", start_node="06: Review_Implementation")
        run.save()
        loaded = RunState.load("/tmp/sn_rt")
        self.assertEqual(loaded.start_node, "06: Review_Implementation")

    def test_old_state_backfills_start_node_to_global_start(self):
        import app
        old = {"project":"P","cwd":"/tmp/sn_old","arch":"A","dev":"D",
               "node":"06: Review_Implementation","status":"RUNNING","total_turns":3,
               "counters":{},"history":[],"watermark":0,
               "dispatch_marker":{"ts":"","offset":0,"target":""},
               "controller_token":None,"escalation_kind":None,"stagnation_hashes":{}}
        path = Path(app._config["logDir"]) / "tmp_sn_old.state.json"
        with open(path,"w") as f: json.dump(old, f)
        run = RunState.load("/tmp/sn_old")
        self.assertEqual(run.start_node, "02: Critique_Spec")

    def test_progress_path_from_custom_start_node(self):
        run = RunState("P", "/tmp/sn_prog", "A", "D", start_node="06: Review_Implementation")
        p = get_run_progress(run)
        # shortest path 06 -> 10 -> 11 => 2 edges, currently at step 0, on-path
        self.assertEqual(p["topicStep"], 0)
        self.assertEqual(p["topicTotal"], 2)
        self.assertFalse(p["offPath"])


class TestStartRunSelection(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        _setup_config(self.td)
        Path(self.td, "Task_FSD.md").write_text("fsd")
        Path(self.td, "Task_CodeReview.md").write_text("cr")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _tmux_mock(self):
        m = MagicMock(); m.stdout = self.td + "\n"; return m

    def _post(self, client, **body):
        with patch("app.subprocess.run", return_value=self._tmux_mock()), \
             patch("app.preflight_hooks", return_value=True), \
             patch("app.dispatch"):
            return client.post('/api/start', json=body)

    def test_invalid_start_topic_rejected(self):
        import app
        with app.app.test_client() as client:
            res = self._post(client, project="P", arch="ARCH", dev="DEV",
                             startTopic="08: Clarify_Review")  # not an entry point
        self.assertEqual(res.status_code, 400)

    def test_fresh_run_uses_chosen_start(self):
        import app
        with app.app.test_client() as client:
            res = self._post(client, project="P", arch="ARCH", dev="DEV",
                             startTopic="06: Review_Implementation")
        self.assertEqual(res.status_code, 200, res.get_json())
        run = app._run_states[self.td]
        self.assertEqual(run.start_node, "06: Review_Implementation")
        self.assertEqual(run.node, "06: Review_Implementation")

    def test_omitted_start_defaults_to_flow_start(self):
        import app
        with app.app.test_client() as client:
            res = self._post(client, project="P", arch="ARCH", dev="DEV")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(app._run_states[self.td].start_node, "02: Critique_Spec")

    def test_live_persisted_state_resumes_ignoring_choice(self):
        import app
        prior = RunState("P", self.td, "ARCH", "DEV", start_node="02: Critique_Spec")
        prior.node = "04: Implement_Spec"; prior.status = "PAUSED_FOR_HUMAN"
        prior.save()
        with app.app.test_client() as client:
            res = self._post(client, project="P", arch="ARCH", dev="DEV",
                             startTopic="06: Review_Implementation")
        self.assertEqual(res.status_code, 200, res.get_json())
        run = app._run_states[self.td]
        self.assertEqual(run.node, "04: Implement_Spec")       # resumed live run
        self.assertEqual(run.start_node, "02: Critique_Spec")  # chosen start ignored

    def test_terminal_persisted_state_yields_to_fresh_choice(self):
        import app
        done = RunState("P", self.td, "ARCH", "DEV")
        done.status = "DONE"; done.save()
        with app.app.test_client() as client:
            res = self._post(client, project="P", arch="ARCH", dev="DEV",
                             startTopic="06: Review_Implementation")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(app._run_states[self.td].start_node, "06: Review_Implementation")


# ── Branding (Task 1) ──────────────────────────────────────────────────────────

class TestBranding(unittest.TestCase):
    def setUp(self):
        _setup_config()
        # F2-R: Ensure real logo exists and is non-empty; fail (don't skip) if missing
        self.real_logo = Path(_app_module.app.root_path) / "static" / "LLMDirector.png"
        self.assertTrue(self.real_logo.exists(), f"Logo asset missing at {self.real_logo}")
        self.assertGreater(self.real_logo.stat().st_size, 0, f"Logo asset at {self.real_logo} is empty")

    def test_logo_route_serves_png(self):
        with _app_module.app.test_client() as client:
            res = client.get('/logo.png')
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.content_type, "image/png")

    def test_index_template_branding(self):
        with _app_module.app.test_client() as client:
            res = client.get('/')
        html = res.data.decode()
        self.assertIn("<title>[SPISim] LLMDirector</title>", html)
        self.assertIn('<link rel="icon" type="image/png" href="/logo.png">', html)
        self.assertIn('<img src="/logo.png" alt="Logo"', html)
        self.assertIn("[SPISim] LLMDirector", html)


# ── Hook resolution (Task 3) ───────────────────────────────────────────────────


# ── Script Generation (Task 3) ─────────────────────────────────────────────────

class TestScriptGeneration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_script_generated_when_missing(self):
        import app
        hook_path = Path(self.tmpdir) / "hook.sh"
        app._config["Hook"] = str(hook_path)
        app._config["eventDir"] = "/tmp/events"
        preflight_hooks(self.tmpdir, "ARCH", "DEV")
        self.assertTrue(hook_path.exists())
        content = hook_path.read_text()
        # Single LLMHookEvent.sh deployed with eventDir injected
        self.assertIn('EVENT_DIR="/tmp/events"', content)
        self.assertIn('--prompt', content)
        self.assertEqual(hook_path.stat().st_mode & 0o777, 0o755)

    def test_script_regenerated_when_stale(self):
        import app
        hook_path = Path(self.tmpdir) / "hook.sh"
        hook_path.write_text('EVENT_DIR="/old/events"', encoding='utf-8')
        app._config["Hook"] = str(hook_path)
        app._config["eventDir"] = "/new/events"
        preflight_hooks(self.tmpdir, "ARCH", "DEV")
        self.assertIn('EVENT_DIR="/new/events"', hook_path.read_text())

    def test_script_reused_when_current(self):
        import app
        hook_path = Path(self.tmpdir) / "hook.sh"
        app._config["Hook"] = str(hook_path)
        app._config["eventDir"] = "/same/events"
        # First deploy creates it; second must not rewrite (mtime + content stable)
        preflight_hooks(self.tmpdir, "ARCH", "DEV")
        first = hook_path.read_text()
        mtime = hook_path.stat().st_mtime_ns
        preflight_hooks(self.tmpdir, "ARCH", "DEV")
        self.assertEqual(hook_path.read_text(), first)
        self.assertEqual(hook_path.stat().st_mtime_ns, mtime)


# ── Prompt Augmentation (Task 3) ───────────────────────────────────────────────

class TestPromptAugmentation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)
        import app
        app._config["Hook"] = "/bin/hook.sh"
        app._config["HumanNotifyScript"] = "/bin/notify.sh"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dispatch_augmentation_topic_turn(self):
        import app
        run = RunState("P", self.tmpdir, "ARCH_T", "DEV_T")
        run.node = "02: Critique_Spec"
        run.roles_initialized = {"Developer": True}
        run.controller_token = "tok"
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Developer"), \
             patch("app.conductor_core.topic_message", return_value="Base Task"), \
             patch("app.conductor_core.dispatch") as mock_d:
            _real_dispatch(run)

        args = mock_d.call_args[0]
        msg = args[4]
        self.assertIn("Base Task", msg)
        self.assertIn("run this exact command once as your final action", msg)
        self.assertIn("If the command does not print EVENT_SENT_OK, run this script to notify the human: /bin/notify.sh DEV_T and then stop.", msg)

    def test_dispatch_augmentation_human_notify_end_topic(self):
        import app
        run = RunState("P", self.tmpdir, "ARCH_T", "DEV_T")
        run.node = "11: Ready_To_Commit"
        run.roles_initialized = {"Developer": True}
        run.controller_token = "tok"
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Developer"), \
             patch("app.conductor_core.topic_message", return_value="Base Task"), \
             patch("app.conductor_core.dispatch") as mock_d:
            _real_dispatch(run)

        msg = mock_d.call_args[0][4]
        self.assertIn("notify the human: /bin/notify.sh DEV_T", msg)

    def test_dispatch_augmentation_human_notify_escalatable(self):
        import app
        run = RunState("P", self.tmpdir, "ARCH_T", "DEV_T")
        run.node = "06: Review_Implementation" # escalatable
        run.roles_initialized = {"Architect": True}
        run.controller_token = "tok"
        with patch("app.acquire_token", return_value="tok"), \
             patch("app.conductor_core.topic_role", return_value="Architect"), \
             patch("app.conductor_core.topic_message", return_value="Base Task"), \
             patch("app.conductor_core.dispatch") as mock_d:
            _real_dispatch(run)

        msg = mock_d.call_args[0][4]
        self.assertIn("If you raise a [HUMAN] question this turn, also run this script: /bin/notify.sh ARCH_T", msg)


# ── Hook Validation (Task 3) ───────────────────────────────────────────────────

class TestHookValidation(unittest.TestCase):
    def test_validation_unusable_path_rejected(self):
        import app
        conductor = _load_json("valid_conductor.json")
        flow = _load_json("valid_flow.json")
        # Point to a path where parent exists but is unwritable (e.g. /root/hook.sh)
        # or where parent doesn't exist and closest parent is unwritable.
        # /proc is usually not writable for new files.
        app._config = {"Hook": "/proc/director_test_hook.sh"}
        errs = validate_flow_config(flow, conductor)
        self.assertTrue(any("not writable" in e for e in errs))


# ── Stagnation detail (names the offending file) ──────────────────────────────

class TestStagnationDetail(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_detect_stagnation_returns_offending_files(self):
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "02: Critique_Spec"   # backIf sentinel = TempTBD_Questions.md
        (Path(self.tmpdir) / "TempTBD_Questions.md").write_text("q")
        self.assertEqual(_app_module.detect_stagnation(run), [])            # first time: record
        self.assertEqual(_app_module.detect_stagnation(run),
                         ["TempTBD_Questions.md"])                          # unchanged: stagnates

    def test_stagnation_escalation_names_file(self):
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "02: Critique_Spec"
        (Path(self.tmpdir) / "TempTBD_Questions.md").write_text("q")
        with patch("app.transition"):  # avoid full FSM; we call _escalate path via detect
            _app_module.detect_stagnation(run)  # prime the hash
            stag = _app_module.detect_stagnation(run)
            with patch("app.notify_operator"):
                _app_module._escalate(run, "STAGNATION", detail="delete to continue: " + ", ".join(stag))
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "STAGNATION")
        self.assertIn("TempTBD_Questions.md", run.escalation_detail)



# ── Event Script Execution (xta/bin/LLMHookEvent.sh) ──────────────────────────

class TestEventScript(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.script = str(Path(__file__).resolve().parent.parent / "bin" / "LLMHookEvent.sh")
        self.env = os.environ.copy()
        self.env["HOME"] = self.tmpdir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_strict_arguments(self):
        res = subprocess.run([self.script, "--prompt", "CODEX", "Stop", "a"*32], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("EVENT_SEND_FAILED", res.stdout)

        res = subprocess.run([self.script, "--prompt", "CODEX", "Stop", "a"*32, "/cwd", "extra"], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)

    def test_invalid_dispatch_id_rejected(self):
        bad_ids = ["A"*32, "a"*31, "a"*33, "123e4567-e89b-12d3-a456-426614174000"]
        for bad_id in bad_ids:
            res = subprocess.run(
                [self.script, "--prompt", "CODEX", "Stop", bad_id, "/cwd"],
                capture_output=True, text=True, env=self.env
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("EVENT_SEND_FAILED", res.stdout)

    def test_unsupported_event_rejected(self):
        res = subprocess.run(
            [self.script, "--prompt", "CODEX", "BadEvent", "a"*32, "/cwd"],
            capture_output=True, text=True, env=self.env
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("EVENT_SEND_FAILED", res.stdout)

    def test_empty_positional_values_rejected(self):
        res = subprocess.run(
            [self.script, "--prompt", "", "Stop", "a"*32, "/cwd"],
            capture_output=True, text=True, env=self.env
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("EVENT_SEND_FAILED", res.stdout)

    def test_json_correctness_and_verification(self):
        # Pass a CWD with quotes and special chars to test JSON robustness
        tricky_cwd = "/tmp/dir with \"quotes\" and \\ slashes"
        did = "0123456789abcdef0123456789abcdef"
        res = subprocess.run([self.script, "--prompt", "CODEX", "Stop", did, tricky_cwd], capture_output=True, text=True, env=self.env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("EVENT_SENT_OK", res.stdout)

        # Verify it wrote valid NDJSON
        evdir = Path(self.tmpdir) / ".llmdirector" / "events"
        evfile = list(evdir.glob("*.ndjson"))[0]
        data = json.loads(evfile.read_text())
        self.assertEqual(data["cwd"], tricky_cwd)
        self.assertEqual(data["dispatch_id"], did)

    def test_idempotency(self):
        cwd = "/tmp/idem"
        did = "11111111111111111111111111111111"
        res1 = subprocess.run([self.script, "--prompt", "CODEX", "Stop", did, cwd], capture_output=True, text=True, env=self.env)
        self.assertEqual(res1.returncode, 0)

        evdir = Path(self.tmpdir) / ".llmdirector" / "events"
        evfile = list(evdir.glob("*.ndjson"))[0]
        lines_before = evfile.read_text().strip().split("\n")

        res2 = subprocess.run([self.script, "--prompt", "CODEX", "Stop", did, cwd], capture_output=True, text=True, env=self.env)
        self.assertEqual(res2.returncode, 0)

        lines_after = evfile.read_text().strip().split("\n")
        self.assertEqual(len(lines_before), len(lines_after))

    def test_canonical_cwd_drives_event_filename(self):
        real_dir = Path(self.tmpdir) / "real"
        real_dir.mkdir()
        alias_dir = Path(self.tmpdir) / "alias"
        alias_dir.symlink_to(real_dir, target_is_directory=True)
        did = "22222222222222222222222222222222"

        res = subprocess.run(
            [self.script, "--prompt", "CODEX", "Stop", did, str(alias_dir)],
            capture_output=True, text=True, env=self.env
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("EVENT_SENT_OK", res.stdout)

        evdir = Path(self.tmpdir) / ".llmdirector" / "events"
        expected = evdir / (str(real_dir.resolve()).replace("/", "_").strip("_") + ".ndjson")
        alias_named = evdir / (str(alias_dir).replace("/", "_").strip("_") + ".ndjson")
        self.assertTrue(expected.exists())
        self.assertFalse(alias_named.exists())
        data = json.loads(expected.read_text())
        self.assertEqual(data["cwd"], str(real_dir.resolve()))

# ── Correlation and Delay ──────────────────────────────────────────────────────

class TestCorrelationAndDelay(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)
        import app
        app._run_states.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_target_concurrent_projects(self):
        import app
        run1 = RunState("P1", "/tmp/p1", "ARCH", "DEV")
        run1.dispatch_marker = {"target": "DEV", "dispatch_id": "aaa", "ts": "2026-06-01T00:00:00Z"}
        run1.status = "DISPATCHED_AWAITING_EVENT"
        app._run_states["/tmp/p1"] = run1

        run2 = RunState("P2", "/tmp/p2", "ARCH", "DEV")
        run2.dispatch_marker = {"target": "DEV", "dispatch_id": "bbb", "ts": "2026-06-01T00:00:00Z"}
        run2.status = "DISPATCHED_AWAITING_EVENT"
        app._run_states["/tmp/p2"] = run2

        evdir = Path(app._config["eventDir"])
        evdir.mkdir(parents=True, exist_ok=True)
        # Event for run1 written to run2's event file (wrong cwd, won't match)
        f = evdir / "tmp_p2.ndjson"
        f.write_text(json.dumps({"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/p2", "dispatch_id": "aaa"}) + "\n")

        with patch("app.transition") as mock_t:
            _app_module.check_for_event(run2)
        mock_t.assert_not_called()
        self.assertEqual(run2.status, "DISPATCHED_AWAITING_EVENT")

    def test_stale_dispatch_id(self):
        import app
        run = RunState("P", "/tmp/p", "ARCH", "DEV")
        run.dispatch_marker = {"target": "DEV", "dispatch_id": "current", "ts": "2026-06-01T00:00:00Z"}
        run.status = "DISPATCHED_AWAITING_EVENT"
        app._run_states["/tmp/p"] = run

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_p.ndjson"
        f.write_text(json.dumps({"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/p", "dispatch_id": "stale"}) + "\n")

        with patch("app.transition") as mock_t:
            _app_module.check_for_event(run)
        mock_t.assert_not_called()
        self.assertEqual(run.status, "DISPATCHED_AWAITING_EVENT")

    def test_legacy_state_escalation(self):
        import app
        run = RunState("P", "/tmp/p", "ARCH", "DEV")
        run.dispatch_marker = {"target": "DEV", "ts": "2026-06-01T00:00:00Z"} # No dispatch_id
        run.status = "DISPATCHED_AWAITING_EVENT"
        app._run_states["/tmp/p"] = run

        with patch("app.notify_operator"):
            _app_module.check_for_event(run)
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "ERROR")

    def test_delay_triggers(self):
        import app
        app._config["dispatchEventPrompt"] = {"initialWaitSec": 10}

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_p.ndjson"
        f.write_text(json.dumps({"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/p", "dispatch_id": "did"}) + "\n")

        # flow-driven delay trigger for backIf, nextIf, escalateIf
        cases = [
            {"backIf": "f.txt"},
            {"nextIf": "f.txt", "nextIfTo": "next"},
            {"escalateIf": "f.txt", "escalateKind": "ERROR"}
        ]
        for topic_cfg in cases:
            app._flow_config = {"startTopic": "test", "topics": {"test": topic_cfg}}
            run = RunState("P", "/tmp/p", "ARCH", "DEV")
            run.node = "test"; run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}; run.status = "DISPATCHED_AWAITING_EVENT"
            with patch("app.transition") as mock_t:
                _app_module.check_for_event(run)
            self.assertEqual(run.status, "IN TRANSITION TO ...", f"Failed for {topic_cfg}")

        # universal questions guard trigger
        app._flow_config = {"startTopic": "test", "questionsSentinel": "Q.md", "questionsBackTo": "back", "topics": {"test": {}}}
        run = RunState("P", "/tmp/p", "ARCH", "DEV")
        run.node = "test"; run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}; run.status = "DISPATCHED_AWAITING_EVENT"
        with patch("app.transition") as mock_t:
            _app_module.check_for_event(run)
        self.assertEqual(run.status, "IN TRANSITION TO ...")

    def test_questions_back_topic_does_not_trigger_universal_delay(self):
        import app
        app._config["dispatchEventPrompt"] = {"initialWaitSec": 10}

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_qb.ndjson"
        f.write_text(json.dumps({"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/qb", "dispatch_id": "did"}) + "\n")

        app._flow_config = {"startTopic": "test", "questionsSentinel": "Q.md", "questionsBackTo": "back", "topics": {"back": {}}}
        run = RunState("P", "/tmp/qb", "ARCH", "DEV")
        run.node = "back"; run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}; run.status = "DISPATCHED_AWAITING_EVENT"

        with patch("app.transition") as mock_t:
            _app_module.check_for_event(run)

        mock_t.assert_called_once_with(run)
        self.assertNotIn("transition_due_at", run.dispatch_marker)

    def test_no_delay_edge(self):
        import app
        app._config["dispatchEventPrompt"] = {"initialWaitSec": 10}

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_nd.ndjson"
        f.write_text(json.dumps({"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/nd", "dispatch_id": "did"}) + "\n")

        # no sentinel triggers: nextTo, action=validate, action=commit_approval
        cases = [
            {"nextTo": "next"},
            {"action": "validate", "passTo": "p", "failTo": "f"},
            {"action": "commit_approval"}
        ]
        for topic_cfg in cases:
            app._flow_config = {"startTopic": "test", "topics": {"test": topic_cfg}}
            run = RunState("P", "/tmp/nd", "ARCH", "DEV")
            run.node = "test"; run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}; run.status = "DISPATCHED_AWAITING_EVENT"
            with patch("app.transition") as mock_t:
                _app_module.check_for_event(run)
            mock_t.assert_called_once()
    def test_missing_field_events(self):
        import app
        run = RunState("P", "/tmp/p", "ARCH", "DEV")
        run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}
        run.status = "DISPATCHED_AWAITING_EVENT"
        app._run_states["/tmp/p"] = run

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_p.ndjson"

        # Test missing various required fields
        cases = [
            {"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/p"}, # missing did
            {"ts": "Z", "event": "Stop", "dispatch_id": "did", "cwd": "/tmp/p"}, # missing target
            {"ts": "Z", "target": "DEV", "dispatch_id": "did", "cwd": "/tmp/p"}, # missing event
            # missing cwd already covered in malformed_events_correlation
        ]

        for case in cases:
            f.write_text(json.dumps(case) + "\n")
            with patch("app.transition") as mock_t:
                _app_module.check_for_event(run)
            mock_t.assert_not_called()

            self.assertNotEqual(run.status, "IN TRANSITION TO ...")
            # If it called transition, it advanced the FSM (status should change or node should change)
            # In this test we mock transition so we just check it was called.

    def test_human_answer_dispatch_delay(self):
        import app
        app._config["dispatchEventPrompt"] = {"initialWaitSec": 10}
        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_ha.ndjson"
        f.write_text(json.dumps({"ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/ha", "dispatch_id": "did"}) + "\n")

        app._flow_config = {"startTopic": "test", "topics": {"test": {"backIf": "f.txt"}}}
        run = RunState("P", "/tmp/ha", "ARCH", "DEV")
        run.node = "test"; run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"};
        run.status = "HUMAN_ANSWER_SENT_AWAITING_EVENT"

        with patch("app.transition") as mock_t:
            _app_module.check_for_event(run)
        self.assertEqual(run.status, "IN TRANSITION TO ...")

    def test_matched_event_persisted_before_immediate_transition(self):
        import app
        app._flow_config = {"startTopic": "test", "topics": {"test": {"nextTo": "next"}}}

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_immediate.ndjson"
        f.write_text(json.dumps({
            "ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/immediate", "dispatch_id": "did"
        }) + "\n")

        run = RunState("P", "/tmp/immediate", "ARCH", "DEV")
        run.node = "test"
        run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}
        run.status = "DISPATCHED_AWAITING_EVENT"

        persisted = {}

        def assert_persisted_before_transition(arg_run):
            reloaded = RunState.load(arg_run.cwd)
            persisted["state"] = reloaded

        with patch("app.transition", side_effect=assert_persisted_before_transition) as mock_t:
            _app_module.check_for_event(run)

        mock_t.assert_called_once_with(run)
        reloaded = persisted["state"]
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.status, "IN TRANSITION TO ...")
        self.assertIn("event_matched_at", reloaded.dispatch_marker)
        self.assertEqual(reloaded.watermark, f.stat().st_size)

    def test_matched_event_state_persisted_before_log_decision(self):
        import app
        app._flow_config = {"startTopic": "test", "topics": {"test": {"nextTo": "next"}}}

        evdir = Path(app._config["eventDir"]); evdir.mkdir(parents=True, exist_ok=True)
        f = evdir / "tmp_logpersist.ndjson"
        f.write_text(json.dumps({
            "ts": "Z", "event": "Stop", "target": "DEV", "cwd": "/tmp/logpersist", "dispatch_id": "did"
        }) + "\n")

        run = RunState("P", "/tmp/logpersist", "ARCH", "DEV")
        run.node = "test"
        run.dispatch_marker = {"target": "DEV", "dispatch_id": "did", "ts": "Z"}
        run.status = "DISPATCHED_AWAITING_EVENT"

        def assert_persisted_before_logging(arg_run, _message):
            reloaded = RunState.load(arg_run.cwd)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, "IN TRANSITION TO ...")
            self.assertIn("event_matched_at", reloaded.dispatch_marker)
            self.assertEqual(reloaded.watermark, f.stat().st_size)

        with patch("app.log_decision", side_effect=assert_persisted_before_logging) as mock_log:
            with patch("app.transition") as mock_t:
                _app_module.check_for_event(run)

        mock_log.assert_called_once()
        mock_t.assert_called_once_with(run)

# ── Lifecycle during Transition ─────────────────────────────────────────────────

class TestTransitionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)
        import app
        app._run_states.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pause_resume_during_transition(self):
        import app
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        due = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        run.dispatch_marker = {"transition_due_at": due, "ts": "Z"}
        run.status = "IN TRANSITION TO ..."
        app._run_states[self.tmpdir] = run

        with app.app.test_client() as client:
            client.post('/api/pause', json={"cwd": self.tmpdir})
        self.assertEqual(run.status, "PAUSED_BY_USER")

        with patch("app.dispatch") as mock_d:
            with app.app.test_client() as client:
                client.post('/api/resume', json={"cwd": self.tmpdir})
        mock_d.assert_not_called()
        self.assertEqual(run.status, "IN TRANSITION TO ...")

    def test_abort_during_transition(self):
        import app
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.status = "IN TRANSITION TO ..."
        app._run_states[self.tmpdir] = run
        with app.app.test_client() as client:
            client.post('/api/abort', json={"cwd": self.tmpdir})
        self.assertEqual(run.status, "ABORTED")
        self.assertNotIn(self.tmpdir, app._run_states)

    def test_timeout_excluded_during_transition(self):
        import app
        app._config['limits']['turnTimeoutSec'] = 0
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.status = "IN TRANSITION TO ..."
        # Old dispatch timestamp that would normally timeout
        run.dispatch_marker = {"ts": "2026-06-01T00:00:00Z", "transition_due_at": "Z"}
        _app_module.check_timeout(run)
        self.assertEqual(run.status, "IN TRANSITION TO ...")

    def test_transition_no_early_read(self):
        import app
        # Topic with backIf sentinel
        app._flow_config = {"startTopic": "test", "topics": {"test": {"backIf": "Q.md", "backTo": "back"}}}
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "test"
        # Due in future
        due = (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat().replace("+00:00", "Z")
        run.dispatch_marker = {"transition_due_at": due, "ts": "Z"}
        run.status = "IN TRANSITION TO ..."
        app._run_states[self.tmpdir] = run

        # Create the sentinel that would trigger transition
        Path(self.tmpdir, "Q.md").touch()

        with patch("app.transition") as mock_t:
            _app_module.poll_runs()

        mock_t.assert_not_called()
        self.assertEqual(run.status, "IN TRANSITION TO ...")

    def test_transition_non_blocking(self):
        import app
        # Run 1: Delayed
        run1 = RunState("P1", self.tmpdir, "ARCH", "DEV")
        run1.status = "IN TRANSITION TO ..."
        run1.dispatch_marker = {"transition_due_at": "2099-01-01T00:00:00Z", "ts": "Z"}
        app._run_states[self.tmpdir] = run1

        # Run 2: Awaiting Event, timed out
        cwd2 = os.path.join(self.tmpdir, "r2")
        os.makedirs(cwd2, exist_ok=True)
        run2 = RunState("P2", cwd2, "ARCH", "DEV")
        run2.status = "DISPATCHED_AWAITING_EVENT"
        run2.dispatch_marker = {"ts": "2020-01-01T00:00:00Z", "target": "DEV", "dispatch_id": "did2"} # Timed out
        app._run_states[cwd2] = run2

        with patch("app.notify_operator"):
            _app_module.poll_runs()

        # Run 2 should have timed out even though Run 1 is delayed
        self.assertEqual(run2.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run2.escalation_kind, "TURN_TIMEOUT")

    def test_restart_during_transition_safe(self):
        import app
        app._config["logDir"] = self.tmpdir
        td = os.path.join(self.tmpdir, "restart_test")
        os.makedirs(td, exist_ok=True)
        Path(td, "Task_FSD.md").touch()
        Path(td, "Task_CodeReview.md").touch()
        run = RunState("P", td, "ARCH", "DEV")
        # Overdue
        due = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        run.dispatch_marker = {"transition_due_at": due, "ts": "Z", "target": "DEV"}
        run.status = "IN TRANSITION TO ..."
        run.save()
        app._run_states.clear()

        # 1. Restart reloads
        with patch("app.subprocess.run") as mock_sub, patch("app.preflight_hooks"):
            mock_sub.return_value = type("Res", (), {"stdout": td + "\n"})()
            with app.app.test_client() as client:
                res = client.post('/api/start', json={"project": "P", "arch": "ARCH", "dev": "DEV", "cwd": td})
        self.assertEqual(res.status_code, 200)
        run_loaded = app._run_states[td]

        # 2. Polling consumes and transitions
        with patch("app.transition") as mock_t:
            _app_module.poll_runs()

        mock_t.assert_called_once_with(run_loaded)
        self.assertNotIn("transition_due_at", run_loaded.dispatch_marker)

        # 3. Verify consumption was persisted
        reloaded = RunState.load(td)
        self.assertNotIn("transition_due_at", reloaded.dispatch_marker)

    def test_restart_during_transition_interrupted_idempotent(self):
        import app
        app._config["logDir"] = self.tmpdir
        td = os.path.join(self.tmpdir, "idempotent_test")
        os.makedirs(td, exist_ok=True)
        Path(td, "Task_FSD.md").touch()
        Path(td, "Task_CodeReview.md").touch()

        run = RunState("P", td, "ARCH", "DEV")
        # Turn 1 Matched
        run.total_turns = 1
        # Simulated Matched state: IN TRANSITION TO ..., NO transition_due_at (already popped but crashed before finishing transition)
        run.status = "IN TRANSITION TO ..."
        run.dispatch_marker = {"ts": "Z", "target": "DEV", "dispatch_id": "did"}
        run.save()
        app._run_states.clear()

        # 1. Restart reloads the run in this "Interrupted" state
        with patch("app.subprocess.run") as mock_sub, patch("app.preflight_hooks"):
            mock_sub.return_value = type("Res", (), {"stdout": td + "\n"})()
            with app.app.test_client() as client:
                res = client.post('/api/start', json={"project": "P", "arch": "ARCH", "dev": "DEV", "cwd": td})
        self.assertEqual(res.status_code, 200)
        run_loaded = app._run_states[td]
        self.assertEqual(run_loaded.total_turns, 1)

        # 2. Polling sees it and calls transition() again
        with patch("app.transition") as mock_t:
            _app_module.poll_runs()

        # Should have called transition once more to "finish" it
        mock_t.assert_called_once_with(run_loaded)
        # Verify it didn't double-increment total_turns (now handled at match time)
        self.assertEqual(run_loaded.total_turns, 1)

    def test_transition_recovery_after_partial_side_effect(self):
        import app
        app._config["logDir"] = self.tmpdir
        app._flow_config = {"startTopic": "test", "topics": {"test": {"action": "commit_approval"}}}
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.node = "test"
        run.status = "IN TRANSITION TO ..."

        with patch("app.notify_operator") as mock_notify:
            _app_module.transition(run)

        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "COMMIT_APPROVAL")
        mock_notify.assert_called_once()

        # Call transition again (recovery simulation)
        with patch("app.notify_operator") as mock_notify2:
            _app_module.transition(run)

        # Expected: No second notification if we implement idempotency
        mock_notify2.assert_not_called()
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")

    def test_restart_during_transition_stranded_recovery(self):
        import app
        app._config["logDir"] = self.tmpdir
        td = os.path.join(self.tmpdir, "strand_test")
        os.makedirs(td, exist_ok=True)
        Path(td, "Task_FSD.md").touch()
        Path(td, "Task_CodeReview.md").touch()
        run = RunState("P", td, "ARCH", "DEV")
        # Missing transition_due_at but IN TRANSITION TO ... (simulating crash after pop but before downstream save)
        run.dispatch_marker = {"ts": "Z", "target": "DEV"}
        run.status = "IN TRANSITION TO ..."
        run.save()
        app._run_states[td] = run

        with patch("app.transition") as mock_t:
            _app_module.poll_runs()

        # Should have recovered and called transition
        mock_t.assert_called_once_with(run)
        reloaded = RunState.load(td)
        self.assertIsNotNone(reloaded)
        self.assertNotIn("transition_due_at", reloaded.dispatch_marker)

    def test_restart_during_validation_safe(self):
        import app
        app._config["logDir"] = self.tmpdir
        td = os.path.join(self.tmpdir, "v_restart_test")
        os.makedirs(td, exist_ok=True)
        Path(td, "Task_FSD.md").touch()
        Path(td, "Task_CodeReview.md").touch()

        # Topic with validate action
        app._flow_config = {"startTopic": "test", "topics": {"test": {"action": "validate", "passTo": "p", "failTo": "f"}}}

        run = RunState("P", td, "ARCH", "DEV")
        run.node = "test"
        # Simulated Matched state: IN TRANSITION TO ..., v_fails present (crashed after validation but before finish)
        run.status = "IN TRANSITION TO ..."
        run.dispatch_marker = {"ts": "Z", "target": "DEV", "dispatch_id": "did", "v_fails": 0}
        run.save()
        app._run_states[td] = run

        with patch("app.subprocess.run") as mock_run, patch("app.dispatch") as mock_d:
            _app_module.poll_runs()

        # Should have skipped subprocess.run and called dispatch
        mock_run.assert_not_called()
        mock_d.assert_called_once()
        self.assertEqual(run.node, "p")

    def test_interrupted_validation_escalates(self):
        import app
        app._config["logDir"] = self.tmpdir
        td = os.path.join(self.tmpdir, "v_interrupted_test")
        os.makedirs(td, exist_ok=True)
        Path(td, "Task_FSD.md").touch()
        Path(td, "Task_CodeReview.md").touch()

        # Topic with validate action
        app._flow_config = {"startTopic": "test", "topics": {"test": {"action": "validate", "passTo": "p", "failTo": "f"}}}

        run = RunState("P", td, "ARCH", "DEV")
        run.node = "test"
        # Simulated state: crashed after v_started saved but before v_fails saved
        run.status = "IN TRANSITION TO ..."
        run.dispatch_marker = {"ts": "Z", "target": "DEV", "dispatch_id": "did", "v_started": True}
        run.save()
        app._run_states[td] = run

        with patch("app.subprocess.run") as mock_run, patch("app.notify_operator"):
            _app_module.poll_runs()

        # Should have detected the interruption and escalated
        mock_run.assert_not_called()
        self.assertEqual(run.status, "PAUSED_FOR_HUMAN")
        self.assertEqual(run.escalation_kind, "ERROR")
        self.assertIn("Validation interrupted", run.escalation_detail)


class TestInitialWaitSecValidation(unittest.TestCase):
    def test_invalid_initial_wait_sec(self):
        import app
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "LLMDirector.json"

            invalid_vals = [-1, True, False, float("inf"), float("nan"), "10", None]
            for val in invalid_vals:
                config_path.write_text(json.dumps({"dispatchEventPrompt": {"initialWaitSec": val}, "logDir": td, "eventDir": td}))
                app._flow_config_error = None
                app.load_config(str(config_path))
                self.assertIsNotNone(app._flow_config_error)

            # Explicit zero (valid)
            config_path.write_text(json.dumps({"dispatchEventPrompt": {"initialWaitSec": 0}, "logDir": td, "eventDir": td}))
            app._flow_config_error = None
            app.load_config(str(config_path))
            self.assertIsNone(app._flow_config_error)
            self.assertEqual(app._config["dispatchEventPrompt"]["initialWaitSec"], 0)

            # Missing (valid, defaults to 10)
            config_path.write_text(json.dumps({"logDir": td, "eventDir": td}))
            app._flow_config_error = None
            app.load_config(str(config_path))
            self.assertIsNone(app._flow_config_error)
            self.assertEqual(app.get_dispatch_event_prompt_config()["initialWaitSec"], 10)
            # Finite positive (valid)
            config_path.write_text(json.dumps({"dispatchEventPrompt": {"initialWaitSec": 15}, "logDir": td, "eventDir": td}))
            app._flow_config_error = None
            app.load_config(str(config_path))
            self.assertIsNone(app._flow_config_error)
            self.assertEqual(app._config["dispatchEventPrompt"]["initialWaitSec"], 15)



# ── Dispatch ID Contract (T-2) ────────────────────────────────────────────────

class TestDispatchIdContract(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _setup_config(self.tmpdir)
        import app
        app._run_states.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("conductor_core.dispatch")
    def test_dispatch_id_freshness_and_format(self, mock_dispatch):
        import app
        mock_dispatch.return_value = MagicMock(ok=True)
        run = RunState("P", self.tmpdir, "ARCH", "DEV")

        # Turn 1
        _app_module.dispatch(run)
        id1 = run.dispatch_marker["dispatch_id"]
        self.assertTrue(re.match(r"^[0-9a-f]{32}$", id1))

        # Turn 2
        _app_module.dispatch(run)
        id2 = run.dispatch_marker["dispatch_id"]
        self.assertNotEqual(id1, id2)
        self.assertTrue(re.match(r"^[0-9a-f]{32}$", id2))

    @patch("conductor_core.dispatch")
    def test_dispatch_augmentation_contract(self, mock_dispatch):
        import app
        mock_dispatch.return_value = MagicMock(ok=True)
        # Path with spaces to test quoting
        tricky_cwd = os.path.join(self.tmpdir, "tricky dir")
        os.makedirs(tricky_cwd, exist_ok=True)
        # Touch required files
        Path(tricky_cwd, "Task_FSD.md").touch()
        Path(tricky_cwd, "Task_CodeReview.md").touch()

        app._config["Hook"] = "/bin/my hook.sh"
        run = RunState("P", tricky_cwd, "ARCH", "DEV")

        _app_module.dispatch(run)

        # Get the final_msg passed to conductor_core.dispatch
        final_msg = mock_dispatch.call_args[0][4]
        did = run.dispatch_marker["dispatch_id"]

        # Assert full positional syntax with quoting
        # Expected: '/bin/my hook.sh' --prompt DEV Stop <did> <canonical_cwd>
        expected_cmd = f"'/bin/my hook.sh' --prompt DEV Stop {did} {shlex.quote(str(Path(tricky_cwd).resolve()))}"
        self.assertIn(expected_cmd, final_msg)

        # Assert absence of obsolete retry instructions
        self.assertNotRegex(final_msg.lower(), r"\bwait\s+\d")
        self.assertNotIn("retry", final_msg.lower())
        self.assertNotIn("attempts", final_msg.lower())

        # Assert exact wording
        self.assertIn("run this exact command once as your final action", final_msg)
        self.assertIn("If the command does not print EVENT_SENT_OK, run this script to notify the human:", final_msg)
    @patch("conductor_core.dispatch")
    def test_dispatch_id_human_answer(self, mock_dispatch):
        import app
        mock_dispatch.return_value = MagicMock(ok=True)
        run = RunState("P", self.tmpdir, "ARCH", "DEV")
        run.dispatch_marker = {"dispatch_id": "old-id", "ts": "Z"}

        _app_module.dispatch(run, message_override="Human answer")

        new_id = run.dispatch_marker["dispatch_id"]
        self.assertNotEqual(new_id, "old-id")
        self.assertTrue(re.match(r"^[0-9a-f]{32}$", new_id))

        final_msg = mock_dispatch.call_args[0][4]
        self.assertIn(new_id, final_msg)
        self.assertIn("Human answer", final_msg)


# ── Test runner ────────────────────────────────────────────────────────────────

def run_python_tests():
    suite = unittest.TestSuite()
    for cls in [
        TestFixtures, TestTimestampFormat, TestFlowConfigValidation, TestReachability,
        TestValidationParsing, TestFSMTransitions, TestResumeDispatch, TestCommitApproval,
        TestAbortPersistence, TestTurnTimeout, TestConductorLockLifecycle,
        TestHumanAnswerDispatch, TestPrePromptInit, TestPersistenceCompat, TestProgressComputation,
        TestInProcessConductor, TestConfigErrorSurfacing,
        TestEntryTopics, TestPerRunStartTopic, TestStartRunSelection,
        TestBranding, TestScriptGeneration, TestPromptAugmentation,
        TestHookValidation, TestStagnationDetail, TestEventScript, TestCorrelationAndDelay, TestTransitionLifecycle, TestInitialWaitSecValidation, TestDispatchIdContract
    ]:
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else len(result.failures) + len(result.errors)

def main():
    parser = argparse.ArgumentParser(description="LLMDirector Self-Test")
    parser.add_argument("-a", "--all", action="store_true", help="Run all tests")
    args = parser.parse_args()
    if not args.all:
        print("Usage: ./xta/tst/RunTest.py -a"); sys.exit(1)
    print("Running LLMDirector Python unit tests...")
    fail_count = run_python_tests()
    success = 1 if fail_count == 0 else 0
    print("\nSummary")
    print("  Report  : xta/tst/TestResult.html")
    print(f"  SUCCESS : {success}")
    print(f"  SKIPPED : 0")
    print(f"  FAIL    : {fail_count}")
    print(f"  Total   : {fail_count + success}")
    print(f"  Elapsed : 0s")
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
