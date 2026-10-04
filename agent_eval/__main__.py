"""Run: python -m agent_eval demo --out artifacts/demo"""

import argparse
import json
from pathlib import Path

from .core import summarize
from .offline import compare, execute, load_cases, resolve_adapter

DEFAULT_DATASET = str(Path(__file__).resolve().parents[1] / "examples" / "cases.jsonl")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def simulate(dataset, trials, version):
    if trials < 1:
        raise ValueError("trials must be positive")
    cases = load_cases(dataset)
    adapter = resolve_adapter("agent_eval.simulation:run_case")
    runs = [execute(cases, adapter, version, seed) for seed in range(trials)]
    reliability = []
    for i, case in enumerate(cases):
        outcomes = [results[i]["status"] for results, _ in runs]
        known = all(status in ("pass", "fail") for status in outcomes)
        reliability.append({"case_id": case["case_id"], "trial_statuses": outcomes,
                            "success_fraction": outcomes.count("pass") / trials,
                            "any_trial_passed": any(status == "pass" for status in outcomes) if known else None,
                            "all_trials_passed": all(status == "pass" for status in outcomes) if known else None})
    return {"scheme": "C: stateful simulation", "version": version, "trials": trials,
            "summary": summarize([result for results, _ in runs for result in results]),
            "reliability": reliability,
            "episodes": [episode for _, episodes in runs for episode in episodes],
            "limitation": "Scripted policies and synthetic cost/latency. Repeats measure this simulator, not an LLM or production distribution."}


def demo(out):
    from .online import OnlineStore
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    safe = compare(DEFAULT_DATASET)
    unsafe = compare(DEFAULT_DATASET, candidate="regressed")
    write_json(out / "offline.json", safe)
    write_json(out / "blocked.json", unsafe)
    write_json(out / "simulation.json", simulate(DEFAULT_DATASET, 3, "candidate"))
    cases = load_cases(DEFAULT_DATASET)
    results, episodes = execute(cases, resolve_adapter("agent_eval.simulation:run_case"), "baseline")
    store = OnlineStore(str(out / "online.sqlite"))
    records = []
    for case, grade, episode in zip(cases, results, episodes):
        verified_outcome = grade["status"] == "pass" if grade["status"] in ("pass", "fail") else None
        record = {
            "episode": episode, "observed_at": "2026-10-01T09:00:00+00:00",
            "outcome": {"mature_at": "2026-10-02T09:00:00+00:00", "success": verified_outcome,
                        "source": "synthetic_business_audit_v1"},
            "risk_flags": ["contract_failure"] if grade["status"] == "fail" else [],
            "review": {"verified": grade["status"] == "fail", "reviewer": "synthetic_fixture_reviewer"},
            "replay_case": case,
        }
        store.ingest(record, random_rate=0.5)
        records.append(record)
    pending = json.loads(json.dumps(records[0]))
    pending["episode"]["episode_id"] += ":pending"
    pending["outcome"] = {"mature_at": "2026-10-10T09:00:00+00:00", "success": None, "source": "pending"}
    pending["risk_flags"] = ["followup_pending"]
    store.ingest(pending, random_rate=0.5)
    as_of = "2026-10-04T12:00:00+00:00"
    write_json(out / "online.json", store.report(as_of))
    promoted_path = out / "regression.jsonl"
    promotion = store.promote(str(promoted_path), as_of)
    write_json(out / "promotion.json", promotion)
    if promoted_path.exists() and promoted_path.stat().st_size:
        write_json(out / "loop-validation.json", compare(promoted_path))
    manifest = {
        "offline_candidate": safe["candidate"], "offline_gate": safe["gate"]["decision"],
        "unsafe_gate": unsafe["gate"]["decision"], "online_report": str(out / "online.json"),
        "promotion": promotion,
        "note": "All data and agents are synthetic. Small fixtures do not establish production readiness.",
    }
    write_json(out / "summary.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Vendor-neutral Agent Eval reference implementation")
    sub = parser.add_subparsers(dest="command", required=True)
    demo_parser = sub.add_parser("demo", help="run all three schemes and the feedback loop")
    demo_parser.add_argument("--out", default="artifacts/demo")
    offline_parser = sub.add_parser("offline", help="paired offline comparison and fail-closed CI gate")
    offline_parser.add_argument("--dataset", default=DEFAULT_DATASET)
    offline_parser.add_argument("--adapter", default="agent_eval.simulation:run_case")
    offline_parser.add_argument("--baseline", default="baseline")
    offline_parser.add_argument("--candidate", default="candidate")
    offline_parser.add_argument("--out", default="artifacts/offline.json")
    offline_parser.add_argument("--min-families", type=int, default=30)
    offline_parser.add_argument("--margin", type=float, default=0.05)
    offline_parser.add_argument("--min-success-rate", type=float, default=0.8)
    sim_parser = sub.add_parser("simulate", help="repeated stateful episodes with fault injection")
    sim_parser.add_argument("--dataset", default=DEFAULT_DATASET)
    sim_parser.add_argument("--version", choices=["baseline", "candidate", "regressed"], default="candidate")
    sim_parser.add_argument("--trials", type=int, default=3)
    sim_parser.add_argument("--out", default="artifacts/simulation.json")
    args = parser.parse_args(argv)
    if args.command == "demo":
        print(json.dumps(demo(args.out), ensure_ascii=False, indent=2))
        return 0
    if args.command == "simulate":
        result = simulate(args.dataset, args.trials, args.version)
        write_json(args.out, result)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return 0
    result = compare(args.dataset, args.adapter, args.baseline, args.candidate,
                     min_families=args.min_families, margin=args.margin,
                     min_success_rate=args.min_success_rate)
    write_json(args.out, result)
    print(json.dumps(result["gate"], ensure_ascii=False, indent=2))
    return {"PASS": 0, "BLOCK": 2, "INCONCLUSIVE": 3}[result["gate"]["decision"]]


if __name__ == "__main__":
    raise SystemExit(main())
