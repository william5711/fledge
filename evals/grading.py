"""Checks that grade one finished conversation.

Each check returns a dict with "type", "passed", and "detail". Saved data and
reply patterns are graded by code. Only "judge" checks use a model.
"""
import json
import re

REPLY_CHECKS = {"reply_regex", "judge"}


def get_path(value, path):
    for part in path.split("."):
        if isinstance(value, dict):
            if part not in value:
                return None
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def row_matches(row, where):
    for key, expected in where.items():
        if key.endswith("~"):
            actual = get_path(row, key[:-1])
            if actual is None or str(expected).casefold() not in str(actual).casefold():
                return False
        elif get_path(row, key) != expected:
            return False
    return True


def result(check, passed, detail):
    return {"type": check["type"], "passed": bool(passed), "detail": detail}


def check_count(check, run):
    rows = get_path(run["store"], check["collection"]) or []
    found = sum(1 for row in rows if row_matches(row, check.get("where", {})))
    return result(check, found == check["equals"], f"{check['collection']}: found {found}, wanted {check['equals']}")


def check_value(check, run):
    actual = get_path(run["store"], check["path"])
    return result(check, actual == check["equals"], f"{check['path']}: got {actual!r}, wanted {check['equals']!r}")


def check_tracker(check, run):
    output = run["tracker"](check["args"])
    actual = get_path(output, check["path"])
    return result(check, actual == check["equals"], f"{check['path']}: got {actual!r}, wanted {check['equals']!r}")


def check_unchanged(check, run):
    return result(check, run["store"] == run["seeded_store"], "saved data compared with the starting data")


def check_store_regex(check, run):
    found = re.search(check["pattern"], json.dumps(run["store"]))
    return result(check, not found, f"pattern {check['pattern']!r} must not be in saved data")


def check_reply_regex(check, run):
    found = re.search(check["pattern"], run["reply"], re.IGNORECASE)
    negate = check.get("negate", False)
    passed = not found if negate else found
    wanted = "absent" if negate else "present"
    return result(check, passed, f"pattern {check['pattern']!r} should be {wanted}")


def check_trigger(check, run):
    return result(check, run["triggered"] == check["expected"], f"skill used: {run['triggered']}")


def check_judge(check, run):
    passed, reason = run["judge"](check["criterion"], run["reply"])
    return result(check, passed, f"{check['criterion']} | judge: {reason}")


GRADERS = {
    "count": check_count,
    "value": check_value,
    "tracker": check_tracker,
    "unchanged": check_unchanged,
    "store_regex": check_store_regex,
    "reply_regex": check_reply_regex,
    "trigger": check_trigger,
    "judge": check_judge,
}


def checks_for_case(case, mode):
    """Without the skill, an agent can't see the student's records or save data.

    Grading it on those would make the skill look better than it is, so only
    cases marked compare_without_skill run, and only their replies are graded.
    """
    checks = list(case["checks"])
    if mode == "without_skill":
        if not case.get("compare_without_skill"):
            return []
        return [c for c in checks if c["type"] in REPLY_CHECKS]
    if "expect_trigger" in case:
        checks.append({"type": "trigger", "expected": case["expect_trigger"]})
    return checks


def grade(case, mode, run):
    return [GRADERS[check["type"]](check, run) for check in checks_for_case(case, mode)]


def validate_cases(data):
    """Catch mistakes in cases.json before any paid run starts."""
    seen = set()
    for case in data["cases"]:
        name = case.get("id", "<missing id>")
        if name in seen:
            raise ValueError(f"Duplicate case id: {name}")
        seen.add(name)
        if case.get("split") not in ("dev", "test"):
            raise ValueError(f"{name}: split must be dev or test")
        if not case.get("turns"):
            raise ValueError(f"{name}: needs at least one turn")
        if not case.get("checks") and "expect_trigger" not in case:
            raise ValueError(f"{name}: needs at least one check")
        if case.get("seed") and case["seed"] not in data["seeds"]:
            raise ValueError(f"{name}: unknown seed {case['seed']}")
        for check in case.get("checks", []):
            if check.get("type") not in GRADERS:
                raise ValueError(f"{name}: unknown check type {check.get('type')}")


def summarize(runs):
    """Runs with infrastructure errors are counted apart, not as agent failures."""
    summary = {}
    for mode in sorted({r["mode"] for r in runs}):
        mode_runs = [r for r in runs if r["mode"] == mode]
        graded = [r for r in mode_runs if not r["error"]]
        by_case = {}
        for run in graded:
            by_case.setdefault(run["case"], []).append(run["passed"])
        triggers = [r for r in graded if r.get("expect_trigger") is not None]
        should = [r for r in triggers if r["expect_trigger"]]
        should_not = [r for r in triggers if not r["expect_trigger"]]
        categories = {}
        for run in graded:
            bucket = categories.setdefault(run["category"], {"passed": 0, "runs": 0})
            bucket["runs"] += 1
            bucket["passed"] += 1 if run["passed"] else 0
        checks = [c for r in graded for c in r["checks"]]
        summary[mode] = {
            "runs": len(mode_runs),
            "errors": len(mode_runs) - len(graded),
            "runs_passed": sum(1 for r in graded if r["passed"]),
            "pass_rate": round(sum(1 for r in graded if r["passed"]) / len(graded), 3) if graded else None,
            "cases": len(by_case),
            "cases_passing_every_run": sum(1 for values in by_case.values() if all(values)),
            "unstable_cases": sorted(c for c, v in by_case.items() if any(v) and not all(v)),
            "checks_passed": sum(1 for c in checks if c["passed"]),
            "checks_total": len(checks),
            "trigger_rate_when_expected": rate(should, True),
            "trigger_rate_when_not_expected": rate(should_not, True),
            "categories": categories,
            "cost_usd": round(sum(r["cost_usd"] for r in mode_runs), 4),
        }
    return summary


def rate(runs, value):
    if not runs:
        return None
    return round(sum(1 for r in runs if r["triggered"] == value) / len(runs), 3)
