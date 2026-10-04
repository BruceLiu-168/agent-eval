"""Scheme A: versioned datasets, adapter execution, paired grading, CI gate."""

import hashlib
import importlib
import inspect
import json
import platform
from pathlib import Path

from . import __version__
from .core import grade_episode, release_gate, summarize


def load_cases(path):
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    identifiers = [case["case_id"] for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("dataset has duplicate case IDs")
    if any(not case.get("expected_state") for case in cases):
        raise ValueError("every dataset case requires an independent expected_state oracle")
    return cases


def resolve_adapter(spec):
    module, name = spec.split(":", 1)
    return getattr(importlib.import_module(module), name)


def execute(cases, adapter, version, seed=0):
    results, episodes = [], []
    for case in cases:
        # The adapter receives its own copy, so it cannot mutate the oracle.
        copy = json.loads(json.dumps(case))
        # Expected state is evaluator-owned and never exposed to the agent.
        for private_key in ("expected_state", "required_actions", "forbidden_actions", "required_order", "required_success_before"):
            copy.pop(private_key, None)
        try:
            episode = adapter(copy, version, seed)
            if not isinstance(episode, dict) or episode.get("agent_version") != version:
                raise ValueError("adapter returned an unexpected agent version")
        except Exception as exc:
            episode = {"episode_id": f"{version}:{case['case_id']}:{seed}",
                       "case_id": case["case_id"], "agent_version": version,
                       "status": "infra_error", "error_type": type(exc).__name__}
        episodes.append(episode)
        results.append(grade_episode(case, episode))
    return results, episodes


def compare(dataset, adapter_spec="agent_eval.simulation:run_case", baseline="baseline",
            candidate="candidate", seed=0, min_families=30, margin=0.05, min_success_rate=0.8):
    if baseline == candidate:
        raise ValueError("baseline and candidate version names must differ")
    cases = load_cases(dataset)
    adapter = resolve_adapter(adapter_spec)
    old, _ = execute(cases, adapter, baseline, seed)
    new, episodes = execute(cases, adapter, candidate, seed)
    source = inspect.getsourcefile(adapter)
    return {
        "scheme": "A: offline CI gate", "baseline": summarize(old), "candidate": summarize(new),
        "gate": release_gate(old, new, min_families=min_families, margin=margin,
                             min_success_rate=min_success_rate),
        "results": {baseline: old, candidate: new}, "episodes": episodes,
        "manifest": {"runner_version": __version__, "python": platform.python_version(),
                     "dataset_sha256": hashlib.sha256(Path(dataset).read_bytes()).hexdigest(),
                     "adapter": adapter_spec,
                     "adapter_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None,
                     "baseline_version": baseline, "candidate_version": candidate, "seed": seed,
                     "grader_version": "state-contract-v1",
                     "limitation": "Real adapters must also pin model/prompt/tools/knowledge/environment versions."},
    }
