# 本地使用指南

本指南覆盖安装、接入自有 Agent、导入真实数据、查看指标，以及线上失败回流。框架运行于本地 Python 3.10+，运行时只依赖标准库。真实 Agent 和真实业务标注由你在本地接入；内置 Agent 用于验证框架链路。

## 1. 安装并跑通首个项目

以下命令使用 macOS/Linux 的 Bash。Windows 可在虚拟环境的 `Scripts` 目录激活 Python 环境，随后使用相同的 `agent-eval` 命令。

```bash
git clone https://github.com/BruceLiu-168/agent-eval.git
cd agent-eval
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
agent-eval --version
agent-eval self-test
agent-eval init ../my-eval --template python
cd ../my-eval
agent-eval validate --config eval.json
agent-eval run --config eval.json --out runs/first
```

`init` 需要空目录，生成 `eval.json`、`data/cases.jsonl`、`custom_adapter.py`、`agent_command.py` 和本地忽略规则。Python 模板默认调用独立的脚本测试 Agent，覆盖退款与权限申请，包含超时、服务不可用和越权等场景。合成故障产生 `infra_error`，少量家族使门禁保持 `INCONCLUSIVE`，都是可见的评测结果。

每次运行生成：

| 文件 | 内容 |
|---|---|
| `runs/first/manifest.json` | Run ID、数据集和实现指纹、版本、实验来源 |
| `runs/first/records.jsonl` | 每次尝试的原始 Episode、完整 grade、trial、seed 和校验摘要 |
| `runs/first/report.json` | 可编程读取的汇总、业务切片、证据和门禁 |
| `runs/first/results.csv` | 每次尝试的指标与评分证据 |
| `runs/first/report.html` | 可搜索、筛选的独立本地报告 |

报告随 `run`/`evaluate` 自动生成，没有单独的 `report` 子命令。可直接双击 HTML，或运行：

```bash
python - <<'PY'
from pathlib import Path
import webbrowser
webbrowser.open(Path('runs/first/report.html').resolve().as_uri())
PY
```

HTML 无外部网络依赖，不包含完整输入/输出/轨迹；评分证据摘录仍可能包含业务内容。完整原始数据保存在本地 `records.jsonl`。生成的项目默认忽略 `data/`、`runs/`、`.env` 和 SQLite 文件，避免将真实数据随代码提交。

## 2. 实验配置、并发和恢复

```json
{
  "schema_version": 1,
  "dataset": "data/cases.jsonl",
  "adapter": {"kind":"python","callable":"custom_adapter:run_case"},
  "versions": ["baseline","candidate"],
  "execution": {"trials":3,"seed":42,"timeout_seconds":30,"workers":2},
  "grader": {"kind":"deterministic","timeout":30},
  "gate": {
    "enabled":true,"baseline":"baseline","candidate":"candidate",
    "min_families":30,"margin":0.05,"alpha":0.05,"min_success_rate":0.8
  },
  "provenance": {
    "data_source":"reviewed_local_cases",
    "model":"pin-your-model-version",
    "prompt_version":"support-prompt-v1",
    "tools_version":"business-tools-v1",
    "knowledge_version":"kb-v1",
    "environment":"local-test-snapshot"
  }
}
```

`dataset` 相对配置文件目录解析；Python 模块和命令行工作目录同样是配置所在目录。`--out` 相对当前终端目录解析。模型、提示词、知识库及远端服务版本由接入方固定，并放进 `provenance`，以便解释真实指标变化。

`trials` 为每个用例/版本的重复次数，trial 从 0 开始，seed 为配置 seed 加 trial。`workers` 为并发任务数（1–64）。`execution.timeout_seconds` 控制一次 Agent 调用总时长；`grader.timeout` 控制一次评分总时长，正数、默认 30 秒。没有自动重试。

中断后使用同一配置、代码、数据和输出目录恢复：

```bash
agent-eval run --config eval.json --out runs/first --resume
```

