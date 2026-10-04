import csv
import io
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from agent_eval.reporting import build_report, write_report


def record(case_id="case-1", status="pass", *, version="candidate", trial=0,
           business="refund", family_id=None, cost=0.1, latency=10,
           execution_latency=20, evidence=None, violations=None):
    return {"version": version, "case_id": case_id, "trial": trial, "seed": trial + 42,
            "episode": {"input": "PRIVATE INPUT", "final_answer": "PRIVATE ANSWER",
                        "events": [{"action": "PRIVATE ACTION"}],
                        "execution_latency_ms": execution_latency,
                        "metric_provenance": {"cost": "synthetic", "latency_ms": "synthetic"}},
            "grade": {"case_id": case_id, "business": business, "family_id": family_id or case_id,
                      "status": status, "cost": cost, "latency_ms": latency,
                      "evidence": evidence or ["independent state checked"],
                      "violations": violations or []}}


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.in_script = False
        self.event_handlers = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.scripts.append("")
        self.event_handlers.extend((name, value) for name, value in attrs if name.startswith("on"))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts[-1] += data


class ReportingTests(unittest.TestCase):
    def test_rates_expose_all_denominators(self):
        report = build_report({"run_id": "r"}, [record(str(i), status) for i, status in enumerate(
            ["pass", "pass", "fail", "unknown", "infra_error"])])
        summary = report["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["adjudicated"], 3)
        self.assertEqual(summary["counts"], {"pass": 2, "fail": 1, "unknown": 1, "infra_error": 1})
        self.assertAlmostEqual(summary["success_rate_among_adjudicated"], 2 / 3)
        self.assertEqual(summary["success_rate_among_all"], 0.4)
        self.assertEqual(summary["evidence_coverage"], 0.6)

    def test_versions_and_business_have_separate_denominators(self):
        report = build_report({}, [record("a"), record("b", "fail", business="access"),
                                   record("a", "fail", version="baseline")])
        candidate = report["versions"]["candidate"]
        self.assertEqual(candidate["summary"]["success_rate_among_all"], 0.5)
        self.assertEqual(candidate["business"]["refund"]["total"], 1)
        self.assertEqual(candidate["business"]["refund"]["success_rate_among_all"], 1)
        self.assertEqual(report["versions"]["baseline"]["summary"]["success_rate_among_all"], 0)

    def test_sparse_import_exposes_dataset_coverage_separately_from_evidence(self):
        report = build_report({"dataset_case_count": 100}, [record("a")])
        self.assertEqual(report["summary"]["evidence_coverage"], 1)
        self.assertEqual(report["versions"]["candidate"]["summary"]["dataset_coverage"], {
            "expected_cases": 100, "observed_cases": 1, "missing_cases": 99, "coverage": 0.01,
            "note": "case coverage for this version; repeated trials do not increase coverage"})
        self.assertEqual(len(report["rows"]), 1)
        with tempfile.TemporaryDirectory() as directory:
            paths = write_report(report, Path(directory))
            markup = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertIn("1/100 用例；缺失 99", markup)
            self.assertIn("已提供 episode 证据覆盖", markup)
            self.assertIn("数据集覆盖（按版本）", markup)

    def test_dataset_coverage_counts_cases_per_version_and_includes_missing_versions(self):
        report = build_report({"case_count": 2, "versions": ["candidate", "baseline", "missing"]},
                              [record("a"), record("a", trial=1), record("b", version="baseline")])
        coverage = report["dataset_coverage"]
        self.assertEqual(coverage["versions"]["candidate"]["coverage"], 0.5)
        self.assertEqual(coverage["versions"]["baseline"]["coverage"], 0.5)
        self.assertEqual(coverage["versions"]["missing"]["coverage"], 0)
        self.assertEqual(coverage["versions"]["missing"]["missing_cases"], 2)
        self.assertEqual(coverage["minimum_version_coverage"], 0)
        self.assertEqual(report["versions"]["missing"]["summary"]["total"], 0)

    def test_unknown_dataset_size_does_not_claim_full_coverage(self):
        report = build_report({}, [record()])
        coverage = report["versions"]["candidate"]["summary"]["dataset_coverage"]
        self.assertIsNone(coverage["expected_cases"])
        self.assertIsNone(coverage["coverage"])
        self.assertIsNone(coverage["missing_cases"])
        self.assertIsNone(report["dataset_coverage"]["minimum_version_coverage"])

    def test_missing_cost_is_not_zero_or_complete_cost_per_success(self):
        report = build_report({}, [record("a", cost=0.3), record("b", "fail", cost=None)])
        cost = report["summary"]["cost"]
        self.assertEqual(cost["known_count"], 1)
        self.assertEqual(cost["coverage"], 0.5)
        self.assertEqual(cost["observed_total"], 0.3)
        self.assertIsNone(cost["total"])
        self.assertIsNone(cost["cost_per_success"])
        self.assertIsNone(report["rows"][1]["cost"])

    def test_efficiency_includes_failed_and_infrastructure_attempts(self):
        report = build_report({}, [record("a", cost=1, latency=10, execution_latency=100),
                                   record("b", "fail", cost=3, latency=30, execution_latency=300),
                                   record("c", "infra_error", cost=5, latency=50, execution_latency=500)])
        summary = report["summary"]
        self.assertEqual(summary["cost"]["cost_per_success"], 9)
        self.assertEqual(summary["latency_ms"]["execution"]["p50"], 300)
        self.assertEqual(summary["latency_ms"]["reported"]["p50"], 30)
        self.assertEqual(summary["latency_ms"]["execution"]["p95"], 480)
        self.assertEqual(summary["latency_ms"]["execution"]["known_count"], 3)
        self.assertEqual(summary["metric_provenance"][0]["value"]["cost"], "synthetic")

    def test_unobserved_and_invalid_numbers_are_missing(self):
        report = build_report({}, [record("a", cost=True, latency=float("nan"), execution_latency=-1),
                                   record("b", cost=float("inf"), latency=None, execution_latency=None)])
        summary = report["summary"]
        self.assertIsNone(summary["cost"]["observed_total"])
        self.assertEqual(summary["cost"]["coverage"], 0)
        self.assertIsNone(summary["latency_ms"]["reported"]["p50"])
        self.assertEqual(summary["latency_ms"]["execution"]["coverage"], 0)
        json.dumps(report, allow_nan=False)

    def test_absent_or_string_metric_provenance_does_not_break_import_reports(self):
        attempts = [record("a"), record("b")]
        attempts[0]["episode"].pop("metric_provenance")
        attempts[1]["episode"]["metric_provenance"] = "self_reported"
        report = build_report({}, attempts)
        provenance = report["summary"]["metric_provenance"]
        self.assertEqual({item["value"] for item in provenance}, {"unspecified", "self_reported"})

    def test_numeric_overflow_does_not_break_other_result_statistics(self):
        report = build_report({}, [record("a", cost=1e308), record("b", "fail", cost=1e308)])
        summary = report["summary"]
        self.assertEqual(summary["counts"]["pass"], 1)
        self.assertIsNone(summary["cost"]["total"])
        self.assertIsNone(summary["cost"]["cost_per_success"])
        self.assertEqual(summary["cost"]["aggregation_error"], "numeric_overflow")
        json.dumps(report, allow_nan=False)

    def test_repeated_trials_do_not_create_independent_cases(self):
        attempts = [record("a", "pass", trial=0, family_id="shared"),
                    record("a", "unknown", trial=1, family_id="shared"),
                    record("b", "fail", trial=0, family_id="shared"),
                    record("b", "infra_error", trial=1, family_id="shared"),
                    record("c", "pass", trial=0, family_id="shared"),
                    record("c", "fail", trial=1, family_id="shared"),
                    record("d", "fail", family_id="shared")]
        report = build_report({}, attempts)
        summary = report["summary"]
        self.assertEqual(summary["trial_count"], 7)
        self.assertEqual(summary["unique_cases"], 4)
        self.assertEqual(summary["unique_families"], 1)
        cases = {case["case_id"]: case for case in report["case_results"]}
        self.assertTrue(cases["a"]["any_pass"])
        self.assertIsNone(cases["a"]["all_pass"])
        self.assertIsNone(cases["b"]["any_pass"])
        self.assertFalse(cases["b"]["all_pass"])
        self.assertTrue(cases["c"]["any_pass"])
        self.assertFalse(cases["c"]["all_pass"])
        self.assertFalse(cases["d"]["any_pass"])
        self.assertFalse(cases["d"]["all_pass"])
        self.assertEqual(summary["case_any_pass"]["unknown_cases"], 1)

    def test_all_pass_known_failure_is_decisive_despite_unknown_trial(self):
        for unresolved in ("unknown", "infra_error"):
            with self.subTest(unresolved=unresolved):
                report = build_report({}, [record("a", "fail"), record("a", unresolved, trial=1)])
                self.assertFalse(report["case_results"][0]["all_pass"])
                self.assertIsNone(report["case_results"][0]["any_pass"])
                self.assertEqual(report["summary"]["case_all_pass"]["fail_cases"], 1)
                self.assertEqual(report["summary"]["case_all_pass"]["unknown_cases"], 0)

    def test_empty_run_does_not_claim_success_or_free_cost(self):
        report = build_report({}, [])
        summary = report["summary"]
        self.assertIsNone(summary["success_rate_among_all"])
        self.assertEqual(summary["evidence_coverage"], 0)
        self.assertIsNone(summary["cost"]["total"])
        self.assertIsNone(summary["cost"]["cost_per_success"])
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(Path(write_report(report, Path(directory))["html"]).is_file())

    def test_gate_is_preserved_and_violations_remain_visible(self):
        gate = {"decision": "BLOCK", "blocking_reasons": ["unsafe action"]}
        report = build_report({}, [record("a", "fail", violations=["forbidden_action:grant"]),
                                   record("a", "fail", trial=1, violations=["forbidden_action:grant"])], gate)
        self.assertEqual(report["gate"], gate)
        self.assertEqual(report["critical_violations"][0]["attempts"], 2)
        self.assertEqual(report["critical_violations"][0]["unique_cases"], 1)
        self.assertEqual(report["summary"]["hard_violation_attempts"], 2)
        gate["decision"] = "PASS"
        self.assertEqual(report["gate"]["decision"], "BLOCK")

    def test_report_omits_raw_agent_data(self):
        report = build_report({"run_id": "r"}, [record()])
        serialized = json.dumps(report)
        self.assertNotIn("PRIVATE INPUT", serialized)
        self.assertNotIn("PRIVATE ANSWER", serialized)
        self.assertNotIn("PRIVATE ACTION", serialized)
        self.assertIn("independent state checked", serialized)

    def test_html_escapes_untrusted_attributes_content_and_script_endings(self):
        payload = '\"><img src=x onerror="alert(1)"></script><script>attacker()</script>'
        report = build_report({"run_id": payload}, [record(payload, version=payload, business=payload,
                              evidence=[payload], violations=[payload])])
        with tempfile.TemporaryDirectory() as directory:
            paths = write_report(report, Path(directory))
            markup = Path(paths["html"]).read_text(encoding="utf-8")
            parser = ScriptParser()
            parser.feed(markup)
            self.assertEqual(len(parser.scripts), 1)
            self.assertNotIn("attacker()", parser.scripts[0])
            self.assertFalse(parser.event_handlers)
            self.assertIn("&lt;script&gt;attacker()&lt;/script&gt;", markup)
            self.assertNotIn("<img", markup)
            self.assertNotIn("fetch(", markup)
            self.assertNotIn("<script src=", markup)

    def test_csv_prevents_formula_injection(self):
        for payload in ['=HYPERLINK("https://invalid")', "+cmd", "-cmd", "@SUM(1,2)",
                        "  =SUM(1,2)", "\tformula", "\rformula", "\nformula"]:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                report = build_report({}, [record(payload, version=payload)])
                paths = write_report(report, Path(directory))
                data = Path(paths["csv"]).read_text(encoding="utf-8")
                row = list(csv.DictReader(io.StringIO(data)))[0]
                self.assertTrue(row["case_id"].startswith("'"))
                self.assertTrue(row["version"].startswith("'"))

    def test_json_is_strict_and_serialization_failure_preserves_existing_files(self):
        with self.assertRaises(ValueError):
            build_report({"unexpected": float("nan")}, [])
        with tempfile.TemporaryDirectory() as directory:
            report = build_report({"run_id": "valid"}, [record()])
            paths = write_report(report, Path(directory))
            previous = {key: Path(path).read_bytes() for key, path in paths.items()}
            report["manifest"]["bad"] = float("inf")
            with self.assertRaises(ValueError):
                write_report(report, Path(directory))
            self.assertEqual(previous, {key: Path(path).read_bytes() for key, path in paths.items()})
            self.assertFalse(list(Path(directory).glob(".report-*.tmp")))

    def test_duplicate_records_or_conflicting_metadata_cannot_inflate_metrics(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_report({}, [record(), record()])
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            build_report({}, [record(), record(trial=1, family_id="changed")])

    def test_artifacts_are_valid_utf8_and_paths_are_absolute(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_report({"run_id": "中文运行"}, [record("退款业务")])
            paths = write_report(report, Path(directory))
            self.assertEqual(set(paths), {"json", "csv", "html"})
            self.assertTrue(all(Path(path).is_absolute() for path in paths.values()))
            self.assertEqual(json.loads(Path(paths["json"]).read_text(encoding="utf-8")), report)
            self.assertIn("退款业务", Path(paths["html"]).read_text(encoding="utf-8"))
            self.assertIn("中文运行", Path(paths["html"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
