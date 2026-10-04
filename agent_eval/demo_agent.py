"""Independent scripted test Agent; synthetic business behavior, no LLM calls.

Run ``python -m agent_eval.demo_agent --port 8765`` and POST the adapter request
contract to ``http://127.0.0.1:8765/run``. This development server deliberately
binds only to loopback, has no authentication, and is not a production service.
The policy only sees case inputs, initial state, and budgets, never eval targets.
"""

from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time

from .adapters import MAX_REQUEST_BYTES, decode_json
from .simulation import VERSIONS, run_case


PUBLIC_FIELDS = {"case_id", "business", "input", "initial_state", "limits"}


def run(case: dict, version: str, seed: int = 0) -> dict:
    """Refund/access simulator plus an independent support-routing business.

    Support candidate handles English/Chinese billing, security, technical, and
    general requests, including a VIP priority flag. Baseline misses security
    routing and regressed routes every request to general support.
    """
    if (not isinstance(case, dict) or set(case) - PUBLIC_FIELDS
            or not isinstance(case.get("case_id"), str) or not case["case_id"]
            or version not in VERSIONS or type(seed) is not int):
        raise ValueError("invalid public case or agent version")
    if case.get("business") in {"refund", "access_request"}:
        result = run_case(case, version, seed)
        result["metric_provenance"] = {"cost": "synthetic", "latency_ms": "synthetic"}
        result["agent_kind"] = "scripted_demo"
        return result
    if case.get("business") != "support_routing":
        raise ValueError("unsupported demo business")
    started = time.monotonic()
    request = case.get("input")
    if not isinstance(request, dict) or not isinstance(request.get("text"), str):
        raise ValueError("support input requires text")
    text = request["text"].casefold()
    queue = "general"
    if version != "regressed":
        groups = [
            ("security", ("hacked", "compromised", "breach", "stolen", "盗号", "泄露")),
            ("billing", ("refund", "invoice", "payment", "charge", "退款", "发票", "扣款")),
            ("technical", ("error", "crash", "bug", "broken", "故障", "报错", "崩溃")),
        ]
        for name, keywords in groups:
            if name == "security" and version == "baseline":
                continue
            if any(keyword in text for keyword in keywords):
                queue = name
                break
    priority = "high" if request.get("customer_tier") == "vip" else "normal"
    output = {"queue": queue, "priority": priority}
    final_state = copy.deepcopy(case.get("initial_state", {}))
    final_state["routing"] = dict(output)
    return {
        "episode_id": f"{version}:{case['case_id']}:{seed}", "case_id": case["case_id"],
        "agent_version": version, "business": case["business"], "seed": seed,
        "status": "completed", "output": output, "final_state": final_state,
        "events": [{"action": "classify_request", "result": {"queue": queue}},
                   {"action": "route_ticket", "args": output, "result": {"ok": True}}],
        "cost": None, "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "metric_provenance": {"cost": "unknown", "latency_ms": "measured"},
        "agent_kind": "scripted_demo",
    }


class DemoAgentHandler(BaseHTTPRequestHandler):
    """Strict bounded JSON API; request/response bodies are never logged."""

    server_version = "AgentEvalDemo/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def _send(self, code, body):
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "agent_kind": "scripted_demo",
                             "versions": sorted(VERSIONS)})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/run":
            self._send(404, {"error": "not_found"})
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                self._send(400, {"error": "unsupported_transfer_encoding"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                self._send(411, {"error": "content_length_required"})
                return
            length = int(lengths[0])
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._send(413, {"error": "request_too_large"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"error": "json_required"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._send(400, {"error": "invalid_request"})
                return
            request = decode_json(body)
            if (not isinstance(request, dict) or set(request) != {"case", "version", "seed"}
                    or not isinstance(request["version"], str)):
                raise ValueError("invalid request")
            result = run(request["case"], request["version"], request["seed"])
        except (ValueError, KeyError, TypeError, RecursionError):
            self._send(400, {"error": "invalid_request"})
            return
        except Exception:
            self._send(500, {"error": "agent_execution_failed"})
            return
        self._send(200, result)


def create_server(port=8765):
    """Return a loopback-only server; port=0 is convenient for local tests."""
    return ThreadingHTTPServer(("127.0.0.1", port), DemoAgentHandler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = create_server(args.port)
    print(f"Scripted demo Agent: http://127.0.0.1:{server.server_port}/run", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
