"""Deadline-bounded grading with conservative, evidence-preserving fallback.

All regex, custom Python, and LLM grading runs in a subprocess. Trusted grader
code retains the user's OS permissions; this is a deadline boundary, not a
security sandbox. Only environment variable names belong in grader configs.
"""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from .adapters import AdapterFailure, MAX_RESPONSE_BYTES, _run_process, decode_json
from .core import grade_episode
from .grading import GRADER_VERSION


MAX_GRADE_REQUEST_BYTES = 8 * 1024 * 1024
_FAILURE_EVIDENCE = {
    "timeout": "Grader exceeded its execution timeout; incomplete grading is not a pass.",
    "worker_exit": "Grader process failed; sensitive diagnostics were omitted.",
    "grader_contract": "Grader request or response violated the grading JSON contract.",
}


def _valid_grade(result, case, episode):
    if not isinstance(result, dict):
        return False
    identities = {"case_id": case["case_id"], "agent_version": episode["agent_version"],
                  "episode_id": episode["episode_id"], "business": case["business"],
                  "family_id": case.get("family_id", case["case_id"])}
    if any(result.get(key) != value for key, value in identities.items()):
        return False
    if result.get("status") not in {"pass", "fail", "unknown", "infra_error"}:
        return False
    if result.get("outcome") is not None and type(result["outcome"]) is not bool:
        return False
    for field in ("evidence", "violations"):
        if not isinstance(result.get(field), list) or any(not isinstance(x, str) for x in result[field]):
            return False
    if not isinstance(result.get("grader"), dict) or not isinstance(result.get("checks"), list):
        return False
    for check in result["checks"]:
        if (not isinstance(check, dict) or not isinstance(check.get("id"), str)
                or check.get("status") not in {"pass", "fail", "unknown"}
                or not isinstance(check.get("evidence"), list)
                or any(not isinstance(x, str) for x in check["evidence"])):
            return False
    return True


def _fallback(case, episode, error_type):
    """Retain safe, deterministic trajectory/state findings after abstention.

    This core check does not import user code, execute regex, or call a model.
    Passing a subset of checks cannot compensate for the missing full verdict.
    """
    try:
        result = grade_episode(case, episode)
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        result = {
            "case_id": case.get("case_id", "invalid-case"),
            "family_id": case.get("family_id", case.get("case_id", "invalid-case")),
            "business": case.get("business", "unknown"),
            "episode_id": episode.get("episode_id"),
            "agent_version": episode.get("agent_version"),
            "status": "unknown", "outcome": None, "violations": [], "evidence": [],
            "cost": episode.get("cost"), "latency_ms": episode.get("latency_ms"),
        }
    if result["status"] != "fail":
        result["status"] = "infra_error" if episode.get("status") == "infra_error" else "unknown"
        result["outcome"] = None
    reason = _FAILURE_EVIDENCE[error_type]
    result["evidence"].append(reason)
    result["grader_version"] = GRADER_VERSION
    result["grader"] = {"kind": "bounded_fallback", "version": GRADER_VERSION,
                        "error_type": error_type}
    result["checks"] = [{"id": "grader:execution", "status": "unknown", "evidence": [reason]}]
    return result


def evaluate_bounded(case, episode, grader_config=None, working_directory=None, timeout=30):
    """Return one grade within a subprocess deadline; execution failures abstain.

    On failure, already observed mandatory violations and business-state failures
    remain failures. The fallback never upgrades a partial verdict to pass.
    Cost and latency of the Agent are untouched; ``grading_latency_ms`` records
    the separate measured duration of grading, including worker startup.
    """
    started = time.monotonic()
    try:
        if (not isinstance(case, dict) or not isinstance(episode, dict)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid bounded grading arguments")
        config = grader_config if grader_config is not None else {"kind": "deterministic"}
        if (not isinstance(config, dict)
                or any(key in config for key in ("api_key", "token", "password"))):
            raise ValueError("grader credentials must use environment variables")
        cwd = os.getcwd() if working_directory is None else os.fspath(working_directory)
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("invalid grader working directory")
        payload = json.dumps({"case": case, "episode": episode, "grader_config": config},
                             ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_GRADE_REQUEST_BYTES:
            raise ValueError("grader request exceeds size limit")
        raw = _run_process([sys.executable, "-m", "agent_eval.grade_worker"], payload,
                           timeout, cwd, worker=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("grader response exceeds size limit")
        result = decode_json(raw)
        if not _valid_grade(result, case, episode):
            raise ValueError("invalid grader response")
    except AdapterFailure as exc:
        error_type = {"timeout": "timeout", "transport": "worker_exit",
                      "adapter_contract": "grader_contract"}[exc.error_type]
        result = _fallback(case, episode, error_type)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        result = _fallback(case if isinstance(case, dict) else {},
                           episode if isinstance(episode, dict) else {}, "grader_contract")
    except (OSError, subprocess.SubprocessError):
        result = _fallback(case, episode, "worker_exit")
    result["grading_latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result
