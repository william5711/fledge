import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from grading import checks_for_case, get_path, grade, row_matches, summarize, validate_cases  # noqa: E402
from run_evals import AgentError, parse_stream, run_tracker, skill_was_used  # noqa: E402

RUNNER = ROOT / "evals" / "run_evals.py"

# Stands in for the Claude CLI so these tests are free and need no login.
FAKE_AGENT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    args = sys.argv[1:]
    message = sys.stdin.read()
    if "--output-format" in args and args[args.index("--output-format") + 1] == "json":
        passed = "BAD REPLY" not in message
        print(json.dumps({"result": json.dumps({"pass": passed, "reason": "fake"}), "total_cost_usd": 0.001}))
        raise SystemExit
    if "crash" in message:
        print("no events here")
        raise SystemExit(1)
    tools = []
    if os.path.exists(".claude/skills/fledge/SKILL.md") and "groceries" in message:
        command = [sys.executable, ".claude/skills/fledge/scripts/tracker.py", "purchase", "add",
                   "--item", "Groceries", "--amount", "23.40", "--category", "groceries"]
        subprocess.run(command, check=True, capture_output=True)
        tools.append({"type": "tool_use", "name": "Bash", "input": {"command": " ".join(command)}})
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "fake-model"}))
    print(json.dumps({"type": "assistant", "message": {"content": tools + [{"type": "text", "text": "hi"}]}}))
    print(json.dumps({"type": "result", "result": "Saved: $23.40", "total_cost_usd": 0.01, "session_id": "s1"}))
    """
)


def run_data(store=None, seeded=None, reply="", triggered=True, judge=None, tracker=None):
    return {
        "store": store or {},
        "seeded_store": seeded or {},
        "reply": reply,
        "triggered": triggered,
        "judge": judge,
        "tracker": tracker,
    }


class GradingTest(unittest.TestCase):
    def test_get_path_handles_nested_and_missing_values(self):
        value = {"a": {"b": [{"c": 1}]}}
        self.assertEqual(get_path(value, "a.b.0.c"), 1)
        self.assertIsNone(get_path(value, "a.x"))
        self.assertIsNone(get_path(value, "a.b.5"))

    def test_row_matching(self):
        row = {"item": "Winter Jacket", "amount_cents": 500, "shares": {"me": 10}}
        self.assertTrue(row_matches(row, {"item~": "jacket", "amount_cents": 500, "shares.me": 10}))
        self.assertFalse(row_matches(row, {"item~": "shoes"}))
        self.assertFalse(row_matches(row, {"amount_cents": 501}))
        self.assertFalse(row_matches(row, {"missing~": "x"}))

    def grade_one(self, check, **kwargs):
        case = {"checks": [check]}
        return grade(case, "with_skill", run_data(**kwargs))[0]["passed"]

    def test_count_check(self):
        store = {"purchases": [{"status": "bought"}, {"status": "planned"}]}
        check = {"type": "count", "collection": "purchases", "where": {"status": "bought"}, "equals": 1}
        self.assertTrue(self.grade_one(check, store=store))
        self.assertFalse(self.grade_one(dict(check, equals=2), store=store))
        self.assertTrue(self.grade_one({"type": "count", "collection": "shared.expenses", "equals": 0}, store=store))

    def test_reply_checks_ignore_case_and_support_negation(self):
        check = {"type": "reply_regex", "pattern": "leave"}
        self.assertTrue(self.grade_one(check, reply="LEAVE now"))
        self.assertFalse(self.grade_one(dict(check, negate=True), reply="Leave now"))

    def test_unchanged_and_store_checks(self):
        self.assertTrue(self.grade_one({"type": "unchanged"}, store={"a": 1}, seeded={"a": 1}))
        self.assertFalse(self.grade_one({"type": "unchanged"}, store={"a": 2}, seeded={"a": 1}))
        self.assertFalse(self.grade_one({"type": "store_regex", "pattern": "4111"}, store={"note": "4111"}))

    def test_tracker_and_judge_checks_use_supplied_callables(self):
        tracker_check = {"type": "tracker", "args": ["x"], "path": "a", "equals": 1}
        self.assertTrue(self.grade_one(tracker_check, tracker=lambda args: {"a": 1}))
        judge_check = {"type": "judge", "criterion": "good"}
        self.assertFalse(self.grade_one(judge_check, judge=lambda criterion, reply: (False, "no")))

    def test_without_skill_runs_only_grade_replies(self):
        case = {
            "expect_trigger": True,
            "checks": [
                {"type": "count", "collection": "purchases", "equals": 1},
                {"type": "reply_regex", "pattern": "x"},
                {"type": "judge", "criterion": "y"},
            ],
        }
        self.assertEqual(checks_for_case(case, "without_skill"), [])
        case["compare_without_skill"] = True
        self.assertEqual([c["type"] for c in checks_for_case(case, "without_skill")], ["reply_regex", "judge"])
        self.assertEqual(checks_for_case(case, "with_skill")[-1], {"type": "trigger", "expected": True})

    def test_summary_keeps_errors_apart_and_finds_unstable_cases(self):
        def record(case, passed, error=None, triggered=True, expected=True):
            return {
                "case": case, "category": "c", "mode": "with_skill", "passed": passed, "error": error,
                "checks": [{"passed": passed}] if not error else [], "triggered": triggered,
                "expect_trigger": expected, "cost_usd": 0.5,
            }
        runs = [
            record("a", True), record("a", False), record("b", True), record("c", False, error="timeout"),
            record("d", True, triggered=False, expected=False), record("e", False, expected=False),
        ]
        summary = summarize(runs)["with_skill"]
        self.assertEqual(summary["runs"], 6)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["runs_passed"], 3)
        self.assertEqual(summary["pass_rate"], 0.6)
        self.assertEqual(summary["unstable_cases"], ["a"])
        self.assertEqual(summary["cases_passing_every_run"], 2)
        self.assertEqual(summary["trigger_rate_when_expected"], 1.0)
        self.assertEqual(summary["trigger_rate_when_not_expected"], 0.5)
        self.assertEqual(summary["cost_usd"], 3.0)


class StreamTest(unittest.TestCase):
    def test_parse_stream_collects_reply_tools_and_cost(self):
        lines = [
            "not json",
            json.dumps({"type": "system", "subtype": "init", "session_id": "abc", "model": "m"}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "fledge"}}
            ]}}),
            json.dumps({"type": "result", "result": "done", "total_cost_usd": 0.25, "session_id": "abc"}),
        ]
        info = parse_stream("\n".join(lines))
        self.assertEqual(info["reply"], "done")
        self.assertEqual(info["cost_usd"], 0.25)
        self.assertEqual(info["session_id"], "abc")
        self.assertTrue(skill_was_used(info["tools"]))

    def test_output_without_a_result_is_an_error(self):
        with self.assertRaises(AgentError):
            parse_stream("just text")

    def test_skill_detection(self):
        self.assertTrue(skill_was_used([{"name": "Bash", "input": {"command": "python3 x/tracker.py briefing"}}]))
        self.assertTrue(skill_was_used([{"name": "Read", "input": {"file_path": ".claude/skills/fledge/SKILL.md"}}]))
        self.assertFalse(skill_was_used([{"name": "Skill", "input": {"skill": "pdf"}}]))
        self.assertFalse(skill_was_used([{"name": "Bash", "input": {"command": "ls"}}]))


class CaseFileTest(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "evals" / "cases.json").read_text())

    def test_cases_are_valid(self):
        validate_cases(self.data)

    def test_validation_rejects_mistakes(self):
        broken = {"seeds": {}, "cases": [{"id": "x", "split": "dev", "turns": ["hi"], "checks": [{"type": "nope"}]}]}
        with self.assertRaises(ValueError):
            validate_cases(broken)

    def test_every_seed_and_setup_command_works(self):
        for case in self.data["cases"]:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as folder:
                env = dict(
                    os.environ,
                    FLEDGE_DATA=str(Path(folder) / "data.json"),
                    FLEDGE_TODAY="2026-11-10",
                )
                for command in self.data["seeds"].get(case.get("seed"), []) + case.get("setup", []):
                    self.assertNotIn("error", run_tracker(command, env))

    def test_tracker_checks_use_valid_commands(self):
        for case in self.data["cases"]:
            for check in case["checks"]:
                if check["type"] == "tracker":
                    with tempfile.TemporaryDirectory() as folder:
                        env = dict(os.environ, FLEDGE_DATA=str(Path(folder) / "data.json"))
                        self.assertNotIn("error", run_tracker(check["args"], env))

    def test_both_splits_have_cases(self):
        splits = [case["split"] for case in self.data["cases"]]
        self.assertGreater(splits.count("dev"), 0)
        self.assertGreater(splits.count("test"), 0)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.agent = Path(self.folder.name) / "fake_claude.py"
        self.agent.write_text(FAKE_AGENT.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        self.agent.chmod(self.agent.stat().st_mode | stat.S_IEXEC)
        self.out = Path(self.folder.name) / "results"

    def run_runner(self, *args, expected_code=0):
        env = {k: v for k, v in os.environ.items() if k != "FLEDGE_DATA"}
        done = subprocess.run(
            [sys.executable, str(RUNNER), "--agent-bin", str(self.agent), "--out", str(self.out), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(done.returncode, expected_code, done.stdout + done.stderr)
        return done.stdout

    def test_dry_run_calls_no_agent(self):
        output = self.run_runner("--dry-run", "--cases", "safety-gas-smell", "--reps", "3", "--mode", "both")
        self.assertEqual(json.loads(output)["runs"], 6)
        output = self.run_runner("--dry-run", "--cases", "log-casual-groceries", "--reps", "3", "--mode", "both")
        self.assertEqual(json.loads(output)["runs"], 3)
        self.assertFalse(self.out.exists())

    def test_full_run_grades_saved_data_and_writes_reports(self):
        self.run_runner("--cases", "log-casual-groceries,no-trigger-capital")
        results = json.loads((self.out / "results.json").read_text())
        by_case = {r["case"]: r for r in results["runs"]}
        groceries = by_case["log-casual-groceries"]
        self.assertTrue(groceries["triggered"])
        details = {c["type"]: c["passed"] for c in groceries["checks"]}
        self.assertTrue(details["count"])
        self.assertTrue(details["trigger"])
        # The fake reply omits the balance, so the reply check must fail the run.
        self.assertFalse(details["reply_regex"])
        self.assertFalse(groceries["passed"])
        self.assertTrue(by_case["no-trigger-capital"]["passed"])
        self.assertEqual(results["summary"]["with_skill"]["runs_passed"], 1)
        self.assertEqual(results["metadata"]["models"], "fake-model")
        self.assertIn("log-casual-groceries", (self.out / "summary.md").read_text())

    def test_without_skill_only_runs_cases_that_are_fair_to_compare(self):
        self.run_runner("--cases", "log-casual-groceries,safety-gas-smell", "--mode", "without_skill")
        results = json.loads((self.out / "results.json").read_text())
        self.assertEqual([r["case"] for r in results["runs"]], ["safety-gas-smell"])
        self.assertFalse(results["runs"][0]["triggered"])
        self.assertEqual([c["type"] for c in results["runs"][0]["checks"]], ["judge"])

    def test_session_is_only_kept_for_multi_turn_cases(self):
        log = Path(self.folder.name) / "calls.log"
        text = self.agent.read_text().replace(
            "args = sys.argv[1:]\n",
            f"args = sys.argv[1:]\nopen({str(log)!r}, 'a').write(' '.join(args) + chr(10))\n",
        )
        self.agent.write_text(text)
        self.run_runner("--cases", "log-two-turn-plan-buy")
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--no-session-persistence", calls[0])
        self.assertNotIn("--resume", calls[0])
        self.assertIn("--resume s1", calls[1])
        log.unlink()
        self.run_runner("--cases", "no-trigger-capital")
        self.assertIn("--no-session-persistence", log.read_text())

    def test_agent_failures_are_errors_not_failed_answers(self):
        fake = self.agent.read_text().replace('if "crash" in message:', "if True:")
        self.agent.write_text(fake)
        self.run_runner("--cases", "no-trigger-capital", expected_code=1)
        run = json.loads((self.out / "results.json").read_text())["runs"][0]
        self.assertIsNotNone(run["error"])
        self.assertFalse(run["passed"])


if __name__ == "__main__":
    unittest.main()
