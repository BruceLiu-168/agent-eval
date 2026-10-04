"""Composable evidence grading for state, answers, trajectories, and rubrics."""

import copy
import importlib
import re

from .contracts import validate_case, validate_episode
from .core import grade_episode
from .judge import evaluate_rubric

GRADER_VERSION = "framework-grader-v1"
_MISSING = object()
_STATE_MISSING = "missing independent business-state oracle or observed state"


def _equal(expected, actual):
    # Python's True == 1 must not accidentally satisfy a business oracle.
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(_equal(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(_equal(a, b) for a, b in zip(expected, actual))
    return expected == actual


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(value, actual[key]) for key, value in expected.items())
    return _equal(expected, actual)


def _lookup(episode, path):
    value = episode
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
        if value is _MISSING:
            return value
    return value


def _check(identifier, passed, reason, **details):
    return {"id": identifier, "status": "unknown" if passed is None else "pass" if passed else "fail",
            "evidence": [reason], **details}


def _declarative(item, episode):
    path, kind, expected = item["path"], item["kind"], item["expected"]
    actual = _lookup(episode, path)
    if actual is _MISSING:
        return _check("check:" + item["id"], None, "missing observable evidence: " + path,
                      check_id=item["id"], path=path, kind=kind)
    if kind == "equals":
        passed = _equal(expected, actual)
    elif kind == "json_subset":
        passed = _subset(expected, actual)
    elif kind == "regex":
        passed = isinstance(actual, str) and re.search(expected, actual) is not None
    elif isinstance(actual, str):
        passed = isinstance(expected, str) and expected in actual
    elif isinstance(actual, list):
        passed = any(_equal(expected, value) for value in actual)
    elif isinstance(actual, dict):
        passed = isinstance(expected, str) and expected in actual
    else:
        passed = False
    return _check("check:" + item["id"], passed, f"{kind} {'matched' if passed else 'mismatched'} at {path}",
                  check_id=item["id"], path=path, kind=kind)


def _plugin(case, episode, config):
    """Trusted local grader code runs with a copy of the full private oracle."""
    reference = config["callable"]
    metadata = {"kind": "python", "callable": reference, "version": config.get("version", "unversioned")}
    try:
        if not isinstance(reference, str) or reference.count(":") != 1:
            raise ValueError("invalid callable reference")
        module, name = reference.split(":")
        function = getattr(importlib.import_module(module), name)
        result = function(copy.deepcopy(case), copy.deepcopy(episode))
        if isinstance(result, dict):
            result = result["checks"]
        if not isinstance(result, list) or not result:
            raise ValueError("plugin must return nonempty checks")
        checks, seen = [], set()
        for item in result:
            if not isinstance(item, dict):
                raise ValueError("invalid plugin check")
            identifier, status, evidence = item.get("id"), item.get("status"), item.get("evidence")
            if (not isinstance(identifier, str) or not identifier.strip() or identifier in seen
                    or status not in ("pass", "fail", "unknown")):
                raise ValueError("invalid plugin check identity or status")
            if isinstance(evidence, str) and evidence.strip():
                evidence = [evidence]
            if (not isinstance(evidence, list) or not evidence
                    or any(not isinstance(value, str) or not value.strip() for value in evidence)):
                raise ValueError("plugin evidence must be nonempty strings")
            seen.add(identifier)
            checks.append({"id": "plugin:" + identifier, "status": status, "evidence": evidence})
        return checks, metadata
    except Exception as exc:
        # Plugin failures are evaluator abstentions, never agent product failures.
        metadata["error_type"] = type(exc).__name__
        return [_check("plugin:unavailable", None, "custom grader failed: " + type(exc).__name__)], metadata


def evaluate(case, episode, grader_config=None):
    """AND all declared expectations while preserving unknown/infra evidence.

    ``grader_config`` defaults to deterministic. Explicit ``kind='llm'`` judges
    rubric criteria; ``callable='module:function'`` adds trusted custom checks.
    Resource budgets and mandatory trajectory rules always remain binding.
    """
    case = validate_case(case)
    episode = validate_episode(episode, case_id=case["case_id"])
    config = copy.deepcopy(grader_config) if grader_config is not None else {"kind": "deterministic"}
    if not isinstance(config, dict):
        raise ValueError("grader: must be an object")
    if config.get("kind", "deterministic") not in ("deterministic", "llm", "python", "callable"):
        raise ValueError("grader.kind: must be deterministic, llm, or python")
    if config.get("kind") in ("python", "callable") and "callable" not in config:
        raise ValueError("grader.callable: required for custom grader")
    result = grade_episode(case, episode)
    result["evidence"] = [entry for entry in result["evidence"]
                          if entry not in (_STATE_MISSING, "business state matches", "business state mismatch")]
    result["grader_version"] = GRADER_VERSION
    result["grader"] = {"kind": config.get("kind", "deterministic"), "version": GRADER_VERSION}
    checks = []
    completed = episode["status"] == "completed"
    unavailable = "execution did not complete; business outcome not adjudicated"
    if "expected_state" in case:
        observed = episode.get("final_state", _MISSING)
        passed = _subset(case["expected_state"], observed) if completed and observed is not _MISSING else None
        reason = unavailable if not completed else "business state unavailable" if observed is _MISSING else (
            "business state matches" if passed else "business state mismatch")
        checks.append(_check("expected_state", passed, reason))
    if "expected_output" in case:
        observed = episode.get("output", _MISSING)
        passed = _equal(case["expected_output"], observed) if completed and observed is not _MISSING else None
        reason = unavailable if not completed else "output unavailable" if observed is _MISSING else (
            "output matches" if passed else "output mismatch")
        checks.append(_check("expected_output", passed, reason))
    for item in case.get("checks", []):
        checks.append(_declarative(item, episode) if completed else
                      _check("check:" + item["id"], None, unavailable, check_id=item["id"],
                             path=item["path"], kind=item["kind"]))
    if config.get("kind") == "llm":
        judged = evaluate_rubric(case, episode, config)
        checks.extend(judged["checks"])
        result["grader"]["judge"] = judged["judge"]
        result["grader_version"] += "+" + judged["judge"]["version"]
    else:
        checks.extend(_check("rubric:" + item["id"], None, "rubric requires an explicitly configured LLM judge",
                             criterion_id=item["id"]) for item in case.get("rubric", []))
    if "callable" in config:
        if completed:
            custom_checks, metadata = _plugin(case, episode, config)
        else:
            custom_checks = [_check("plugin:unavailable", None, unavailable)]
            metadata = {"kind": "python", "callable": config["callable"], "version": config.get("version", "unversioned")}
        checks.extend(custom_checks)
        result["grader"]["plugin"] = metadata
    # These checks describe business expectations. A mandatory trajectory or
    # resource violation still causes a fail even if the outcome itself passed.
    statuses = [item["status"] for item in checks]
    result["outcome"] = False if "fail" in statuses else None if "unknown" in statuses or not statuses else True
    budget_unknown = False
    measurements = {"max_steps": len(episode["events"]), "max_cost": episode.get("cost"),
                    "max_latency_ms": episode.get("latency_ms")}
    for name, bound in case.get("limits", {}).items():
        observed = measurements[name]
        passed = None if observed is None else observed <= bound
        budget_unknown = budget_unknown or passed is None
        checks.append(_check("budget:" + name, passed,
                             "resource evidence missing: " + name if passed is None else
                             "resource budget satisfied: " + name if passed else "resource budget exceeded: " + name))
    if result["violations"] or result["outcome"] is False:
        result["status"] = "fail"
    elif not completed:
        result["status"] = "infra_error"
    elif result["outcome"] is None or budget_unknown:
        result["status"] = "unknown"
    else:
        result["status"] = "pass"
    result["checks"] = checks
    for item in checks:
        for evidence in item["evidence"]:
            if evidence not in result["evidence"]:
                result["evidence"].append(evidence)
    return result
