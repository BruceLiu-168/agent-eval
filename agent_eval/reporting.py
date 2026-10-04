"""Portable, evidence-aware reports without external services or dependencies.

Attempt rates describe the observed attempts; repeated trials are deliberately
not promoted to independent cases or confidence intervals. Full agent inputs,
outputs, and trajectories are not copied into this report.
"""

import csv
import html
import io
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path


STATUSES = ("pass", "fail", "unknown", "infra_error")


def _number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


def _items(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _percentile(values, quantile):
    """Linear interpolation over all observed attempts, including failures."""
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _latency(rows, field, source):
    values = [r[field] for r in rows if _number(r.get(field))]
    return {"source": source, "unit": "ms", "known_count": len(values),
            "coverage": len(values) / len(rows) if rows else 0,
            "p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95)}


def _case_results(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["version"], row["case_id"])].append(row)
    result = []
    for (version, case_id), attempts in sorted(groups.items()):
        statuses = [r["status"] for r in attempts]
        incomplete = any(status not in ("pass", "fail") for status in statuses)
        result.append({
            "version": version, "case_id": case_id,
            "business": attempts[0]["business"], "family_id": attempts[0]["family_id"],
            "trial_count": len(attempts), "counts": dict(Counter(statuses)),
            "any_pass": True if "pass" in statuses else None if incomplete else False,
            "all_pass": False if "fail" in statuses else None if incomplete else True,
        })
    return result


def _case_rates(cases, field):
    passed = sum(case[field] is True for case in cases)
    failed = sum(case[field] is False for case in cases)
    return {"pass_cases": passed, "fail_cases": failed,
            "unknown_cases": len(cases) - passed - failed,
            "rate_among_known": _ratio(passed, passed + failed),
            "rate_among_all": _ratio(passed, len(cases))}


def _dataset_coverage(rows, expected):
    observed = len({row["case_id"] for row in rows})
    consistent = expected is not None and observed <= expected
    return {"expected_cases": expected, "observed_cases": observed,
            "missing_cases": expected - observed if consistent else None,
            "coverage": observed / expected if consistent and expected else None,
            "note": ("expected dataset size unavailable" if expected is None else
                     "observed case count exceeds manifest dataset size" if not consistent else
                     "case coverage for this version; repeated trials do not increase coverage")}


def _aggregate(rows):
    counts = {status: sum(r["status"] == status for r in rows) for status in STATUSES}
    total = len(rows)
    adjudicated = counts["pass"] + counts["fail"]
    costs = [r["cost"] for r in rows if _number(r.get("cost"))]
    complete_cost = bool(rows) and len(costs) == total
    observed_cost = sum(costs) if costs else None
    overflow_cost = observed_cost is not None and not _number(observed_cost)
    if overflow_cost:
        observed_cost = None
    cases = _case_results(rows)
    provenance = Counter(json.dumps(r["metric_provenance"], ensure_ascii=False,
                                    sort_keys=True, allow_nan=False) for r in rows)
    return {
        "total": total, "trial_count": total,
        "unique_cases": len({r["case_id"] for r in rows}),
        "unique_families": len({(r["business"], r["family_id"]) for r in rows}),
        "case_version_pairs": len(cases), "counts": counts, "adjudicated": adjudicated,
        "success_rate_among_adjudicated": _ratio(counts["pass"], adjudicated),
        "success_rate_among_all": _ratio(counts["pass"], total),
        "evidence_coverage": adjudicated / total if total else 0,
        "hard_violation_attempts": sum(bool(r["violations"]) for r in rows),
        "case_any_pass": _case_rates(cases, "any_pass"),
        "case_all_pass": _case_rates(cases, "all_pass"),
        "cost": {"source": "reported", "known_count": len(costs),
                 "coverage": len(costs) / total if total else 0,
                 "observed_total": observed_cost,
                 "total": observed_cost if complete_cost else None,
                 "aggregation_error": "numeric_overflow" if overflow_cost else None,
                 "cost_per_success": (observed_cost / counts["pass"]
                                      if complete_cost and counts["pass"] and observed_cost is not None else None)},
        "latency_ms": {
            "execution": _latency(rows, "execution_latency_ms", "execution_wall_time"),
            "reported": _latency(rows, "latency_ms", "reported"),
        },
        "metric_provenance": [{"value": json.loads(key), "attempts": count}
                              for key, count in sorted(provenance.items())],
    }


def build_report(manifest: dict, records: list[dict], gate: dict | None = None) -> dict:
    """Build JSON-safe metrics and minimal rows from runner records.

    The optional release gate is preserved as supplied by the experiment runner.
    No inference or release decision is manufactured by the report renderer.
    """
    rows = []
    metadata = {}
    identities = set()
    for record in records:
        grade, episode = record["grade"], record.get("episode", {})
        status = grade.get("status", "unknown")
        if status not in STATUSES:
            raise ValueError("unsupported grade status: " + str(status))
        version, case_id = str(record["version"]), str(record["case_id"])
        business = str(grade.get("business", "unspecified"))
        family_id = str(grade.get("family_id", case_id))
        identity = (version, case_id, record["trial"])
        if identity in identities:
            raise ValueError("duplicate version/case/trial record")
        identities.add(identity)
        if case_id in metadata and metadata[case_id] != (business, family_id):
            raise ValueError("inconsistent case business or family metadata")
        metadata[case_id] = (business, family_id)
        rows.append({
            "version": version, "case_id": case_id, "business": business,
            "family_id": family_id, "trial": record["trial"], "seed": record.get("seed"),
            "status": status, "violations": _items(grade.get("violations")),
            "evidence": _items(grade.get("evidence")),
            "cost": grade.get("cost") if _number(grade.get("cost")) else None,
            "latency_ms": grade.get("latency_ms") if _number(grade.get("latency_ms")) else None,
            "execution_latency_ms": (episode.get("execution_latency_ms")
                                     if _number(episode.get("execution_latency_ms")) else None),
            "metric_provenance": episode.get("metric_provenance", "unspecified"),
        })
    expected_cases = manifest.get("dataset_case_count", manifest.get("case_count"))
    if type(expected_cases) is not int or expected_cases < 0:
        expected_cases = None
    configured_versions = manifest.get("versions", [])
    configured_versions = (configured_versions if isinstance(configured_versions, list) else [])
    version_names = {r["version"] for r in rows} | {
        name for name in configured_versions if isinstance(name, str) and name}
    versions = {}
    for version in sorted(version_names):
        subset = [r for r in rows if r["version"] == version]
        versions[version] = {"summary": _aggregate(subset), "business": {
            business: _aggregate([r for r in subset if r["business"] == business])
            for business in sorted({r["business"] for r in subset})}}
        versions[version]["summary"]["dataset_coverage"] = _dataset_coverage(subset, expected_cases)
    coverage_by_version = {version: data["summary"]["dataset_coverage"]
                           for version, data in versions.items()}
    coverages = [item["coverage"] for item in coverage_by_version.values()]
    dataset_coverage = {"expected_cases": expected_cases, "versions": coverage_by_version,
                        "minimum_version_coverage": (min(coverages) if coverages and
                            all(value is not None for value in coverages) else None),
                        "scope": "Each version covers the dataset separately; counts are never pooled across versions."}
    violation_rows = defaultdict(list)
    for row in rows:
        for violation in set(_text(v) for v in row["violations"]):
            violation_rows[violation].append(row)
    violations = [{"violation": name, "attempts": len(items),
                   "unique_cases": len({r["case_id"] for r in items}),
                   "versions": sorted({r["version"] for r in items})}
                  for name, items in sorted(violation_rows.items())]
    report = {
        "schema_version": "agent-eval-report-v1", "manifest": manifest,
        "gate": gate, "summary": _aggregate(rows), "versions": versions,
        "dataset_coverage": dataset_coverage,
        "case_results": _case_results(rows), "critical_violations": violations, "rows": rows,
        "metric_definitions": {
            "success_rate_among_adjudicated": "pass / (pass + fail)",
            "success_rate_among_all": "pass / all supplied attempts, including unknown and infra_error; absent dataset cases are reflected separately in dataset_coverage",
            "evidence_coverage": "(pass + fail) / all attempts",
            "trial_count": "observed attempts; repeated trials are not independent cases",
            "case_any_pass": "True when any trial passes; null when unresolved and no known pass",
            "case_all_pass": "False when any trial fails; otherwise null when unresolved, or True when every trial passes",
            "cost_per_success": "total cost of all attempts / passed attempts; null unless cost coverage is 100%",
            "latency_ms": "p50/p95 use all attempts with observed values, separated by execution wall time and reported latency",
            "scope": "descriptive sample metrics; no confidence interval or production quality claim",
        },
    }
    # Snapshot the report and reject invalid JSON (NaN, infinity, custom objects).
    return json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))


