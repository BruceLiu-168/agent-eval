"""Installed-package acceptance using a separate local Agent service."""

import json
from pathlib import Path
import tempfile
import threading

from .demo_agent import create_server
from .experiment import run_experiment
from .scaffold import initialize


def self_test():
    results = []
    with tempfile.TemporaryDirectory(prefix="agent-eval-selftest-") as directory:
        root = Path(directory)
        server = create_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for template in ("python", "http", "command"):
                project = root / template
                initialize(project, template)
                config_path = project / "eval.json"
                config = json.loads(config_path.read_text())
                if template == "http":
                    config["adapter"]["url"] = f"http://127.0.0.1:{server.server_port}/run"
                config_path.write_text(json.dumps(config))
                outcome = run_experiment(config_path, project / "runs" / "first")
                report = json.loads(Path(outcome["reports"]["json"]).read_text())
                counts = report["versions"]["candidate"]["summary"]["counts"]
                expected = {"pass": 11, "fail": 0, "unknown": 0, "infra_error": 2}
                if counts != expected or outcome["gate"]["decision"] != "INCONCLUSIVE":
                    raise RuntimeError(f"{template} acceptance failed: {counts}")
                resumed = run_experiment(config_path, project / "runs" / "first", resume=True)
                if resumed["run_id"] != outcome["run_id"] or resumed["records"] != 26:
                    raise RuntimeError("resume acceptance failed")
                results.append({"adapter": template, "records": outcome["records"],
                                "candidate": counts, "resume": "passed"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    return {"status": "passed", "adapters": results,
            "note": "Local scripted agents and synthetic business data; no external model calls."}
