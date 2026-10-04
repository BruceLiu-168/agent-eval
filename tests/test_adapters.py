"""Integration tests exercise real local subprocess and HTTP boundaries."""

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_eval.adapters import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, execute_adapter
from agent_eval.contracts import validate_episode


CASE = {"case_id": "route-1", "business": "support_routing",
        "input": {"text": "I need a refund", "customer_tier": "vip"}}
ECHO_COMMAND = """
import json,sys
r=json.load(sys.stdin)
c=r['case']
json.dump({'episode_id':'test-episode','case_id':c['case_id'],
 'agent_version':r['version'],'seed':r['seed'],'business':c['business'],
 'status':'completed','output':c,'events':[]},sys.stdout)
"""


class BoundaryHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, request, dict(self.headers)))
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/destination")
            self.end_headers()
            return
        if self.path == "/bad-json":
            body = b"SENSITIVE_SERVER_DIAGNOSTIC"
        elif self.path == "/error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"SENSITIVE_SERVER_DIAGNOSTIC")
            return
        elif self.path == "/oversized":
            self.send_response(200)
            self.send_header("Content-Length", str(MAX_RESPONSE_BYTES + 1))
            self.end_headers()
            return
        elif self.path == "/slow":
            time.sleep(0.5)
            body = b"{}"
        else:
            body = json.dumps({"episode_id": "http-test", "case_id": request["case"]["case_id"],
                               "agent_version": request["version"], "seed": request["seed"],
                               "status": "completed", "output": request["case"], "events": [],
                               "cost": 0.0123, "latency_ms": 42}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BoundaryHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, config, case=None, timeout=3):
        result = execute_adapter(config, copy.deepcopy(case or CASE), "candidate", 7, timeout)
        validate_episode(result, case_id=(case or CASE)["case_id"], version="candidate")
        return result

    def command(self, code):
        return {"kind": "command", "argv": [sys.executable, "-c", code]}

    def assert_error(self, episode, kind):
        self.assertEqual(episode["status"], "infra_error")
        self.assertEqual(episode["error_type"], kind)
        self.assertIsNone(episode["cost"])
        self.assertIsNone(episode["latency_ms"])
        self.assertGreaterEqual(episode["execution_latency_ms"], 0)
        self.assertEqual(episode["metric_provenance"]["cost"], "unknown")

    def test_command_returns_json_and_does_not_invent_cost(self):
        result = self.call(self.command(ECHO_COMMAND))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], CASE)
        self.assertNotIn("cost", result)
        self.assertNotIn("latency_ms", result)
        self.assertEqual(result["metric_provenance"]["cost"], "unknown")
        self.assertEqual(result["metric_provenance"]["execution_latency_ms"], "measured")

    def test_oracle_and_unknown_metadata_do_not_cross_any_agent_boundary(self):
        private = dict(CASE, expected_output={"SECRET": True}, metadata={"SECRET": True},
                       required_actions=["SECRET"], new_future_private_field="SECRET")
        for adapter in (self.command(ECHO_COMMAND), {"kind": "http", "url": self.url + "/run"}):
            with self.subTest(kind=adapter["kind"]):
                result = self.call(adapter, private)
                self.assertEqual(result["output"], CASE)

    def test_python_worker_imports_from_working_directory_and_captures_native_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "custom_agent.py").write_text(
                "import os\nprint('IMPORT_LOG')\nos.write(1,b'NATIVE_IMPORT_LOG')\n"
                "def invoke(case,version,seed):\n"
                " print('FUNCTION_LOG')\n os.write(1,b'NATIVE_FUNCTION_LOG')\n"
                " return {'episode_id':'python-test','case_id':case['case_id'],"
                "'agent_version':version,'status':'completed','events':[],'output':case}\n",
                encoding="utf-8")
            result = self.call({"kind": "python", "callable": "custom_agent:invoke",
                                "working_directory": directory})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], CASE)
        self.assertNotIn("LOG", json.dumps(result))

    def test_python_worker_stdout_contains_only_protocol_and_stderr_has_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "logging_agent.py").write_text(
                "def run(case,version,seed):\n print('worker diagnostic')\n"
                " return {'value':'json'}\n", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = directory + os.pathsep + str(Path(__file__).resolve().parents[1])
            completed = subprocess.run([sys.executable, "-m", "agent_eval.worker", "--callable",
                                        "logging_agent:run"], input=json.dumps({"case": CASE,
                                        "version": "candidate", "seed": 7}).encode(),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                       timeout=3, check=True)
        self.assertEqual(json.loads(completed.stdout), {"value": "json"})
        self.assertIn(b"worker diagnostic", completed.stderr)

    def test_timeout_has_no_automatic_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = str(Path(directory, "calls"))
            code = ("from pathlib import Path; import time; "
                    f"p=Path({counter!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
                    "time.sleep(5)")
            result = self.call(self.command(code), timeout=0.25)
            self.assertEqual(Path(counter).read_text(), "x")
        self.assert_error(result, "timeout")
        self.assertLess(result["execution_latency_ms"], 2000)

    def test_python_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "slow_agent.py").write_text(
                "import time\ndef run(case,version,seed):\n time.sleep(5)\n", encoding="utf-8")
            result = self.call({"kind": "python", "callable": "slow_agent:run",
                                "working_directory": directory}, timeout=0.2)
        self.assert_error(result, "timeout")

    def test_command_exit_error_discards_stderr(self):
        result = self.call(self.command("import sys; sys.stderr.write('TOP_SECRET'); sys.exit(1)"))
        self.assert_error(result, "transport")
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_invalid_json_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        for value in ("invalid SECRET", '{"case_id":"one","case_id":"two"}', '{"cost":NaN}'):
            with self.subTest(value=value):
                result = self.call(self.command(f"print({value!r})"))
                self.assert_error(result, "adapter_contract")
                self.assertNotIn("SECRET", json.dumps(result))

    def test_response_version_and_case_must_match(self):
        for original, replacement in (("r['version']", "'other'"),
                                      ("c['case_id']", "'different-case'")):
            with self.subTest(field=original):
                self.assert_error(self.call(self.command(ECHO_COMMAND.replace(original, replacement))),
                                  "adapter_contract")

    def test_oversized_output_is_bounded(self):
        result = self.call(self.command(f"import sys; sys.stdout.write('x'*{MAX_RESPONSE_BYTES + 100000})"))
        self.assert_error(result, "adapter_contract")

    def test_oversized_request_is_rejected_before_execution(self):
        case = dict(CASE, input={"text": "x" * MAX_REQUEST_BYTES})
        self.assert_error(self.call(self.command(ECHO_COMMAND), case), "adapter_contract")

    def test_http_metrics_are_preserved_with_separate_observed_latency(self):
        result = self.call({"kind": "http", "url": self.url + "/run"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["latency_ms"], 42)
        self.assertEqual(result["cost"], 0.0123)
        self.assertEqual(result["metric_provenance"]["cost"], "self_reported")
        self.assertGreater(result["execution_latency_ms"], 0)

    def test_http_redirect_is_not_followed_and_bearer_secret_not_reported(self):
        before = len(BoundaryHandler.requests)
        with patch.dict(os.environ, {"TEST_EVAL_BEARER": "test-private-secret"}):
            result = self.call({"kind": "http", "url": self.url + "/redirect",
                                "api_key_env": "TEST_EVAL_BEARER"})
        requests = BoundaryHandler.requests[before:]
        self.assertEqual([item[0] for item in requests], ["/redirect"])
        self.assertEqual(requests[0][2]["Authorization"], "Bearer test-private-secret")
        self.assert_error(result, "transport")
        self.assertNotIn("test-private-secret", json.dumps(result))

    def test_http_failures_are_sanitized(self):
        for path, kind in (("/bad-json", "adapter_contract"), ("/error", "transport"),
                           ("/oversized", "adapter_contract")):
            with self.subTest(path=path):
                result = self.call({"kind": "http", "url": self.url + path})
                self.assert_error(result, kind)
                self.assertNotIn("SENSITIVE_SERVER_DIAGNOSTIC", json.dumps(result))

    def test_http_total_timeout(self):
        result = self.call({"kind": "http", "url": self.url + "/slow"}, timeout=0.2)
        self.assert_error(result, "timeout")

    def test_literal_auth_header_and_url_credentials_are_rejected(self):
        for config in ({"kind": "http", "url": self.url + "/run",
                        "headers": {"Authorization": "Bearer literal-secret"}},
                       {"kind": "http", "url": "http://name:password@127.0.0.1/run"}):
            with self.subTest(config=config):
                result = self.call(config)
                self.assert_error(result, "adapter_contract")
                self.assertNotIn("literal-secret", json.dumps(result))
                self.assertNotIn("password", json.dumps(result))

    def test_invalid_configuration_fails_without_execution(self):
        for config in ({"kind": "unknown"}, {"kind": "command", "argv": "echo shell"},
                       {"kind": "python", "callable": "missing-function-format"},
                       {"kind": "http", "url": "file:///etc/passwd"}):
            with self.subTest(config=config):
                self.assert_error(self.call(config), "adapter_contract")

    def test_invalid_arguments_still_return_a_valid_infrastructure_episode(self):
        result = execute_adapter(None, None, None, None, timeout=float("nan"))
        validate_episode(result)
        self.assert_error(result, "adapter_contract")


if __name__ == "__main__":
    unittest.main()