def _csv_cell(value):
    text = "" if value is None else _text(value)
    # Quoting CSV fields alone does not stop spreadsheet formula execution.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _csv_report(report):
    output = io.StringIO(newline="")
    fields = ["version", "case_id", "business", "family_id", "trial", "seed", "status",
              "violations", "evidence", "cost", "latency_ms", "execution_latency_ms", "metric_provenance"]
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in report["rows"]:
        writer.writerow([_csv_cell(row.get(field)) for field in fields])
    return output.getvalue()


def _escape(value):
    return html.escape(_text(value), quote=True)


def _percent(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def _decimal(value):
    return "—" if value is None else f"{value:.4g}"


def _metric_row(version, business, metrics):
    counts = metrics["counts"]
    coverage = metrics.get("dataset_coverage", {})
    values = [version, business, metrics["unique_cases"], metrics["unique_families"],
              metrics["trial_count"], counts["pass"], counts["fail"], counts["unknown"],
              counts["infra_error"], _percent(metrics["success_rate_among_adjudicated"]),
              _percent(metrics["success_rate_among_all"]), _percent(metrics["evidence_coverage"]),
              _percent(coverage.get("coverage")),
              coverage.get("missing_cases") if coverage.get("missing_cases") is not None else "—"]
    return "<tr>" + "".join("<td>" + _escape(value) + "</td>" for value in values) + "</tr>"


def _html_report(report, paths):
    summary = report["summary"]
    run_id = report["manifest"].get("run_id", "unspecified")
    gate = report.get("gate")
    gate_state = gate.get("decision", "未提供") if gate else "未提供"
    gate_content = json.dumps(gate, ensure_ascii=False, indent=2, allow_nan=False) if gate else "本次运行未提供发布判定。"
    coverage_cards = []
    for version, coverage in report["dataset_coverage"]["versions"].items():
        expected = coverage["expected_cases"]
        fraction = (str(coverage["observed_cases"]) + "/" + str(expected)
                    if expected is not None else str(coverage["observed_cases"]) + "/未知")
        coverage_cards.append("<p><b>" + _escape(version) + "</b>：" + _percent(coverage["coverage"])
                              + "（" + fraction + " 用例；缺失 "
                              + (str(coverage["missing_cases"]) if coverage["missing_cases"] is not None else "未知") + "）</p>")
    coverage_summary = "".join(coverage_cards) or "<p>未知：未提供版本或预期用例数。</p>"
    minimum_coverage = report["dataset_coverage"]["minimum_version_coverage"]
    dataset_coverage_text = "未知" if minimum_coverage is None else _percent(minimum_coverage)
    options = lambda items: "".join('<option value="' + _escape(item) + '">' + _escape(item) + "</option>" for item in items)
    table_rows = []
    efficiency_rows = []
    for version, data in report["versions"].items():
        table_rows.append(_metric_row(version, "全部业务", data["summary"]))
        for business, metrics in data["business"].items():
            table_rows.append(_metric_row(version, business, metrics))
        metrics = data["summary"]
        cost, latency = metrics["cost"], metrics["latency_ms"]
        values = [version, _percent(cost["coverage"]), _decimal(cost["observed_total"]),
                  _decimal(cost["total"]), _decimal(cost["cost_per_success"]),
                  _percent(latency["execution"]["coverage"]), _decimal(latency["execution"]["p50"]),
                  _decimal(latency["execution"]["p95"]), _percent(latency["reported"]["coverage"]),
                  _decimal(latency["reported"]["p50"]), _decimal(latency["reported"]["p95"])]
        efficiency_rows.append("<tr>" + "".join("<td>" + _escape(v) + "</td>" for v in values) + "</tr>")
    attempt_rows = []
    for row in report["rows"]:
        violations = "; ".join(_text(value) for value in row["violations"]) or "—"
        evidence = json.dumps(row["evidence"], ensure_ascii=False, indent=2, allow_nan=False)
        search = " ".join([row["case_id"], row["family_id"], row["version"], row["business"], violations, evidence])
        attrs = ' data-version="' + _escape(row["version"]) + '" data-business="' + _escape(row["business"]) + '" data-status="' + _escape(row["status"]) + '" data-search="' + _escape(search.lower()) + '"'
        values = [row["case_id"], row["version"], row["business"], row["trial"], row["seed"], row["status"], violations]
        attempt_rows.append("<tr" + attrs + ">" + "".join("<td>" + _escape(v) + "</td>" for v in values)
                            + "<td><details><summary>查看证据</summary><pre>" + html.escape(evidence)
                            + "</pre></details></td></tr>")
    case_rows = []
    for case in report["case_results"]:
        state = lambda value: "未知" if value is None else "是" if value else "否"
        values = [case["case_id"], case["version"], case["business"], case["family_id"], case["trial_count"],
                  state(case["any_pass"]), state(case["all_pass"])]
        case_rows.append("<tr>" + "".join("<td>" + _escape(v) + "</td>" for v in values) + "</tr>")
    critical_rows = []
    for violation in report["critical_violations"]:
        values = [violation["violation"], violation["attempts"], violation["unique_cases"], ", ".join(violation["versions"])]
        critical_rows.append("<tr>" + "".join("<td>" + _escape(v) + "</td>" for v in values) + "</tr>")
    provenance = {version: data["summary"]["metric_provenance"] for version, data in report["versions"].items()}
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>Agent Eval · 本地评测报告</title>
<style>
:root{font:15px/1.55 system-ui,sans-serif;color:#172b4d;background:#f4f6fa}body{max-width:1440px;margin:auto;padding:28px}h1{margin:0;font-size:28px}h2{font-size:20px;margin:0 0 14px}p{margin:8px 0}section{background:white;border:1px solid #dce3ed;border-radius:10px;padding:20px;margin-top:20px}.muted{color:#53657c}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:18px}.card{padding:16px;border:1px solid #dce3ed;border-radius:8px;background:white}.card strong{display:block;font-size:25px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px;text-align:left;border-bottom:1px solid #e5eaf1;vertical-align:top;overflow-wrap:anywhere}th{background:#f3f6fa;white-space:nowrap}.scroll{overflow-x:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:900px;font-size:12px}input,select,button{font:inherit;padding:7px 10px;border:1px solid #b8c5d5;border-radius:6px;background:white;color:inherit}button{cursor:pointer}input{min-width:250px;flex:1}label{display:flex;align-items:center;gap:6px}.filters{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:12px}code{overflow-wrap:anywhere}.path{display:flex;gap:10px;align-items:center;flex-wrap:wrap}summary{cursor:pointer}tr[hidden]{display:none}.badge{font-weight:700;color:#174b87}.notice{background:#edf5ff;padding:12px;border-radius:6px}footer{padding:20px 0;color:#53657c;font-size:13px}
</style></head><body>
<h1>Agent Eval 本地评测报告</h1>
<p class="path">Run <code>""" + _escape(run_id) + """</code><button type="button" data-copy=""" + '"' + _escape(run_id) + '"' + """>复制 Run ID</button></p>
<p class="path muted">报告路径 <code>""" + _escape(paths["html"]) + """</code><button type="button" data-copy=""" + '"' + _escape(paths["html"]) + '"' + """>复制路径</button></p>
<p id="copy-status" class="muted" role="status"></p>
<div class="cards"><div class="card">尝试次数<strong>""" + str(summary["trial_count"]) + """</strong></div>
<div class="card">用例 ID（跨版本去重）<strong>""" + str(summary["unique_cases"]) + """</strong></div>
<div class="card">任务家族<strong>""" + str(summary["unique_families"]) + """</strong></div>
<div class="card">已提供 episode 证据覆盖<strong>""" + _percent(summary["evidence_coverage"]) + """</strong></div>
<div class="card">数据集覆盖（最低版本）<strong>""" + dataset_coverage_text + """</strong></div>
<div class="card">发布判定<strong class="badge">""" + _escape(gate_state) + """</strong></div></div>
<section><h2>数据集覆盖（按版本）</h2>""" + coverage_summary + """<p class="muted">每个版本分别计算已观察用例 / 数据集预期用例。重复 trial 不增加覆盖率；缺失用例不进入下方尝试分母。证据覆盖率只描述已提供 episode，完整数据集结论须同时检查这里的覆盖率。</p></section>
<section><h2>指标口径</h2><p>通过率（已判定） = pass / (pass + fail)；通过率（全部） = pass / 全部尝试；证据覆盖率 = (pass + fail) / 全部尝试。</p>
<p class="muted">unknown 与 infra_error 单列。重复 trial 不增加独立用例或任务家族数量；本报告不为重复尝试生成置信区间，也不将样本指标等同于生产质量。</p>
<p class="notice">报告只包含评分证据与指标，不包含完整 Agent 输入、输出或轨迹。证据摘录仍可能含业务数据，请按本地数据管理要求保存。</p></section>
<section><h2>版本与业务汇总</h2><div class="scroll"><table><thead><tr><th>版本</th><th>业务</th><th>用例</th><th>家族</th><th>尝试</th><th>pass</th><th>fail</th><th>unknown</th><th>infra_error</th><th>通过率（已判定）</th><th>通过率（全部）</th><th>已提供 episode 证据覆盖</th><th>数据集覆盖</th><th>缺失用例</th></tr></thead><tbody>""" + "".join(table_rows) + """</tbody></table></div></section>
<section><h2>效率与数据来源</h2><p class="muted">统计所有尝试，包括失败。费用来源为 Agent 报告值，单位沿用适配器配置；模拟值与真实账单需结合下方来源核实。费用缺失时完整总成本与每次成功成本为空。墙钟耗时与 Agent 报告耗时分别统计，单位 ms。</p>
<div class="scroll"><table><thead><tr><th>版本</th><th>费用覆盖</th><th>已知费用合计</th><th>完整总费用</th><th>每次成功费用</th><th>墙钟覆盖</th><th>墙钟 p50</th><th>墙钟 p95</th><th>报告耗时覆盖</th><th>报告 p50</th><th>报告 p95</th></tr></thead><tbody>""" + "".join(efficiency_rows) + """</tbody></table></div>
<details><summary>查看指标来源</summary><pre>""" + html.escape(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False)) + """</pre></details></section>
<section><h2>强约束违规</h2><p class="muted">所有评分器报告的强约束违规独立列出，不通过平均分抵消。</p><div class="scroll"><table><thead><tr><th>违规</th><th>尝试次数</th><th>用例数</th><th>版本</th></tr></thead><tbody>""" + ("".join(critical_rows) or '<tr><td colspan="4">未观察到强约束违规</td></tr>') + """</tbody></table></div></section>
<section><h2>尝试与证据</h2><div class="filters"><label>版本 <select id="filter-version"><option value="">全部</option>""" + options(sorted(report["versions"])) + """</select></label>
<label>业务 <select id="filter-business"><option value="">全部</option>""" + options(sorted({r["business"] for r in report["rows"]})) + """</select></label>
<label>状态 <select id="filter-status"><option value="">全部</option>""" + options(STATUSES) + """</select></label>
<input id="filter-search" type="search" aria-label="搜索用例、违规或证据" placeholder="搜索用例、违规或证据"><button type="button" id="filter-reset">重置</button></div>
<p id="filter-count" class="muted" aria-live="polite">""" + str(len(report["rows"])) + """ 条尝试；筛选仅作用于下表，汇总指标保留完整运行口径。</p>
<div class="scroll"><table><thead><tr><th>用例</th><th>版本</th><th>业务</th><th>Trial</th><th>Seed</th><th>状态</th><th>违规</th><th>证据</th></tr></thead><tbody id="attempts">""" + "".join(attempt_rows) + """</tbody></table></div></section>
<section><h2>重复试验的用例结果</h2><p class="muted">any_pass：至少一次已知通过则为是，否则有未判定尝试时为未知。all_pass：至少一次已知失败则为否；没有已知失败但存在 unknown / infra_error 时为未知；每次尝试均通过时为是。</p>
<div class="scroll"><table><thead><tr><th>用例</th><th>版本</th><th>业务</th><th>家族</th><th>尝试</th><th>any_pass</th><th>all_pass</th></tr></thead><tbody>""" + "".join(case_rows) + """</tbody></table></div></section>
<section><h2>发布判定与运行元数据</h2><details><summary>查看发布判定证据</summary><pre>""" + html.escape(gate_content) + """</pre></details>
<details><summary>查看运行元数据</summary><pre>""" + html.escape(json.dumps(report["manifest"], ensure_ascii=False, indent=2, allow_nan=False)) + """</pre></details></section>
<footer>独立 HTML · 无网络请求 · 原始结构化结果见同目录 report.json 与 results.csv</footer>
<script>
const controls = ['version','business','status'].map(name => [name, document.getElementById('filter-' + name)]);
const search = document.getElementById('filter-search');
const attempts = Array.from(document.querySelectorAll('#attempts tr'));
function filterRows() {
  const query = search.value.trim().toLowerCase(); let count = 0;
  for (const row of attempts) {
    const match = controls.every(([name, control]) => !control.value || row.dataset[name] === control.value)
      && (!query || row.dataset.search.includes(query));
    row.hidden = !match; if (match) count++;
  }
  document.getElementById('filter-count').textContent = count + ' / ' + attempts.length + ' 条尝试；筛选仅作用于下表，汇总指标保留完整运行口径。';
}
controls.forEach(([, control]) => control.addEventListener('change', filterRows));
search.addEventListener('input', filterRows);
document.getElementById('filter-reset').addEventListener('click', () => { controls.forEach(([, control]) => {control.value='';}); search.value=''; filterRows(); });
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  let copied = false;
  try { if (navigator.clipboard) { await navigator.clipboard.writeText(button.dataset.copy); copied = true; } } catch (_) {}
  if (!copied) { const area = document.createElement('textarea'); area.value = button.dataset.copy;
    document.body.appendChild(area); area.select(); try {copied = document.execCommand('copy');} catch (_) {} area.remove(); }
  document.getElementById('copy-status').textContent = copied ? '已复制' : '浏览器未允许复制，可直接选择上方文字复制。';
}));
</script></body></html>"""


def write_report(report: dict, out_dir: Path) -> dict:
    """Write each artifact atomically; serialization completes before mutation."""
    directory = Path(out_dir).resolve()
    paths = {"json": str(directory / "report.json"), "csv": str(directory / "results.csv"),
             "html": str(directory / "report.html")}
    content = {
        "json": json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        "csv": _csv_report(report), "html": _html_report(report, paths),
    }
    directory.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for kind, text in content.items():
            fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=directory)
            staged.append((temporary, paths[kind]))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return paths