已落盘记录会复用，包括已保存的 `unknown`、`infra_error`；尚未落盘的调用可能再次执行。涉及真实副作用时，使用测试快照/业务沙箱和幂等请求 ID。修改配置、数据或已纳入指纹的实现后，需要新的 `--out`，不能混入旧运行。

恢复时会校验 checkpoint 内容；损坏记录需要先查明原因。对于已经标记 completed 的运行，checkpoint 丢失或被截断时会拒绝恢复，避免悄悄补跑并改写已完成实验。运行目录的锁防止两个进程同时写入。若进程异常退出遗留 `.agent-eval.lock`，确认原进程已结束后再移除锁；正常中断会自动清理。

## 3. 三种 Agent 接入

### Python

生成的 `custom_adapter.py` 已可运行。将其中的 `run_case(case, version, seed)` 改为调用你的 Agent，返回 [Episode 契约](DATA_CONTRACT.md#episode一行一次执行)。版本名称应确实选择对应模型、提示词或实现，不能只改结果里的标签。

```python
# custom_adapter.py：当前可运行的测试 Agent 入口
from agent_eval.demo_agent import run


def run_case(case, version, seed):
    # 替换此调用以接入你自己的 SDK/工作流。
    # 如果 Agent 会修改业务状态，执行后由此适配器独立查询后端，
    # 再把查询结果写进 Episode.final_state。
    return run(case, version, seed)
```

Python 入口是同步函数。使用 async SDK 时，在适配器中完成协程执行再返回 JSON 可序列化对象。Adapter 只收到公开 Case 字段；它不应读取完整评测数据集来构造答案。

例如退款业务的接入顺序应为：恢复测试订单状态 → 调用 Agent → 采集实际工具轨迹 → 查询订单/账本 → 返回 Episode。`output` 记录 Agent 的回答；`final_state` 记录独立查询结果。只做问答、分类、抽取的 Agent 可使用输出型用例，不必人为制造业务状态。

### HTTP

使用第二个终端激活同一虚拟环境并启动测试 Agent：

```bash
agent-eval serve-agent --port 8765
```

在项目终端创建并执行 HTTP 项目：

```bash
agent-eval init ../eval-http --template http
agent-eval run --config ../eval-http/eval.json --out ../eval-http/runs/first
```

真实服务的配置示例：

```json
{
  "kind":"http",
  "url":"https://your-agent.example/run",
  "api_key_env":"AGENT_EVAL_AGENT_KEY",
  "headers":{"X-Evaluation-Environment":"staging"}
}
```

服务接收 POST JSON `{"case":{...},"version":"candidate","seed":0}`，返回 HTTP 200 和 Episode JSON 对象本身。密钥通过 `api_key_env` 引用，框架发送 `Authorization: Bearer ...`。不要将密钥写进 URL、headers 或配置文件；`.env` 不会自动加载，需要由终端或环境管理工具导出变量。

```bash
read -r -s -p 'Agent API key: ' AGENT_EVAL_AGENT_KEY
export AGENT_EVAL_AGENT_KEY
```

内置 HTTP 服务只绑定本机 `127.0.0.1`，用于联调。真实服务可以运行任何 Agent 框架，只需在入口与出口适配上述协议。

### 命令行

```bash
agent-eval init ../eval-command --template command
agent-eval run --config ../eval-command/eval.json --out ../eval-command/runs/first
```

模板配置为 `{"kind":"command","argv":["/创建项目时的Python绝对路径","agent_command.py"]}`，其中第一个参数由创建项目的 `sys.executable` 自动写入。迁移到另一台机器或更换虚拟环境后，将该路径调整为目标环境的解释器。程序从 stdin 读取与 HTTP 相同的请求，stdout 只输出一个 Episode JSON，日志写到 stderr。`argv` 是参数数组，框架不执行 shell，不展开 `~`、`$VAR`、重定向或管道。当前适配器不将 stderr 写入评测报告；调试时可单独运行自有命令查看日志。

若 baseline/candidate 对应不同部署，可用 `version_adapters` 覆盖：

```json
{
  "baseline":{"kind":"http","url":"http://127.0.0.1:8765/run"},
  "candidate":{"kind":"python","callable":"custom_adapter:run_case"}
}
```

将该对象放到实验配置的 `version_adapters` 字段；其中的键必须在 `versions` 中。

## 4. 放入自有数据

每行一个 Case，至少提供状态、输出、规则或 rubric 中的一种验收标准。完整字段和规则说明见 [数据契约](DATA_CONTRACT.md)。下面先创建一个能用内置测试 Agent 验证的输出型项目配置；随后按相同结构换成你本地审核过的真实业务请求与预期。

在 `my-eval` 目录运行：

```bash
cat > data/routing.jsonl <<'JSONL'
{"case_id":"billing-001","family_id":"billing-routing","business":"support_routing","input":{"text":"我的订单需要退款","customer_tier":"normal"},"expected_output":{"queue":"billing","priority":"normal"}}
{"case_id":"security-001","family_id":"security-routing","business":"support_routing","input":{"text":"账号被盗号了","customer_tier":"vip"},"checks":[{"id":"queue","kind":"equals","path":"output.queue","expected":"security"},{"id":"priority","kind":"equals","path":"output.priority","expected":"high"}]}
JSONL
python - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('eval.json').read_text())
config['dataset'] = 'data/routing.jsonl'
config['execution']['trials'] = 2
config['provenance'] = {'data_source':'synthetic_routing_examples','agent_kind':'scripted_demo'}
Path('eval-routing.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
agent-eval validate --config eval-routing.json
agent-eval run --config eval-routing.json --out runs/routing
```

把真实数据的成本、耗时和轨迹接上之后，才会产生这些维度的真实指标。不要把未知成本填成 0。需要验证状态副作用时，给 Case 增加 `expected_state` 并由适配器独立观测 `final_state`；新增任一验收标准都必须通过，不能由另一项高分抵消。

`metadata` 可保存 `source`、标注版本、审核来源和数据划分，始终属于评测侧。生产回流、调试集与保留测试集应有清晰版本；同一问题的轻微改写与重复 trial 不应被标成独立 family。

## 5. 本地自定义评分与 LLM 评委

自定义函数可以读取私有 Case 和 Episode，追加业务验证。示例在 `my-eval` 创建 `custom_grader.py`：

```bash
cat > custom_grader.py <<'PY'
def grade(case, episode):
    output = episode.get('output')
    if not isinstance(output, dict) or 'queue' not in output:
        return [{'id':'known-queue','status':'unknown','evidence':'缺少可观察的路由结果。'}]
    passed = output['queue'] in {'billing', 'security', 'technical', 'general'}
    return [{'id':'known-queue','status':'pass' if passed else 'fail',
             'evidence':'已核对路由结果是否属于业务允许的队列。'}]
PY
cat > grader-python.json <<'JSON'
{"kind":"python","callable":"custom_grader:grade","version":"queue-grader-v1","timeout":30}
JSON
python - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('eval-routing.json').read_text())
config['grader'] = json.loads(Path('grader-python.json').read_text())
Path('eval-custom.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
agent-eval run --config eval-custom.json --out runs/custom
```

自定义评分器失败或超时会留下评分 `unknown` 与证据，Agent 本身不会因此变成业务失败。若已有确定性强约束违规或独立状态失败，回退评分仍保留 `fail`，不会被评委超时覆盖。评分器仍需显式 Case 验收标准；它的结果是追加约束。

需要 LLM 评判的用例增加 rubric，例如：

```json
{"case_id":"grounded-001","business":"support","input":"我的退款到账了吗？","rubric":[{"id":"grounded","description":"答复不得声称完成观测证据未支持的业务操作。"}]}
```

创建独立评委配置，替换为你有权限使用的真实兼容端点和模型：

```bash
cat > grader-llm.json <<'JSON'
{"kind":"llm","base_url":"https://your-model-gateway.example/v1","model":"your-judge-model","api_key_env":"AGENT_EVAL_JUDGE_KEY","timeout":30,"version":"grounded-judge-v1"}
JSON
read -r -s -p 'Judge API key: ' AGENT_EVAL_JUDGE_KEY
export AGENT_EVAL_JUDGE_KEY
```

将该 JSON 对象放入实验的 `grader` 即用于 `run`；也可通过下一节的 `evaluate --grader grader-llm.json` 重评已有执行。只有带 rubric 的用例才需要评委请求。此时输入、输出、轨迹及状态会发送到所配置服务；不填写此配置时没有默认模型调用。

没有配置评委的 rubric 为 `unknown`，不会被自动跳过。评委版本、模型和配置摘要记录在评分元数据中；模型输出必须为严格布尔/空值和逐项证据。人类标注校准可用：

```bash
cat > data/judge-labels.jsonl <<'JSONL'
{"human_failure":true,"judge_failure":true}
{"human_failure":true,"judge_failure":null}
{"human_failure":false,"judge_failure":false}
JSONL
agent-eval calibrate --labels data/judge-labels.jsonl
```

这里 `true` 表示严重失败，`null` 表示评委弃权。示例用于检查校准命令，实际校准需要独立人工标注样本。评委调用费用没有自动计入 Agent 的 Episode 成本。

## 6. 只评估已有真实 Episode

已经在业务系统执行过 Agent 时，直接导出 Episode JSONL，评分过程不再次运行 Agent：

```bash
cat > data/recorded-episodes.jsonl <<'JSONL'
{"episode_id":"candidate:billing-001:0","case_id":"billing-001","agent_version":"candidate","status":"completed","events":[],"output":{"queue":"billing","priority":"normal"},"cost":null,"latency_ms":1250}
{"episode_id":"candidate:security-001:0","case_id":"security-001","agent_version":"candidate","status":"completed","events":[],"output":{"queue":"security","priority":"high"},"cost":null,"latency_ms":1400}
JSONL
agent-eval evaluate --dataset data/routing.jsonl --episodes data/recorded-episodes.jsonl --out runs/recorded
agent-eval evaluate --dataset data/routing.jsonl --episodes data/recorded-episodes.jsonl --grader grader-python.json --out runs/recorded-custom
```

`--grader` 是单独的 grader JSON 对象，不是整份 `eval.json`。自定义评分模块从该文件所在目录解析；未提供 `--grader` 时使用确定性评分。

还可以直接重评分已有运行的 `records.jsonl`：

```bash
agent-eval evaluate --dataset data/routing.jsonl --episodes runs/routing/records.jsonl --out runs/rescored
```

导入多次重复执行时，使用 `{"trial":1,"seed":43,"episode":{...}}` 包装。唯一性按 `agent_version + case_id + trial` 校验；原始 Episode 默认 trial=0。未提供 Episode 的数据集用例在按版本覆盖率中显示为缺失，不会被标为通过。

`evaluate` 生成描述性报告，门禁为未配置，不推断发布 PASS。导入数据格式错误会明确终止导入，避免将无效数据当成 Agent 业务失败。

## 7. 线上采集 → 延迟标注 → 回归集

以下示例在 `my-eval` 中跑通一个分类失败的闭环。时间显式使用 `+08:00`；真实接入请使用实际观测和标签成熟时间。

```bash
cat > data/online.jsonl <<'JSONL'
{"episode":{"episode_id":"prod-security-001","case_id":"security-online-001","agent_version":"baseline","status":"completed","events":[],"output":{"queue":"general","priority":"high"},"cost":null,"latency_ms":1800},"observed_at":"2026-10-05T08:00:00+08:00","outcome":{"mature_at":"2026-10-05T09:00:00+08:00","success":null,"source":"business-audit-pending"},"risk_flags":["routing_complaint"],"review":{"verified":false,"reviewer":""},"replay_case":{"case_id":"security-online-001","family_id":"security-routing","business":"support_routing","input":{"text":"我的账号被盗号了","customer_tier":"vip"},"expected_output":{"queue":"security","priority":"high"},"metadata":{"source":"reviewed_online_failure"}}}
JSONL
agent-eval online --db data/online.sqlite ingest data/online.jsonl --random-rate 0.1
agent-eval online --db data/online.sqlite report --as-of '2026-10-05T12:00:00+08:00'
```

随机抽样与风险发现是两条可能重叠的流。该例携带风险标记，即使未被随机抽中，审核后也能回流；它不能代表随机生产样本的成功率。

取得业务结果并完成审核后，创建两个 JSON 文件：

```bash
cat > data/outcome.json <<'JSON'
{"mature_at":"2026-10-05T09:00:00+08:00","success":false,"source":"routing-business-audit"}
JSON
cat > data/review.json <<'JSON'
{"verified":true,"reviewer":"local-business-reviewer"}
JSON
agent-eval online --db data/online.sqlite annotate --episode-id prod-security-001 --outcome data/outcome.json --review data/review.json
agent-eval online --db data/online.sqlite report --as-of '2026-10-05T12:00:00+08:00'
agent-eval online --db data/online.sqlite promote data/regression.jsonl --as-of '2026-10-05T12:00:00+08:00'
agent-eval validate --dataset data/regression.jsonl
```

`annotate` 至少提供 `--outcome`、`--review` 中的一个，可分别更新且不改变首次抽样结果。同一 Episode 的原始 intake 重投保持幂等；更改同 ID 的内容应使用 annotate，不能用 ingest 偷换记录。

`promote` 输出数量、重复、跳过和拒绝原因；只纳入成熟、已审核、已抽样或触发风险发现的失败。它支持状态型、输出型、checks 和 rubric 用例，保留预期标准并做语义去重。同一 SQLite store 管理一个回归集目的地，避免多个库竞争修改同一文件。

重跑回流集：

```bash
python - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('eval.json').read_text())
config['dataset'] = 'data/regression.jsonl'
config['provenance'] = {'data_source':'reviewed_online_failure','agent_kind':'scripted_demo'}
Path('eval-regression.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
agent-eval run --config eval-regression.json --out runs/regression
```

这个例子的 candidate 使用内置修复策略，能检验回流后版本差异。真实项目将 adapter 指向自己的修复版本；复测通过后，由业务发布流程安排灰度，再继续采集真实业务结果。线上 report、annotate 和 promote 都不会自动批准发布。

线上 store 对敏感字段和邮箱做基础脱敏，邮箱使用每库稳定伪名；它不保证覆盖全部个人信息，也可能影响依赖邮箱解析的业务逻辑。真实输入应先完成适合业务的脱敏，并验证回放语义。

## 8. 看懂报告与用于 CI

先同时看四个状态、数据集覆盖率和证据覆盖率，再看通过率：

- `pass`：声明的必需标准全部通过；`fail`：已有证据确认失败或强约束违规。
- `unknown`：缺状态/输出、缺预算指标、未配置 rubric 评委或评分故障。
- `infra_error`：Agent 调用未完成或违反协议；已有越权轨迹仍可能优先判 `fail`。
- `cost:null`/缺失表示未知。若用例设置了 `max_cost`，成本缺失会使该预算不可判定。

报告分别给出已判定尝试通过率、所有已提供尝试通过率，以及判定证据覆盖率。导入 Episode 不齐时，各版本的数据集覆盖率会下降。重复 trial 可以说明稳定性，但不能增加独立任务家族数量。

门禁在启用后检验绝对质量下限、基线非劣性、业务切片和强约束。每个 Case 必须通过所有配置 trial；重复失败、未知或基础设施故障都会影响门禁。样本数不足或统计证据不足时保持 `INCONCLUSIVE`，不能仅靠降低 `min_families` 证明真实质量。

```bash
agent-eval run --config eval.json --out runs/ci --enforce-gate
agent-eval gate --report runs/first/report.json
```

普通 `run` 成功执行并写出报告时返回 0，门禁结果仍需查看。`--enforce-gate` 或独立 `gate` 命令将 `PASS` 映射为退出码 0、`BLOCK` 为 2、`INCONCLUSIVE` 为 3；没有配置门禁也返回 3。配置或输入错误返回非零。门禁只产出评测决策，不执行部署。
