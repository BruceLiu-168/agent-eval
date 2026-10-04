"""Generate a local evaluation project that users can replace with real data."""

from importlib.resources import files
import json
from pathlib import Path
import sys


def bundled_dataset():
    return files("agent_eval").joinpath("data/cases.jsonl")


def initialize(directory, template="python"):
    destination = Path(directory).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("project directory must be empty")
    if template not in ("python", "http", "command"):
        raise ValueError("template must be python, http, or command")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "data").mkdir()
    (destination / "data" / "cases.jsonl").write_bytes(bundled_dataset().read_bytes())
    adapter = {
        "python": {"kind": "python", "callable": "custom_adapter:run_case"},
        "http": {"kind": "http", "url": "http://127.0.0.1:8765/run"},
        "command": {"kind": "command", "argv": [sys.executable, "agent_command.py"]},
    }[template]
    config = {
        "schema_version": 1, "dataset": "data/cases.jsonl", "adapter": adapter,
        "versions": ["baseline", "candidate"],
        "execution": {"trials": 1, "seed": 0, "timeout_seconds": 30, "workers": 2},
        "grader": {"kind": "deterministic"},
        "gate": {"enabled": True, "baseline": "baseline", "candidate": "candidate",
                 "min_families": 30, "margin": 0.05, "alpha": 0.05, "min_success_rate": 0.8},
        "provenance": {"data_source": "synthetic_fixtures", "agent_kind": "scripted_demo"},
    }
    (destination / "eval.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "custom_adapter.py").write_text(
        '"""Replace this function with your real Agent plus independent state observer."""\n'
        'from agent_eval.demo_agent import run\n\n'
        'def run_case(case, version, seed):\n'
        '    # case contains only public inputs; do not read the dataset/oracle here.\n'
        '    # Fetch final_state from an independent business system where possible.\n'
        '    return run(case, version, seed)\n', encoding="utf-8")
    (destination / "agent_command.py").write_text(
        '"""JSON stdin/stdout adapter example; log diagnostics to stderr."""\n'
        'import json, sys\nfrom custom_adapter import run_case\n\n'
        'request = json.load(sys.stdin)\n'
        'result = run_case(request["case"], request["version"], request["seed"])\n'
        'print(json.dumps(result, ensure_ascii=False, allow_nan=False))\n', encoding="utf-8")
    (destination / ".gitignore").write_text("data/\nruns/\n.env\n.env.*\n!.env.example\n*.sqlite*\n__pycache__/\n.venv/\n", encoding="utf-8")
    (destination / "README.md").write_text(
        "# Local Agent Eval project\n\n"
        "Replace data/cases.jsonl with privately held, independently labelled cases. "
        "Replace custom_adapter.py or configure an HTTP/command endpoint in eval.json.\n\n"
        "```bash\nagent-eval validate --config eval.json\n"
        "agent-eval run --config eval.json --out runs/first\n"
        "agent-eval run --config eval.json --out runs/first --resume\n"
        "agent-eval gate --report runs/first/report.json\n```\n\n"
        "For the HTTP template, first start `agent-eval serve-agent --port 8765`. "
        "Open runs/first/report.html locally. Demo fixtures may produce INCONCLUSIVE, "
        "which is an intentionally closed release gate. Use run --enforce-gate in CI.\n\n"
        "No automatic Agent retries occur. Resume reuses saved records; unrecorded executions "
        "may run again after interruption. Use read-only snapshots/sandboxes and idempotency keys. "
        "Raw records stay local and may contain sensitive data. Grader API calls happen only "
        "when explicitly configured. Synthetic fixtures do not establish real Agent quality.\n", encoding="utf-8")
    return {"directory": str(destination), "config": str(destination / "eval.json"), "template": template}
