"""User-facing local evaluation workflow."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__


def _display(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _gate_exit(gate):
    return {"PASS": 0, "BLOCK": 2, "INCONCLUSIVE": 3}.get((gate or {}).get("decision"), 3)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("demo", "offline", "simulate"):
        from .__main__ import legacy_main
        return legacy_main(argv)
    if argv and argv[0] == "online":
        from .online import main as online_main
        return online_main(argv[1:])
    if argv and argv[0] == "serve-agent":
        from .demo_agent import main as serve
        return serve(argv[1:]) or 0
    parser = argparse.ArgumentParser(description="Local Agent Eval: validate, run, inspect, and feed back evidence")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a runnable evaluation project")
    init.add_argument("directory")
    init.add_argument("--template", choices=("python", "http", "command"), default="python")
    validate = sub.add_parser("validate", help="validate real datasets and configuration before execution")
    sources = validate.add_mutually_exclusive_group(required=True)
    sources.add_argument("--config")
    sources.add_argument("--dataset")
    run = sub.add_parser("run", help="execute a checkpointed, versioned experiment")
    run.add_argument("--config", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--enforce-gate", action="store_true", help="exit 2/3 if the release gate blocks/is inconclusive")
    score = sub.add_parser("evaluate", help="score existing episode JSONL without running an Agent")
    score.add_argument("--dataset", required=True)
    score.add_argument("--episodes", required=True)
    score.add_argument("--out", required=True)
    score.add_argument("--grader", help="optional JSON grader configuration")
    gate = sub.add_parser("gate", help="enforce an existing report's release decision")
    gate.add_argument("--report", required=True)
    calibration = sub.add_parser("calibrate", help="measure a judge against independent human labels")
    calibration.add_argument("--labels", required=True)
    sub.add_parser("self-test", help="exercise installed Python/HTTP/command adapters and resume")
    for command, description in (("online", "ingest, annotate, report and promote local events"),
                                 ("serve-agent", "start the independent local test Agent"),
                                 ("demo", "run the legacy three-scheme demo"),
                                 ("offline", "run the legacy paired gate"),
                                 ("simulate", "run repeated stateful fixtures")):
        sub.add_parser(command, help=description)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            from .scaffold import initialize
            _display(initialize(args.directory, args.template))
        elif args.command == "validate":
            from .config import load_config
            from .contracts import load_dataset
            config = load_config(args.config) if args.config else None
            cases = load_dataset(config["dataset"] if config else args.dataset)
            _display({"valid": True, "cases": len(cases), "businesses": sorted({c["business"] for c in cases}),
                      "versions": config["versions"] if config else None})
        elif args.command == "run":
            from .experiment import run_experiment
            result = run_experiment(args.config, args.out, args.resume,
                                    progress=lambda done, total: print(f"Evaluated {done}/{total}", file=sys.stderr))
            _display(result)
            return _gate_exit(result["gate"]) if args.enforce_gate else 0
        elif args.command == "evaluate":
            from .config import read_json
            from .experiment import evaluate_recorded
            _display(evaluate_recorded(args.dataset, args.episodes, args.out,
                                       read_json(args.grader) if args.grader else None,
                                       str(Path(args.grader).resolve().parent) if args.grader else None))
        elif args.command == "gate":
            from .config import read_json
            gate = read_json(args.report).get("gate")
            _display(gate or {"decision": "INCONCLUSIVE", "reason": "No release gate was configured."})
            return _gate_exit(gate)
        elif args.command == "calibrate":
            from .core import calibrate_judge
            labels = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line.strip()]
            _display(calibrate_judge(labels))
        elif args.command == "self-test":
            from .selftest import self_test
            _display(self_test())
        return 0
    except (ValueError, OSError, ImportError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Saved records can be resumed using the same config and --out.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
