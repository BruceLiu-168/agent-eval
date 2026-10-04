"""Acceptance tests for the independently served scripted demo Agent."""

import copy
import json
import threading
import unittest
import urllib.error
import urllib.request

from agent_eval.adapters import execute_adapter
from agent_eval.contracts import public_case
from agent_eval.demo_agent import create_server, run
from agent_eval.simulation import load_demo_cases


class DemoAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.cases = {case["case_id"]: case for case in load_demo_cases()}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, payload):
        request = urllib.request.Request(self.url + "/run", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_health_identifies_scripted_agent_and_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with urllib.request.urlopen(self.url + "/health", timeout=3) as response:
            health = json.load(response)
        self.assertEqual(health["agent_kind"], "scripted_demo")
        self.assertEqual(set(health["versions"]), {"baseline", "candidate", "regressed"})

    def test_http_refund_and_access_have_observable_state(self):
        for case_id in ("refund_timeout_after", "access_preapproved"):
            with self.subTest(case=case_id):
                result = execute_adapter({"kind": "http", "url": self.url + "/run"},
                                         public_case(self.cases[case_id]), "candidate", 11, 3)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["final_state"]["outcome"], "succeeded")
                self.assertEqual(result["metric_provenance"]["cost"], "synthetic")
                self.assertEqual(result["metric_provenance"]["latency_ms"], "synthetic")
                self.assertEqual(result["metric_provenance"]["execution_latency_ms"], "measured")

    def test_three_versions_expose_intended_quality_differences(self):
        case = public_case(self.cases["refund_timeout_after"])
        original = copy.deepcopy(case)
        candidate = run(case, "candidate")
        baseline = run(case, "baseline")
        regressed = run(case, "regressed")
        self.assertEqual(candidate["final_state"]["order"]["refund_count"], 1)
        self.assertGreater(baseline["final_state"]["order"]["refund_count"], 1)
        self.assertNotIn("verify_identity", [event["action"] for event in regressed["events"]])
        self.assertEqual(case, original)

    def test_support_routing_is_driven_by_input_and_returns_json_output(self):
        case = {"case_id": "support-1", "business": "support_routing",
                "input": {"text": "My account was hacked", "customer_tier": "vip"}}
        candidate = self.post({"case": case, "version": "candidate", "seed": 0})
        baseline = self.post({"case": case, "version": "baseline", "seed": 0})
        self.assertEqual(candidate["output"], {"queue": "security", "priority": "high"})
        self.assertEqual(baseline["output"]["queue"], "general")
        self.assertIsNone(candidate["cost"])
        self.assertEqual(candidate["metric_provenance"]["latency_ms"], "measured")

    def test_support_routing_handles_chinese_and_general_requests(self):
        for text, queue in (("订单需要退款", "billing"), ("页面报错", "technical"),
                            ("账号泄露", "security"), ("你好", "general")):
            with self.subTest(text=text):
                case = {"case_id": "support-zh", "business": "support_routing", "input": {"text": text}}
                self.assertEqual(run(case, "candidate")["output"], {"queue": queue, "priority": "normal"})

    def test_http_rejects_private_oracle_and_unknown_fields(self):
        for field in ("expected_state", "expected_output", "metadata", "required_actions", "unknown"):
            with self.subTest(field=field):
                case = public_case(self.cases["refund_happy"])
                case[field] = {"secret": "oracle"}
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.post({"case": case, "version": "candidate", "seed": 0})
                self.assertEqual(caught.exception.code, 400)
                self.assertNotIn("oracle", caught.exception.read().decode())
                caught.exception.close()

    def test_direct_python_adapter_uses_same_public_contract(self):
        result = execute_adapter({"kind": "python", "callable": "agent_eval.demo_agent:run"},
                                 self.cases["refund_happy"], "candidate", 4, 3)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_state"]["order"]["refunded"], 40.0)

    def test_invalid_version_returns_fixed_error(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({"case": public_case(self.cases["refund_happy"]),
                       "version": "SECRET_INVALID_VERSION", "seed": 0})
        self.assertEqual(caught.exception.code, 400)
        self.assertNotIn("SECRET", caught.exception.read().decode())
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
