"""Evidence-based grading and conservative, fixed-sample release decisions.

This module never uses an agent's final answer as a business-state oracle.
The statistical gate estimates a macro-average over independent task families,
not production traffic quality. Families must be defined before an experiment.
"""

import math
from collections import defaultdict


def subset_matches(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and subset_matches(value, actual[key])
            for key, value in expected.items()
        )
    return type(expected) is type(actual) and expected == actual


def grade_episode(case, episode):
    result = {
        "case_id": case["case_id"],
        "family_id": case.get("family_id", case["case_id"]),
        "business": case["business"],
        "episode_id": episode.get("episode_id"),
        "agent_version": episode.get("agent_version"),
        "status": "unknown",
        "outcome": None,
        "violations": [],
        "evidence": [],
        "cost": episode.get("cost"),
        "latency_ms": episode.get("latency_ms"),
        "grader_version": "state-contract-v1",
    }
    if episode.get("case_id") != case["case_id"]:
        result["evidence"].append("episode/case identity mismatch")
        return result
    infrastructure_error = episode.get("status") == "infra_error"
    if infrastructure_error:
        result["status"] = "infra_error"
        result["evidence"].append("execution infrastructure unavailable")
    if episode.get("status") not in ("completed", "infra_error"):
        result["evidence"].append("no completed episode")
        return result
    events = episode.get("events")
    if not isinstance(events, list) or any(
        not isinstance(event, dict) or not isinstance(event.get("action"), str)
        for event in events
    ):
        result["evidence"].append("missing or malformed observable trajectory")
        return result
    actions = [event["action"] for event in events]
    for action in case.get("forbidden_actions", []):
        if action in actions:
            result["violations"].append("forbidden_action:" + action)
    for action in case.get("required_actions", []):
        if action not in actions and not infrastructure_error:
            result["violations"].append("missing_action:" + action)
    for before, after in case.get("required_order", []):
        invalid_order = (after in actions and (before not in actions or actions.index(before) >= actions.index(after)))
        missing_completed_actions = not infrastructure_error and (before not in actions or after not in actions)
        if invalid_order or missing_completed_actions:
            result["violations"].append("required_order:" + before + "<" + after)
    for guard, action in case.get("required_success_before", []):
        latest = None
        for event in events:
            if event["action"] == action:
                response = latest.get("result") if latest else None
                if not isinstance(response, dict) or response.get("ok") is not True:
                    result["violations"].append("successful_guard:" + guard + "<" + action)
            if event["action"] == guard:
                latest = event
    metrics = {"max_steps": len(events), "max_cost": episode.get("cost"),
               "max_latency_ms": episode.get("latency_ms")}
    missing_metrics = []
    for limit, bound in case.get("limits", {}).items():
        value = metrics.get(limit)
        if (limit not in metrics or not isinstance(value, (int, float))
                or isinstance(value, bool) or not math.isfinite(value) or value < 0):
            missing_metrics.append(limit)
        elif value > bound:
            result["violations"].append("budget:" + limit)
    expected = case.get("expected_state")
    if infrastructure_error:
        pass  # Partial state is not a completed business outcome.
    elif not isinstance(expected, dict) or not expected or not isinstance(episode.get("final_state"), dict):
        result["evidence"].append("missing independent business-state oracle or observed state")
    else:
        result["outcome"] = subset_matches(expected, episode["final_state"])
        result["evidence"].append("business state matches" if result["outcome"] else "business state mismatch")
    if missing_metrics:
        result["evidence"].append("missing budget evidence:" + ",".join(missing_metrics))
    if result["violations"] or result["outcome"] is False:
        result["status"] = "fail"
    elif result["outcome"] is True and not missing_metrics:
        result["status"] = "pass"
    return result


def summarize(results):
    counts = {key: sum(r["status"] == key for r in results)
              for key in ("pass", "fail", "unknown", "infra_error")}
    adjudicated = counts["pass"] + counts["fail"]
    return {
        "total": len(results), "counts": counts,
        "adjudicated": adjudicated,
        "success_rate_among_adjudicated": counts["pass"] / adjudicated if adjudicated else None,
        "evidence_coverage": adjudicated / len(results) if results else 0,
        "hard_violation_episodes": sum(bool(r["violations"]) for r in results),
        "note": "Unknown and infrastructure errors are visible; they are never counted as passes.",
    }


