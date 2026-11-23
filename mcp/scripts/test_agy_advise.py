"""Comprehensive tests for the integrated agy-advise MCP server and project decision core.

Run from the mcp directory after installing requirements.txt:
    .venv/bin/python -m unittest discover -s scripts -p 'test_*.py' -v
"""

import asyncio
import json
import os
import sqlite3
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from project import (  # noqa: E402
    Activate,
    Decision,
    Initialize,
    L3_DETAILED_TRIALS,
    L3_RECENT_DECISIONS,
    L3_SUMMARY_TRIALS,
    ListProjects,
    MAX_VERIFIED_FACTS,
    ProjectStore,
    Report,
    Review,
    Status,
    UpdateMemory,
    build_three_tier_packet,
    decode_decision,
    handle_project,
    public,
    render_markdown_summary,
    review_prompt,
)
from server import Advisor, build_advice_prompt, execute_advise, server  # noqa: E402
from telemetry import format_summary, read_jsonl, record_feedback, summarize_logs  # noqa: E402
import audit_report  # noqa: E402


class FakeAdvisor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def ask(self, prompt, session_id, timeout, effort=None, model=None):
        self.calls.append(
            {
                "prompt": prompt,
                "session_id": session_id,
                "timeout": timeout,
                "effort": effort,
                "model": model,
            }
        )
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = outcome(prompt)
        return {
            "advice": outcome,
            "session_id": "fresh-session",
            "duration_seconds": 1.2,
            "usage": {"total_tokens": 100},
            "model_reported": "fake-model",
            "queue_seconds": 0.0,
            "elapsed_seconds": 1.2,
            "requested_effort": effort,
            "model_requested": model,
        }


class ProjectCoreTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "projects.sqlite3"
        self.store = ProjectStore(self.db_path)
        self.inv = {"id": "inv-1", "tool": "agy_advise", "transport": "mcp"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_backwards_compatible_numeric_stagnation_and_pivot(self):
        init_evt = Initialize(
            id="robotics",
            phase="phase2",
            action="init",
            objective="Achieve >= 7/8 dynamic gate with single actor",
            frozen_gate="static 14/14, dynamic >= 7/8, ONNX pass",
            score_name="dynamic_pass",
            baseline_score=2,
            pass_score=7,
            route="local-actor-refinement",
        )
        state, must_review = self.store.apply(init_evt)
        self.assertFalse(must_review)
        self.assertTrue(public(state)["dispatch_allowed"])
        # Idempotent init replay succeeds
        state2, must_review2 = self.store.apply(init_evt)
        self.assertFalse(must_review2)
        self.assertEqual(state2["revision"], state["revision"])

        # Trial 1: mechanism A stays 2/8 -> record only
        rep1 = Report(
            id="robotics",
            phase="phase2",
            action="report",
            trial_id="ppo-tail",
            route="local-actor-refinement",
            cycle=0,
            mechanism="tail-ppo-kl",
            score=2,
            evidence="Dynamic gate remains 2/8.",
        )
        state, must_review = self.store.apply(rep1)
        self.assertFalse(must_review)
        self.assertTrue(public(state)["dispatch_allowed"])
        # Exact idempotent replay succeeds without double counting
        state_replay, must_review_replay = self.store.apply(rep1)
        self.assertFalse(must_review_replay)
        self.assertEqual(public(state_replay)["total_trials"], 1)

        # Trial 2: mechanism B also 2/8 -> stagnation triggered
        rep2 = Report(
            id="robotics",
            phase="phase2",
            action="report",
            trial_id="side-drift",
            route="local-actor-refinement",
            cycle=0,
            mechanism="side-drift-reward",
            score=2,
            evidence="Dynamic gate still 2/8, lateral drift worse.",
        )
        state, must_review = self.store.apply(rep2)
        self.assertTrue(must_review)
        self.assertFalse(public(state)["dispatch_allowed"])
        self.assertIn("stagnation", state["review_reasons"])

        # Prompt includes tried_routes and has no hardcoded robotics bias
        prompt = review_prompt(state, "", None)
        self.assertIn("tried_routes", prompt)
        self.assertIn("local-actor-refinement", prompt)
        self.assertNotIn("physical gate", prompt)

        # Legacy minimal Decision payload still validates
        minimal_decision = decode_decision(
            json.dumps(
                {
                    "decision": "PIVOT",
                    "why": "Two mechanisms failed to move the gate.",
                    "route": "end-to-end-closed-loop-rl",
                    "next_trial": "Full-param closed-loop RL seed 1",
                    "expected_signal": "Dynamic >= 5/8",
                    "stop_condition": "<= 3/8 after one cycle",
                    "max_trials": 3,
                }
            )
        )
        state, accepted = self.store.bind_decision(init_evt, state["revision"], minimal_decision, {"invocation_id": "1"})
        self.assertTrue(accepted)
        # Still paused until activated
        self.assertFalse(public(state)["dispatch_allowed"])

        state, _ = self.store.apply(
            Activate(id="robotics", phase="phase2", action="activate", decision_id=state["last_decision"]["id"])
        )
        self.assertTrue(public(state)["dispatch_allowed"])
        self.assertEqual(state["route"], "end-to-end-closed-loop-rl")
        self.assertEqual(state["cycle"], 1)

    def test_same_mechanism_spin_and_min_improvement(self):
        init_evt = Initialize(
            id="proj",
            phase="p1",
            action="init",
            objective="Raise accuracy to 90%",
            frozen_gate="Eval set >= 90%",
            score_name="acc",
            baseline_score=70.0,
            pass_score=90.0,
            min_improvement=1.0,
            same_mechanism_limit=3,
            route="hyperparam-sweep",
        )
        self.store.apply(init_evt)
        # Small noise (+0.3 < min_improvement=1.0) should NOT reset stagnation
        for idx, val in enumerate([70.3, 70.4, 70.2], start=1):
            state, must_review = self.store.apply(
                Report(
                    id="proj",
                    phase="p1",
                    action="report",
                    trial_id=f"t{idx}",
                    route="hyperparam-sweep",
                    cycle=0,
                    mechanism="lr-tweak",
                    score=val,
                    evidence=f"Noise run {idx}: {val}%",
                )
            )
            if idx < 3:
                self.assertFalse(must_review)
            else:
                self.assertTrue(must_review)
                self.assertIn("same_mechanism_spin", state["review_reasons"])

    def test_proxy_drift_catches_unscored_spinning(self):
        init_evt = Initialize(
            id="proj",
            phase="p1",
            action="init",
            objective="Hit real gate",
            frozen_gate="Real E2E >= 80%",
            score_name="e2e",
            baseline_score=40.0,
            pass_score=80.0,
            proxy_limit=3,
            route="offline-patches",
        )
        self.store.apply(init_evt)
        for idx in range(1, 4):
            state, must_review = self.store.apply(
                Report(
                    id="proj",
                    phase="p1",
                    action="report",
                    trial_id=f"proxy-{idx}",
                    route="offline-patches",
                    cycle=0,
                    mechanism=f"offline-{idx}",
                    evidence=f"Offline loss dropped {idx * 10}%",
                )
            )
            if idx < 3:
                self.assertFalse(must_review)
            else:
                self.assertTrue(must_review)
                self.assertIn("proxy_drift", state["review_reasons"])

        # Setting score with evidence_kind="proxy" must be rejected
        with self.assertRaises(ValueError):
            self.store.apply(
                Report(
                    id="proj",
                    phase="p1",
                    action="report",
                    trial_id="bad-proxy-score",
                    route="offline-patches",
                    cycle=0,
                    mechanism="m",
                    evidence_kind="proxy",
                    score=90.0,
                    evidence="Fake proxy score",
                )
            )

    def test_lower_is_better_numeric_gate(self):
        init_evt = Initialize(
            id="search",
            phase="latency",
            action="init",
            domain="performance",
            objective="Reduce P99 query latency <= 80ms",
            frozen_gate="Replay P99 <= 80ms with <0.2% recall loss",
            score_name="p99_ms",
            baseline_score=240.0,
            pass_score=80.0,
            score_direction="lower",
            min_improvement=5.0,
            route="index-pruning",
        )
        self.store.apply(init_evt)

        # Improvement from 240 -> 150
        state, must_review = self.store.apply(
            Report(
                id="search",
                phase="latency",
                action="report",
                trial_id="t1",
                route="index-pruning",
                cycle=0,
                mechanism="prune-tier1",
                score=150.0,
                evidence="P99 dropped to 150ms.",
            )
        )
        self.assertFalse(must_review)
        self.assertEqual(state["best_score"], 150.0)

        # Qualified at 75ms (<= 80ms)
        state, must_review = self.store.apply(
            Report(
                id="search",
                phase="latency",
                action="report",
                trial_id="t2",
                route="index-pruning",
                cycle=0,
                mechanism="cache-hot-postings",
                score=75.0,
                qualified=True,
                evidence="P99 reached 75ms, recall delta -0.05%.",
            )
        )
        self.assertTrue(must_review)
        self.assertIn("qualified_result", state["review_reasons"])

        # Reject bogus qualification above pass_score
        with self.assertRaises(ValueError):
            self.store.apply(
                Report(
                    id="search",
                    phase="latency",
                    action="report",
                    trial_id="t3",
                    route="index-pruning",
                    cycle=0,
                    mechanism="bad",
                    score=95.0,
                    qualified=True,
                    evidence="95ms should not qualify for <=80ms gate.",
                )
            )

    def test_qualitative_milestone_project_lifecycle_and_advance(self):
        init_evt = Initialize(
            id="order-service",
            phase="cutover",
            action="init",
            domain="architecture",
            objective="Eliminate lost updates on concurrent SKU writes",
            frozen_gate="100-concurrency stress test has 0 lost writes and API contract passes",
            non_negotiables=["Preserve HTTP API", "P99 increase < 10ms"],
            route="optimistic-lock",
            review_cadence=4,
        )
        state, must_review = self.store.apply(init_evt)
        self.assertFalse(must_review)
        self.assertEqual(public(state)["gate_mode"], "qualitative")

        # Numeric score on qualitative phase must fail
        with self.assertRaises(ValueError):
            self.store.apply(
                Report(
                    id="order-service",
                    phase="cutover",
                    action="report",
                    trial_id="bad-num",
                    route="optimistic-lock",
                    cycle=0,
                    mechanism="version-col",
                    score=1.0,
                    evidence="Should fail",
                )
            )

        # Blocker triggers immediate review without falsely incrementing proxy/stagnation
        state, must_review = self.store.apply(
            Report(
                id="order-service",
                phase="cutover",
                action="report",
                trial_id="t-block",
                route="optimistic-lock",
                cycle=0,
                mechanism="version-col",
                outcome="blocked",
                evidence="Legacy ORM ignores WHERE version predicate on batch updates.",
            )
        )
        self.assertTrue(must_review)
        self.assertEqual(state["review_reasons"], ["blocker"])
        self.assertEqual(state["consecutive_proxy"], 0)
        self.assertEqual(state["stagnant_mechanisms"], [])

        # Qualitative qualification + ADVANCE closes phase cleanly
        state, must_review = self.store.apply(
            Report(
                id="order-service",
                phase="cutover",
                action="report",
                trial_id="t-pass",
                route="optimistic-lock",
                cycle=0,
                mechanism="atomic-sql-cas",
                outcome="qualified",
                evidence="100-concurrency stress test passed with 0 lost updates, P99 +2ms.",
            )
        )
        self.assertTrue(must_review)
        adv = Decision(
            decision="ADVANCE",
            why="All frozen criteria verified with zero regressions.",
            global_assessment="Concurrency bug resolved without breaking API.",
            main_bottleneck="None remaining for this phase.",
        )
        state, accepted = self.store.bind_decision(init_evt, state["revision"], adv, {"invocation_id": "2"})
        self.assertTrue(accepted)
        pub = public(state)
        self.assertEqual(pub["phase_status"], "advanced")
        self.assertFalse(pub["review_required"])
        self.assertFalse(pub["dispatch_allowed"])

        # Further reports or reviews on an advanced phase are rejected
        with self.assertRaises(ValueError):
            self.store.apply(Review(id="order-service", phase="cutover", action="review"))

    async def test_deferred_batch_review_and_list_and_history_export(self):
        init_evt = Initialize(
            id="demo",
            phase="p1",
            action="init",
            objective="Goal",
            frozen_gate="Gate",
            score_name="s",
            baseline_score=1,
            pass_score=5,
            route="r1",
        )
        advisor = FakeAdvisor(
            [
                json.dumps(
                    {
                        "decision": "HOLD",
                        "why": "Need missing profiler evidence before changing route.",
                        "global_assessment": "Two trials stagnated without root-cause trace.",
                        "main_bottleneck": "Unmeasured bottleneck",
                        "stop_branches": ["blind-tuning"],
                        "missing_facts": ["CPU flamegraph under load"],
                        "unblock_actions": ["Capture CPU flamegraph on replay workload"],
                        "unblock_criteria": "Flamegraph identifies top >30% hotspot with reproducible trace.",
                        "verified_facts": ["Two parameter tweaks produced zero movement from baseline 1."],
                    }
                )
            ]
        )
        await handle_project(self.store, init_evt, advisor, self.inv, "", None, 60, None, None)
        await handle_project(
            self.store,
            Report(
                id="demo", phase="p1", action="report", trial_id="t1", route="r1",
                cycle=0, mechanism="m1", score=1, evidence="flat 1", auto_review=False,
            ),
            advisor, self.inv, "", None, 60, None, None,
        )
        res = await handle_project(
            self.store,
            Report(
                id="demo", phase="p1", action="report", trial_id="t2", route="r1",
                cycle=0, mechanism="m2", score=1, evidence="flat 2", auto_review=False,
            ),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(res["review_status"], "deferred")
        self.assertFalse(res["project"]["dispatch_allowed"])
        self.assertEqual(len(advisor.calls), 0)

        # Single consolidated review fires once
        rev = await handle_project(
            self.store,
            Review(id="demo", phase="p1", action="review"),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(rev["review_status"], "decided")
        self.assertEqual(rev["project"]["phase_status"], "held")
        self.assertFalse(rev["project"]["dispatch_allowed"])
        self.assertEqual(len(advisor.calls), 1)
        self.assertIsNone(advisor.calls[0]["session_id"])

        # List and Status with history
        listed = await handle_project(
            self.store, ListProjects(action="list"), advisor, self.inv, "", None, 60, None, None
        )
        self.assertEqual(len(listed["projects"]), 1)
        self.assertEqual(listed["projects"][0]["id"], "demo")

        st = await handle_project(
            self.store, Status(id="demo", phase="p1", action="status", include_history=True),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertIn("markdown_summary", st["history"])
        self.assertIn("# Project Ledger: demo / p1", st["history"]["markdown_summary"])

    async def test_stale_decision_and_transport_error_recovery(self):
        init_evt = Initialize(
            id="race",
            phase="p1",
            action="init",
            objective="Goal",
            frozen_gate="Gate",
            score_name="s",
            baseline_score=1,
            pass_score=5,
            stagnation_limit=1,
            route="r1",
        )
        self.store.apply(init_evt)

        def inject_concurrent_report(_prompt):
            self.store.apply(
                Report(
                    id="race", phase="p1", action="report", trial_id="t-concurrent",
                    route="r1", cycle=0, mechanism="m2", score=3, evidence="Improved concurrently!",
                )
            )
            return json.dumps(
                {
                    "decision": "HOLD",
                    "why": "Stale hold.",
                    "unblock_actions": ["Re-check metric"],
                    "unblock_criteria": "Verified trace",
                }
            )

        advisor = FakeAdvisor([RuntimeError("timeout"), inject_concurrent_report])

        # First review fails with transport error -> review_required preserved
        res1 = await handle_project(
            self.store,
            Report(
                id="race", phase="p1", action="report", trial_id="t1", route="r1",
                cycle=0, mechanism="m1", score=1, evidence="Stagnant",
            ),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(res1["review_status"], "transport_error")
        self.assertTrue(res1["project"]["review_required"])
        self.assertEqual(res1["project"]["last_review_issue"]["status"], "transport_error")

        # Second review receives concurrent new evidence -> stale!
        res2 = await handle_project(
            self.store, Review(id="race", phase="p1", action="review"),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(res2["review_status"], "stale")
        self.assertEqual(res2["project"]["best_score"], 3.0)

    def test_legacy_db_row_compatibility(self):
        legacy_state = {
            "id": "legacy",
            "phase": "p0",
            "objective": "Old objective",
            "frozen_gate": "Old gate",
            "score_name": "pass_cnt",
            "baseline_score": 2,
            "pass_score": 7,
            "route": "old-route",
            "stagnation_limit": 2,
            "initial": {
                "id": "legacy",
                "phase": "p0",
                "objective": "Old objective",
                "frozen_gate": "Old gate",
                "score_name": "pass_cnt",
                "baseline_score": 2,
                "pass_score": 7,
                "route": "old-route",
                "stagnation_limit": 2,
            },
            "revision": 1,
            "cycle": 0,
            "cycle_trials": 0,
            "best_score": 2,
            "stagnant_mechanisms": [],
            "trials": {},
            "review_reasons": [],
            "qualified_trial_id": None,
            "last_decision": None,
            "decisions": [],
            "plan": None,
            "routes": ["old-route"],
        }
        with self.store.transaction() as db:
            self.store.save(db, legacy_state)

        # Re-initializing with old fields must stay idempotent
        init_evt = Initialize(
            id="legacy",
            phase="p0",
            action="init",
            objective="Old objective",
            frozen_gate="Old gate",
            score_name="pass_cnt",
            baseline_score=2,
            pass_score=7,
            route="old-route",
            stagnation_limit=2,
        )
        state, must_review = self.store.apply(init_evt)
        self.assertFalse(must_review)
        self.assertEqual(public(state)["gate_mode"], "numeric")
        self.assertIn("Old objective", render_markdown_summary(state))

    def test_decode_decision_variants(self):
        payload = {
            "decision": "HOLD",
            "why": "Evidence incomplete.",
            "missing_facts": ["Latency breakdown"],
        }
        raw = json.dumps(payload)
        self.assertEqual(decode_decision(raw).decision, "HOLD")
        self.assertEqual(decode_decision(f"```JSON\n{raw}\n```").decision, "HOLD")
        self.assertEqual(
            decode_decision(f"Here is note {{not-json}} and analysis:\n```json\n{raw}\n```\nDone.").decision,
            "HOLD",
        )
        self.assertEqual(
            decode_decision(f"Leading {{x}} prose {raw} trailing.").decision,
            "HOLD",
        )

    def test_build_advice_prompt_stances(self):
        p_global = build_advice_prompt("Should I tweak cache TTL?", "Latency spiked", "global", is_followup=False)
        self.assertIn("Zoom out before zooming in", p_global)
        self.assertIn("challenge the premise", p_global)

        p_red = build_advice_prompt("Review sharding plan", None, "red_team", is_followup=False)
        self.assertIn("Red-Team Audit", p_red)

        p_tac = build_advice_prompt("Pick hash function", None, "tactical", is_followup=False)
        self.assertIn("Focused Tactical", p_tac)

        p_follow = build_advice_prompt("New trace attached", "CPU 95%", "global", is_followup=True)
        self.assertIn("Continue this independent review", p_follow)
        self.assertNotIn("Zoom out before zooming in", p_follow)


class FakeCliStreamingTests(IsolatedAsyncioTestCase):
    async def test_advisor_stream_protocol_and_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_cli = Path(tmp) / "fake_agy.py"
            fake_cli.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, sys
                    print(json.dumps({"event": "init", "conversation_id": "conv-123"}), flush=True)
                    for line in sys.stdin:
                        msg = json.loads(line)
                        text = msg["message"]["content"]
                        reply = "ZOOM_OUT_OK" if "Zoom out" in text else "FOLLOWUP_OK"
                        print(json.dumps({
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "conversation_id": "conv-123",
                                "response": reply,
                                "duration_seconds": 0.1,
                                "usage": {"total_tokens": 42},
                                "model": "fake-neon",
                            }
                        }), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IXUSR)

            advisor = Advisor(str(fake_cli), tmp)
            try:
                p1 = build_advice_prompt("Q1", "C1", "global", is_followup=False)
                r1 = await advisor.ask(p1, None, 10, effort="low", model="fake-neon")
                self.assertEqual(r1["advice"], "ZOOM_OUT_OK")
                self.assertEqual(r1["model_reported"], "fake-neon")

                p2 = build_advice_prompt("Q2", "C2", "global", is_followup=True)
                r2 = await advisor.ask(p2, r1["session_id"], 10)
                self.assertEqual(r2["advice"], "FOLLOWUP_OK")
                self.assertEqual(r2["session_id"], r1["session_id"])
            finally:
                await advisor.stop()


class TelemetryAndAuditTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "projects.sqlite3"
        self.log_path = Path(self.tmp.name) / "invocations.jsonl"
        self.fb_path = Path(self.tmp.name) / "feedback.jsonl"
        self.store = ProjectStore(self.db_path)
        self.inv = {"id": "inv-t", "tool": "agy_advise", "transport": "mcp"}
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("AGY_ADVISE_LOG", "AGY_ADVISE_LOG_FULL", "AGY_ADVISE_FEEDBACK_LOG")
        }
        for key in self._saved_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    async def test_execute_advise_logs_qa_project_errors_and_feedback_report(self):
        pivot_reply = json.dumps(
            {
                "decision": "PIVOT",
                "why": "Single mechanism spun out.",
                "global_assessment": "Need new route.",
                "main_bottleneck": "Root design flaw",
                "stop_branches": ["old-branch"],
                "missing_facts": [],
                "route": "new-route",
                "next_trial": "Test new architecture",
                "expected_signal": "Latency drops",
                "stop_condition": "No drop after 1 run",
                "max_trials": 2,
            }
        )
        advisor = FakeAdvisor(
            [
                "Global advice: challenge the premise first.",
                RuntimeError("Antigravity timed out after 30 seconds."),
                pivot_reply,
            ]
        )

        # 1. Q&A success (default preview mode: no full context persisted)
        qa_res = await execute_advise(
            store=self.store,
            advisor=advisor,
            question="Should we tune cache size?",
            context="SECRET_CONTEXT_SHOULD_NOT_BE_IN_DEFAULT_LOG",
            stance="red_team",
            model="fake-model",
        )
        qa_inv_id = qa_res["invocation"]["id"]

        # 2. Q&A timeout
        with self.assertRaises(RuntimeError):
            await execute_advise(
                store=self.store,
                advisor=advisor,
                question="Heavy question that times out",
            )

        # 3. Validation error
        with self.assertRaises(ValueError):
            await execute_advise(store=self.store, advisor=advisor, question="   ")

        # 4. Project init + report triggering review -> PIVOT
        init_evt = Initialize(
            id="svc",
            phase="p1",
            action="init",
            domain="systems",
            objective="Fix bottleneck",
            frozen_gate="Zero errors",
            route="old-route",
            stagnation_limit=1,
        )
        await execute_advise(store=self.store, advisor=advisor, project=init_evt)
        rep_evt = Report(
            id="svc",
            phase="p1",
            action="report",
            trial_id="t1",
            route="old-route",
            cycle=0,
            mechanism="m1",
            outcome="no_progress",
            evidence="Still failing under load.",
        )
        proj_res = await execute_advise(store=self.store, advisor=advisor, project=rep_evt)
        self.assertEqual(proj_res["review_status"], "decided")

        rows = read_jsonl(self.log_path)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["mode"], "qa")
        self.assertEqual(rows[0]["stance"], "red_team")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertNotIn("context", rows[0])
        self.assertNotIn("SECRET_CONTEXT", json.dumps(rows[0]))
        self.assertEqual(rows[1]["status"], "timeout")
        self.assertEqual(rows[2]["status"], "validation_error")
        self.assertFalse(rows[2]["llm_called"])
        self.assertEqual(rows[3]["action"], "init")
        self.assertFalse(rows[3]["llm_called"])
        self.assertEqual(rows[4]["decision"], "PIVOT")
        self.assertTrue(rows[4]["llm_called"])

        # Record feedback and verify summary + tuning hints + CLI
        record_feedback(
            invocation_id=qa_inv_id,
            label="rubber_stamp",
            note="Agreed too easily with proposed patch",
            db_path=self.db_path,
        )
        summary = summarize_logs(self.log_path, self.fb_path)
        self.assertEqual(summary["total_invocations"], 5)
        self.assertEqual(summary["llm_invocations"], 3)
        self.assertEqual(summary["timeout_or_transport_errors"], 1)
        self.assertEqual(summary["decision_counts"], {"PIVOT": 1})
        self.assertEqual(summary["feedback"]["label_counts"], {"rubber_stamp": 1})
        self.assertEqual(summary["feedback"]["by_context"], {"red_team": {"rubber_stamp": 1}})
        self.assertTrue(any("rubber_stamp" in h for h in summary["tuning_hints"]))
        report_text = format_summary(summary)
        self.assertIn("agy_advise Invocation & Feedback Report", report_text)

        # CLI smoke check (feedback + summary --json)
        rc_fb = audit_report.main(
            ["feedback", proj_res["invocation"]["id"], "helpful", "--note", "Good pivot", "--db", str(self.db_path)]
        )
        self.assertEqual(rc_fb, 0)
        rc_sum = audit_report.main(["--db", str(self.db_path), "--json"])
        self.assertEqual(rc_sum, 0)

    async def test_log_env_controls_off_and_full(self):
        advisor = FakeAdvisor(["Reply 1", "Reply 2"])

        os.environ["AGY_ADVISE_LOG"] = "off"
        await execute_advise(store=self.store, advisor=advisor, question="Hidden call")
        self.assertFalse(self.log_path.exists())

        os.environ.pop("AGY_ADVISE_LOG")
        os.environ["AGY_ADVISE_LOG_FULL"] = "1"
        await execute_advise(
            store=self.store,
            advisor=advisor,
            question="Full logged question",
            context="FULL_CONTEXT_PAYLOAD",
        )
        rows = read_jsonl(self.log_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context"], "FULL_CONTEXT_PAYLOAD")
        self.assertEqual(rows[0]["advice"], "Reply 2")

    async def test_actionable_hold_prescription_and_unblock_audit_loop(self):
        init_evt = Initialize(
            id="xduck",
            phase="phase2",
            action="init",
            domain="robotics",
            objective="Pass >= 7/8 dynamic gate",
            frozen_gate="Dynamic >= 7/8, static 14/14, ONNX pass",
            non_negotiables=["ONNX FP32 PASS", "Supine >= 10/12"],
            route="full-param",
            score_name="dynamic_pass",
            baseline_score=2,
            pass_score=7,
        )
        bare_hold = json.dumps({"decision": "HOLD", "why": "Entry mismatch."})
        good_hold = json.dumps(
            {
                "decision": "HOLD",
                "why": "Command block and 10x43 reference mismatch in training entrypoint.",
                "global_assessment": "Caught before wasting 100M steps.",
                "main_bottleneck": "Broken training input alignment",
                "stop_branches": ["launch-100m-before-alignment"],
                "unblock_actions": [
                    "Fix command-block encoder wiring in full-param PPO entry",
                    "Re-run 8,640-step CPU replay verifying max abs diff < 1e-5",
                ],
                "unblock_criteria": "CPU replay max abs diff < 1e-5 on both command block and 10x43 reference.",
                "verified_facts": ["Old PPO entry had command diff 0.992 and reference diff 3.609."],
                "route_postmortem": "Entrypoint alignment blocked cycle 1 before training started.",
            }
        )
        lift_continue = json.dumps(
            {
                "decision": "CONTINUE",
                "why": "All unblock criteria verified with max diff 1.2e-7.",
                "global_assessment": "Alignment fixed; safe to run controlled cycle.",
                "main_bottleneck": "Closed-loop dynamic generalization",
                "verified_facts": ["Full-param entry aligned to <1e-5 against prospective trace."],
                "route": "full-param",
                "next_trial": "Run 100k preflight + Stage A 25M",
                "expected_signal": "Dynamic >= 3/8 at 25M",
                "stop_condition": "Dynamic <= 2/8 at 65M or supine < 10/12",
                "max_trials": 2,
            }
        )
        advisor = FakeAdvisor([bare_hold, good_hold, lift_continue])
        await handle_project(self.store, init_evt, advisor, self.inv, "", None, 60, None, None)

        # Bare HOLD without unblock_actions / unblock_criteria is rejected as invalid_decision
        bad = await handle_project(
            self.store,
            Report(
                id="xduck", phase="phase2", action="report", trial_id="preflight-fail",
                route="full-param", cycle=0, mechanism="entry-check", outcome="blocked",
                evidence="Command diff 0.992, ref diff 3.609.",
            ),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(bad["review_status"], "invalid_decision")
        self.assertIn("unblock_actions", bad["error"])

        # Actionable HOLD binds cleanly and exposes pending_unblock
        held = await handle_project(
            self.store, Review(id="xduck", phase="phase2", action="review"),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(held["review_status"], "decided")
        self.assertEqual(held["project"]["phase_status"], "held")
        self.assertIsNotNone(held["project"]["pending_unblock"])
        self.assertEqual(len(held["project"]["pending_unblock"]["unblock_actions"]), 2)
        self.assertEqual(held["project"]["memory_counts"]["verified_facts"], 1)

        # Next review must automatically pin pending_unblock into L1 and instruct checklist audit
        lifted = await handle_project(
            self.store, Review(id="xduck", phase="phase2", action="review"),
            advisor, self.inv, "Re-check after fix", "CPU replay diff now 1.2e-7", 60, None, None,
        )
        self.assertEqual(lifted["review_status"], "decided")
        third_prompt = advisor.calls[2]["prompt"]
        self.assertIn("pending_unblock", third_prompt)
        self.assertIn("audit EVERY item", third_prompt)

        # Activating CONTINUE clears pending_unblock and re-opens dispatch_allowed
        act = await handle_project(
            self.store,
            Activate(id="xduck", phase="phase2", action="activate", decision_id=lifted["project"]["last_decision"]["id"]),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertTrue(act["project"]["dispatch_allowed"])
        self.assertIsNone(act["project"]["pending_unblock"])

    async def test_three_tier_external_memory_bounding_and_curation(self):
        init_evt = Initialize(
            id="longrun",
            phase="p1",
            action="init",
            objective="Reach >= 90%",
            frozen_gate="E2E >= 90%",
            initial_facts=["Baseline hardware is dual 5090."],
            route="route-a",
            score_name="acc",
            baseline_score=50.0,
            pass_score=90.0,
            stagnation_limit=20,
            same_mechanism_limit=30,
        )
        advisor = FakeAdvisor(
            [
                json.dumps(
                    {
                        "decision": "PIVOT",
                        "why": "Route A saturated at 60%.",
                        "route_postmortem": "Route A saturated at 60% after 25 trials due to frozen encoder.",
                        "verified_facts": [
                            "Frozen encoder caps accuracy at 60%.",
                            "Baseline hardware is dual 5090.",  # duplicate should be deduped
                        ],
                        "route": "route-b",
                        "next_trial": "Unfreeze full encoder",
                        "expected_signal": "Accuracy > 75%",
                        "stop_condition": "Accuracy <= 62%",
                        "max_trials": 5,
                    }
                )
            ]
        )
        await handle_project(self.store, init_evt, advisor, self.inv, "", None, 60, None, None)

        # Manual L2 memory curation (add > MAX_VERIFIED_FACTS to verify strict O(1) cap + remove_facts)
        await handle_project(
            self.store,
            UpdateMemory(
                id="longrun",
                phase="p1",
                action="memory",
                add_facts=[f"Fact #{i}" for i in range(1, 9)],
                route_postmortems={"legacy-patch": "Abandoned before init: offline gain failed E2E."},
            ),
            advisor, self.inv, "", None, 60, None, None,
        )
        await handle_project(
            self.store,
            UpdateMemory(
                id="longrun",
                phase="p1",
                action="memory",
                add_facts=[f"Fact #{i}" for i in range(9, 16)],
                remove_facts=["Fact #1"],
            ),
            advisor, self.inv, "", None, 60, None, None,
        )

        # Record 25 trials so L3 window caps are actively exercised
        for idx in range(1, 26):
            score = 50.0 + (10.0 if idx == 1 else 0.0)
            await handle_project(
                self.store,
                Report(
                    id="longrun", phase="p1", action="report", trial_id=f"t{idx}",
                    route="route-a", cycle=0, mechanism=f"m{idx % 5}",
                    score=score, evidence=f"Trial {idx} evidence", auto_review=False,
                ),
                advisor, self.inv, "", None, 60, None, None,
            )

        # Trigger review -> PIVOT promotes route-a postmortem and dedupes verified_facts
        rev = await handle_project(
            self.store, Review(id="longrun", phase="p1", action="review"),
            advisor, self.inv, "", None, 60, None, None,
        )
        self.assertEqual(rev["review_status"], "decided")

        st = await handle_project(
            self.store, Status(id="longrun", phase="p1", action="status", include_history=True),
            advisor, self.inv, "", None, 60, None, None,
        )
        mem = st["history"]["memory"]
        self.assertLessEqual(len(mem["verified_facts"]), MAX_VERIFIED_FACTS)
        self.assertIn("legacy-patch", mem["route_postmortems"])
        self.assertIn("route-a", mem["route_postmortems"])
        self.assertEqual(mem["route_postmortems"]["route-a"]["status"], "abandoned")
        self.assertEqual(mem["route_postmortems"]["route-a"]["trials"], 25)

        # Verify bounded three-tier packet sizes
        with self.store.transaction() as db:
            raw_state = self.store.load(db, init_evt)
        packet = build_three_tier_packet(raw_state)
        self.assertEqual(len(packet["L3_recent_working_memory"]["recent_trials_summary"]), L3_SUMMARY_TRIALS)
        self.assertEqual(len(packet["L3_recent_working_memory"]["recent_evidence"]), L3_DETAILED_TRIALS)
        self.assertLessEqual(len(packet["L3_recent_working_memory"]["recent_decisions"]), L3_RECENT_DECISIONS)
        self.assertEqual(packet["L2_distilled_memory"]["route_stats"]["route-a"]["trials"], 25)
        self.assertIn("## L1 Constitution", st["history"]["markdown_summary"])
        self.assertIn("## L2 Distilled Memory", st["history"]["markdown_summary"])
        self.assertIn("## L3 Recent Working Memory", st["history"]["markdown_summary"])

    async def test_deferred_report_plus_review_does_not_double_count_triggers(self):
        advisor = FakeAdvisor(
            [
                json.dumps(
                    {
                        "decision": "HOLD",
                        "why": "Blocked on preflight.",
                        "unblock_actions": ["Fix alignment"],
                        "unblock_criteria": "Replay diff < 1e-5",
                    }
                )
            ]
        )
        await execute_advise(
            store=self.store,
            advisor=advisor,
            project=Initialize(
                id="cnt", phase="p1", action="init", objective="G", frozen_gate="F", route="r1"
            ),
        )
        await execute_advise(
            store=self.store,
            advisor=advisor,
            project=Report(
                id="cnt", phase="p1", action="report", trial_id="t1", route="r1", cycle=0,
                mechanism="m1", outcome="blocked", regressions=["align"], evidence="bad", auto_review=False,
            ),
        )
        await execute_advise(
            store=self.store,
            advisor=advisor,
            project=Review(id="cnt", phase="p1", action="review"),
        )
        summary = summarize_logs(self.log_path, self.fb_path)
        self.assertEqual(
            summary["trigger_counts"],
            {"protected_regression": 1, "blocker": 1, "explicit_review": 1},
        )

    async def test_advance_ignores_extra_fields_and_prompts_l1_qualified_trial_id(self):
        advisor = FakeAdvisor(
            [
                json.dumps(
                    {
                        "decision": "ADVANCE",
                        "why": "All frozen gates verified locally.",
                        "qualified_trial_id": "echoed-by-model",
                        "extra_comment": "should-be-ignored",
                    }
                )
            ]
        )
        await handle_project(
            self.store,
            Initialize(
                id="adv",
                phase="p1",
                action="init",
                objective="Ship clean gate",
                frozen_gate="All checks pass",
                route="r1",
            ),
            advisor,
            self.inv,
            "",
            None,
            60,
            None,
            None,
        )
        res = await handle_project(
            self.store,
            Report(
                id="adv",
                phase="p1",
                action="report",
                trial_id="t-pass",
                route="r1",
                cycle=0,
                mechanism="final-check",
                outcome="qualified",
                evidence="All frozen gates passed.",
            ),
            advisor,
            self.inv,
            "",
            None,
            60,
            None,
            None,
        )
        self.assertEqual(res["review_status"], "decided")
        self.assertEqual(res["project"]["phase_status"], "advanced")
        last = res["project"]["last_decision"]
        self.assertEqual(last["qualified_trial_id"], "t-pass")
        self.assertNotIn("extra_comment", last)
        prompt = advisor.calls[0]["prompt"]
        self.assertIn("L1_constitution.qualified_trial_id", prompt)
        self.assertIn("do not include qualified_trial_id in JSON", prompt)


if __name__ == "__main__":
    unittest.main()
