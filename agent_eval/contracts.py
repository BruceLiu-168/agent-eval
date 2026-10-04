"""Validated JSON contracts shared by local runs and imported production data.

A case is private evaluation data. Always use :func:`public_case` at the agent
boundary so grading targets and unreviewed metadata cannot leak to the agent.
"""

import copy
import json
import math
import re
from pathlib import Path

_LIMITS = {"max_steps", "max_cost", "max_latency_ms"}
_CHECK_KINDS = {"equals", "contains", "regex", "json_subset"}


def _fail(path, message):
    raise ValueError(f"{path}: {message}")


def _json(value, path, active=None):
    """Reject non-JSON values and non-finite numbers, with a useful location."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(path, "number must be finite")
        return
    if not isinstance(value, (dict, list)):
        _fail(path, "must be a JSON value")
    active = set() if active is None else active
    if id(value) in active:
        _fail(path, "cyclic value is not JSON")
    active.add(id(value))
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, "object keys must be strings")
            _json(item, f"{path}.{key}", active)
    else:
        for index, item in enumerate(value):
            _json(item, f"{path}[{index}]", active)
    active.remove(id(value))


def _object(value, path):
    if not isinstance(value, dict):
        _fail(path, "must be an object")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")


def _number(value, path, nullable=False):
    if nullable and value is None:
        return
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        _fail(path, "must be a finite non-negative number" + (" or null" if nullable else ""))


def _items(value, path):
    if not isinstance(value, list):
        _fail(path, "must be an array")


def validate_case(case):
    """Return an isolated, normalized case, or raise ``ValueError`` with a path.

    Arbitrary business JSON can contain negative values. Resource measurements
    and limits must be non-negative. Unknown case fields remain private and are
    preserved for forward-compatible dataset metadata.
    """
    _object(case, "case")
    _json(case, "case")
    for field in ("case_id", "business"):
        _text(case.get(field), f"case.{field}")
    if "input" not in case:
        _fail("case.input", "is required")
    result = copy.deepcopy(case)
    result.setdefault("family_id", case["case_id"])
    _text(result["family_id"], "case.family_id")
    for field in ("initial_state", "metadata"):
        if field in result:
            _object(result[field], "case." + field)
    if "expected_state" in result:
        _object(result["expected_state"], "case.expected_state")
        if not result["expected_state"]:
            _fail("case.expected_state", "must not be empty")
    for field in ("forbidden_actions", "required_actions"):
        if field in result:
            _items(result[field], "case." + field)
            for index, value in enumerate(result[field]):
                _text(value, f"case.{field}[{index}]")
    for field in ("required_order", "required_success_before"):
        if field in result:
            _items(result[field], "case." + field)
            for index, pair in enumerate(result[field]):
                if not isinstance(pair, list) or len(pair) != 2:
                    _fail(f"case.{field}[{index}]", "must be a pair of action names")
                for offset, value in enumerate(pair):
                    _text(value, f"case.{field}[{index}][{offset}]")
    if "limits" in result:
        _object(result["limits"], "case.limits")
        for key, value in result["limits"].items():
            if key not in _LIMITS:
                _fail("case.limits." + key, "unsupported resource limit")
            _number(value, "case.limits." + key)
            if key == "max_steps" and type(value) is not int:
                _fail("case.limits.max_steps", "must be an integer")
    for field in ("checks", "rubric"):
        if field not in result:
            continue
        _items(result[field], "case." + field)
        seen = set()
        for index, item in enumerate(result[field]):
            path = f"case.{field}[{index}]"
            _object(item, path)
            _text(item.get("id"), path + ".id")
            if item["id"] in seen:
                _fail(path + ".id", "duplicate ID")
            seen.add(item["id"])
            if field == "rubric":
                _text(item.get("description"), path + ".description")
                continue
            if not isinstance(item.get("kind"), str) or item["kind"] not in _CHECK_KINDS:
                _fail(path + ".kind", "must be equals, contains, regex, or json_subset")
            _text(item.get("path"), path + ".path")
            parts = item["path"].split(".")
            if parts[0] not in ("output", "final_state") or any(not part for part in parts):
                _fail(path + ".path", "must target output or final_state using dotted components")
            if "expected" not in item:
                _fail(path + ".expected", "is required (null is allowed)")
            if item["kind"] == "regex":
                if not isinstance(item["expected"], str):
                    _fail(path + ".expected", "regex must be a string")
                try:
                    re.compile(item["expected"])
                except re.error:
                    _fail(path + ".expected", "invalid regular expression")
            if item["kind"] == "json_subset" and not isinstance(item["expected"], dict):
                _fail(path + ".expected", "json_subset expects an object")
    if not ("expected_state" in result or "expected_output" in result
            or result.get("checks") or result.get("rubric")):
        _fail("case", "requires expected_state, expected_output, checks, or rubric")
    return result


def validate_episode(episode, case_id=None, version=None):
    """Validate an observed execution; absent evidence stays absent.

    In particular an absent output is not normalized to null: a real JSON null
    is an observable answer and differs from missing evidence.
    """
    _object(episode, "episode")
    _json(episode, "episode")
    for field in ("episode_id", "case_id", "agent_version"):
        _text(episode.get(field), "episode." + field)
    if case_id is not None and episode["case_id"] != case_id:
        _fail("episode.case_id", "does not match requested case")
    if version is not None and episode["agent_version"] != version:
        _fail("episode.agent_version", "does not match requested version")
    if episode.get("status") not in ("completed", "infra_error"):
        _fail("episode.status", "must be completed or infra_error")
    _items(episode.get("events"), "episode.events")
    for index, event in enumerate(episode["events"]):
        path = f"episode.events[{index}]"
        _object(event, path)
        _text(event.get("action"), path + ".action")
        for field in ("cost", "latency_ms", "duration_ms", "tokens", "input_tokens", "output_tokens"):
            if field in event:
                _number(event[field], path + "." + field, nullable=True)
    if "final_state" in episode:
        _object(episode["final_state"], "episode.final_state")
    for field in ("cost", "latency_ms", "execution_latency_ms", "duration_ms", "tokens", "input_tokens", "output_tokens"):
        if field in episode:
            _number(episode[field], "episode." + field, nullable=True)
    return copy.deepcopy(episode)


def public_case(case):
    """Return only explicitly authorized agent inputs; never oracle or metadata."""
    return copy.deepcopy({key: case[key] for key in
                          ("case_id", "business", "input", "initial_state", "limits")
                          if key in case})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_dataset(path):
    """Read non-empty JSONL cases, reporting physical line numbers on errors."""
    path = Path(path)
    cases, seen = [], set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                case = validate_case(json.loads(line, object_pairs_hook=_unique_object))
                if case["case_id"] in seen:
                    raise ValueError("case.case_id: duplicate case ID " + case["case_id"])
            except (ValueError, RecursionError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from None
            seen.add(case["case_id"])
            cases.append(case)
    if not cases:
        raise ValueError(f"{path}: dataset must not be empty")
    return cases