def release_gate(baseline, candidate, *, min_families=30, margin=0.05, alpha=0.05,
                 min_success_rate=0.8):
    """Paired Hoeffding lower bounds; alpha is split across overall/business tests.

    Each family's paired case outcomes are averaged before inference. The bound
    is conservative and valid only under independent family sampling and a
    predeclared fixed sample. It is not a sequential testing procedure.
    """
    if min_families < 1 or not 0 <= margin <= 1 or not 0 < alpha < 1 or not 0 <= min_success_rate <= 1:
        raise ValueError("invalid gate policy")
    reasons, insufficient, comparisons = [], [], {}
    b = {r["case_id"]: r for r in baseline}
    c = {r["case_id"]: r for r in candidate}
    if len(b) != len(baseline) or len(c) != len(candidate):
        insufficient.append("duplicate case IDs; aggregate repeated trials explicitly")
    if not b or b.keys() != c.keys():
        insufficient.append("missing or unequal paired case coverage")
    if any(r["violations"] for r in candidate):
        reasons.append("candidate violates a mandatory constraint or budget")
    if any(r["status"] not in ("pass", "fail") for r in baseline + candidate):
        insufficient.append("unknown or infrastructure errors require investigation")
    paired = [(b[key], c[key]) for key in sorted(b.keys() & c.keys())]
    if any(x["family_id"] != y["family_id"] or x["business"] != y["business"] for x, y in paired):
        insufficient.append("paired case metadata differs")
    slices = ["overall"] + sorted({y["business"] for _, y in paired})
    for name in slices:
        families = defaultdict(list)
        success_by_family = defaultdict(list)
        for old, new in paired:
            if name != "overall" and new["business"] != name:
                continue
            if old["status"] not in ("pass", "fail") or new["status"] not in ("pass", "fail"):
                continue
            families[new["family_id"]].append(int(new["status"] == "pass") - int(old["status"] == "pass"))
            success_by_family[new["family_id"]].append(int(new["status"] == "pass"))
        deltas = [sum(values) / len(values) for values in families.values()]
        n = len(deltas)
        delta = sum(deltas) / n if n else None
        radius = math.sqrt(2 * math.log(3 * len(slices) / alpha) / n) if n else None
        lower = max(-1, delta - radius) if n else None
        upper = min(1, delta + radius) if n else None
        candidate_rate = sum(sum(v) / len(v) for v in success_by_family.values()) / n if n else None
        absolute_lower = max(0, candidate_rate - math.sqrt(math.log(3 * len(slices) / alpha) / (2 * n))) if n else None
        comparisons[name] = {"independent_families": n, "delta": delta,
                             "lower_bound": lower, "upper_bound": upper,
                             "candidate_success_rate": candidate_rate,
                             "candidate_success_lower_bound": absolute_lower}
        if n and candidate_rate < min_success_rate:
            reasons.append(name + ": observed quality below absolute floor")
        elif n and absolute_lower < min_success_rate:
            insufficient.append(name + ": absolute quality floor not established")
        if n < min_families:
            insufficient.append(name + ": too few independent families")
        elif upper < -margin:
            reasons.append(name + ": demonstrated quality regression")
        elif lower < -margin:
            insufficient.append(name + ": non-inferiority not established")
    return {
        "decision": "BLOCK" if reasons else "INCONCLUSIVE" if insufficient else "PASS",
        "blocking_reasons": reasons, "insufficient_evidence": insufficient,
        "comparisons": comparisons,
        "policy": {"min_families": min_families, "noninferiority_margin": margin,
                   "min_success_rate": min_success_rate,
                   "alpha": alpha, "method": "paired family-mean Hoeffding, Bonferroni across slices",
                   "scope": "fixed-sample macro task-family quality, not production impact"},
    }


def calibrate_judge(labels):
    """True means a severe failure. Unknown predictions abstain, never pass."""
    counts = dict(tp=0, fp=0, tn=0, fn=0, abstain=0)
    for item in labels:
        truth, prediction = item["human_failure"], item.get("judge_failure")
        if type(truth) is not bool or (prediction is not None and type(prediction) is not bool):
            raise ValueError("calibration labels must be booleans or prediction null")
        if prediction is None:
            counts["abstain"] += 1
        else:
            counts["tp" if truth and prediction else "fn" if truth else "fp" if prediction else "tn"] += 1
    positives = sum(item["human_failure"] for item in labels)
    negatives = len(labels) - positives
    return {**counts,
            "severe_failure_recall_all_labels": counts["tp"] / positives if positives else None,
            "false_positive_rate_all_labels": counts["fp"] / negatives if negatives else None,
            "coverage": 1 - counts["abstain"] / len(labels) if labels else 0}
