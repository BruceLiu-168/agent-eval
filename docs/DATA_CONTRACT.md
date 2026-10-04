# 数据与评分契约

框架使用 UTF-8 JSON/JSONL。一个 Case 描述任务及独立验收标准；一个 Episode 记录某个 Agent 版本的一次实际执行。Case 属于评测侧，传给 Agent 的请求只包含明确列出的公开字段。

## Case：一行一个可验收任务

最小的输出型用例：

```json
{"case_id":"faq-refund","business":"support","input":"退款需要多久？","expected_output":"退款预计在3个工作日内到账。"}
```

| 字段 | 要求 | 用途 |
|---|---|---|
| `case_id` | 必需，非空字符串，数据集内唯一 | 与 Episode 配对 |
| `business` | 必需，非空字符串 | 业务切片，如 `refund`、`support_routing` |
| `input` | 必需，任意 JSON 值 | 原始任务，可为字符串或结构化请求 |
| `family_id` | 可选，默认 `case_id` | 相关用例的任务家族，须在实验前定义 |
| `initial_state` | 可选，对象 | 测试环境起始快照 |
| `expected_state` | 可选，非空对象 | 独立业务状态预期，按对象子集匹配 |
| `expected_output` | 可选，任意 JSON 值 | 完整输出等值校验；显式 `null` 是合法预期 |
| `checks` | 可选，数组 | 针对 `output` 或 `final_state` 的确定性校验 |
| `rubric` | 可选，数组 | 需要显式配置 LLM 评委的验收标准 |
| `metadata` | 可选，对象 | 标注来源、审核、数据划分等私有信息 |
| `forbidden_actions`、`required_actions` | 可选，字符串数组 | 禁止/必须出现的可观察操作 |
| `required_order`、`required_success_before` | 可选，二元字符串数组的数组 | 顺序和成功前置条件 |
| `limits` | 可选，对象 | 步数、成本和耗时预算 |

`expected_state`、`expected_output`、非空 `checks`、非空 `rubric` 至少提供一种。只有输入、只有操作约束、只有自定义评分器配置，都不能代替明确的用例验收标准。

JSON 对象键必须是字符串；拒绝 NaN、Infinity、非 JSON 对象、重复 JSON 字段、重复 Case ID 和空数据集。业务输入/输出可以包含负数；计量字段和资源预算必须有限且非负。

```bash
agent-eval validate --dataset data/cases.jsonl
agent-eval validate --config eval.json
```

JSONL 可以包含空行；错误指向物理行号及字段路径。标准校验会补齐 `family_id`，保留私有扩展字段，不修改传入对象。

## Agent 实际收到什么

Python 入口为 `run_case(case, version, seed)`；HTTP/命令行收到的请求为：

```json
{
  "case": {
    "case_id": "refund-001",
    "business": "refund",
    "input": {"request_id":"req-001","order_id":"order-001","amount":40.0},
    "initial_state": {"order":{"refunded":0.0}},
    "limits": {"max_steps":8,"max_cost":0.1,"max_latency_ms":5000}
  },
  "version": "candidate",
  "seed": 0
}
```

`case` 白名单固定为 `case_id`、`business`、`input`、`initial_state`、`limits`，其中可选字段可以缺失。预期答案、预期状态、校验规则、rubric、metadata、family_id 和任意扩展字段不会传给 Agent。

本地适配器运行在独立进程中，仍具有调用者的文件和环境权限。适配器代码应只读取公开请求，不自行读取评测数据集或预期答案文件；进程隔离负责生命周期和超时，不构成操作系统安全沙箱。

## 状态、输出与校验规则

状态型用例示例：

```json
{
  "case_id": "refund-001",
  "family_id": "refund-normal",
  "business": "refund",
  "input": {"request_id":"req-001","order_id":"order-001","amount":40.0},
  "initial_state": {"order":{"refunded":0.0,"refund_count":0}},
  "expected_state": {"order":{"refunded":40.0,"refund_count":1}},
  "required_actions": ["check_policy","commit_refund"],
  "required_order": [["check_policy","commit_refund"]],
  "required_success_before": [["check_policy","commit_refund"]],
  "forbidden_actions": ["bypass_policy"],
  "limits": {"max_steps":8,"max_cost":0.1,"max_latency_ms":5000},
  "metadata": {"source":"reviewed_business_snapshot","split":"test"}
}
```

`final_state` 应由适配器在执行后独立查询数据库、业务 API、审计日志或沙箱状态。Agent 回答“退款成功”只能作为 `output`，不能据此填充 `final_state.order.refunded`。状态暂未可查时，保留证据缺失；可通过后续业务标签补齐线上结果。

`expected_state` 允许实际对象存在额外字段；`expected_output` 要求完整 JSON 等值。比较区分布尔值与数字，也区分整数和浮点数，嵌套数组同样如此。请统一数据生成与观测端的类型。

规则型用例示例：

```json
{
  "case_id": "answer-grounded-001",
  "business": "knowledge_assistant",
  "input": "请回答并给出来源。",
  "checks": [
    {"id":"answer-present","kind":"contains","path":"output.answer","expected":"退款"},
    {"id":"reference","kind":"equals","path":"output.citations.0","expected":"policy-refund-v3"},
    {"id":"ticket","kind":"regex","path":"output.ticket_id","expected":"^T-[0-9]+$"},
    {"id":"state","kind":"json_subset","path":"final_state","expected":{"ticket":{"created":true}}}
  ]
}
```

| `kind` | `expected` | 判定 |
|---|---|---|
| `equals` | 任意 JSON 值 | 严格 JSON 等值 |
| `contains` | 任意 JSON 值 | 字符串子串、数组中的完整元素、对象中的字符串键 |
| `regex` | 合法正则表达式字符串 | 在实际字符串中执行搜索 |
| `json_subset` | 对象 | 递归匹配对象子集，数组按完整等值比较 |

`path` 必须以 `output` 或 `final_state` 开头，用点分隔对象键或数组索引。`output.items.0.id` 有效；当前没有 JSONPath 通配符或带点对象键的转义语法。路径不存在判为 `unknown`；路径存在但值不符合规则判为 `fail`。

`checks` 内的 `id` 必须唯一；`rubric` 内也必须唯一。最终明细使用 `check:<id>`、`rubric:<id>`、`plugin:<id>`、`budget:<name>` 作为命名空间，并保留原校验/criterion ID。

## Episode：一行一次执行

```json
{
  "episode_id": "candidate:refund-001:0",
  "case_id": "refund-001",
  "agent_version": "candidate",
  "status": "completed",
  "events": [
    {"action":"check_policy","args":{"order_id":"order-001"},"result":{"ok":true}},
    {"action":"commit_refund","args":{"request_id":"req-001"},"result":{"ok":true}}
  ],
  "output": {"answer":"已提交退款。"},
  "final_state": {"order":{"refunded":40.0,"refund_count":1}},
  "cost": 0.012,
  "latency_ms": 850,
  "metric_provenance": {"cost":"provider_usage","latency_ms":"measured"}
}
```

必需字段为非空字符串 `episode_id`、`case_id`、`agent_version`，`status`，以及 `events` 数组。每个 event 至少有非空 `action`；`args`、`result`、`agent_id` 等可按业务扩展。

- `status` 只接受 `completed` 或 `infra_error`。`completed` 表示执行结束，评分器另行判断任务是否成功。
- `output` 可为任意有限 JSON，也可缺失。缺失不同于显式 `null`：`expected_output:null` 只能由实际 `output:null` 满足。
- `final_state` 若提供，必须为对象；没有独立观测时应省略，不能填伪造成功状态。
- `cost`、`latency_ms` 可以缺失或为 `null`；已知值必须有限且非负。`0` 表示确实为零，不表示未知。
- 适配器可以保留业务扩展字段；若返回 `business` 或 `seed`，必须与本次请求一致。
- 框架执行时补充 `execution_latency_ms`，表示框架观测的单次调用墙钟耗时，包含进程/通信开销；与 Agent 提供的 `latency_ms` 分开统计。

HTTP 响应正文和命令行 stdout 都是 Episode 对象本身，不再包装 `{"episode":...}`。HTTP 要返回 200；命令行需要正常退出且 stdout 只含一个 JSON 对象。输入上限 1 MiB，适配器输出上限 2 MiB。

## 操作约束与资源预算

`required_order:[["check","commit"]]` 检查前者首次出现早于后者首次出现；完成的 Episode 缺少其中任一操作也失败。对于每次提交都必须重新满足的前置条件，应使用 `required_success_before`：每个目标操作发生前，最近一次 guard 的 `result.ok` 必须严格为 `true`。

支持的预算只有：

| 字段 | 数据来源 |
|---|---|
| `max_steps` | `len(events)`，预算必须为非负整数 |
| `max_cost` | Episode `cost`，单位由适配器约定，数据集内保持一致 |
| `max_latency_ms` | Episode `latency_ms`，不是框架 `execution_latency_ms` |

这些预算是验收约束，不自动拦截工具调用。执行总超时由 `execution.timeout_seconds` 控制；评分总超时由 `grader.timeout` 控制，后者为正数、默认 30 秒。超时不自动重试。

## LLM rubric 与自定义评分器

Rubric 只描述标准，不携带模型配置：

```json
{
  "case_id":"support-tone-001",
  "business":"support",
  "input":"我的退款还没收到。",
  "rubric":[
    {"id":"grounded","description":"答复只陈述可由观测状态支持的信息；不得声称退款已到账。"},
    {"id":"actionable","description":"提供与当前状态相符、可执行的下一步。"}
  ]
}
```

实验的 `grader` 或 `evaluate --grader` 指定的独立 JSON 文件显式选择评委：

```json
{
  "kind":"llm",
  "base_url":"https://your-model-gateway.example/v1",
  "model":"your-judge-model",
  "api_key_env":"AGENT_EVAL_JUDGE_KEY",
  "timeout":30,
  "version":"support-rubric-judge-v1"
}
```

`base_url` 接 OpenAI 兼容的 `/chat/completions`。没有默认云模型或默认付费调用；仅声明 rubric 不会启动评委。未配置评委、缺密钥、超时、传输失败或无效响应都得到 `unknown`。已配置评委时，Case 输入、rubric、观测输出/状态/轨迹会发送到指定服务。

评委每次使用独立上下文，没有工具调用，观测内容作为不可信数据引用。它必须返回以下严格 JSON，`passed` 只能是布尔值或 `null`，每项必须有非空证据：

```json
{"criteria":[{"id":"grounded","passed":true,"evidence":"观测状态为pending，答复没有声称到账。"},{"id":"actionable","passed":null,"evidence":"当前证据不足以确认给出的下一步。"}]}
```

本地自定义评分器配置为 `{"kind":"python","callable":"custom_grader:grade","version":"business-grade-v1","timeout":30}`。函数签名是 `grade(case, episode)`；收到完整私有 Case 和 Episode 的副本，返回非空校验数组或 `{"checks":[...]}`：

```json
[{"id":"audit","status":"pass","evidence":["审计记录与业务预期一致。"]}]
```

每项 `status` 只能是 `pass`、`fail`、`unknown`，证据为非空字符串或非空字符串数组。自定义结果追加到确定性检查中；抛异常或超时产生评分 `unknown`，不会将评委故障当作 Agent 的业务失败。整个评分进程超时或退出时，回退逻辑会保留已观察到的强制轨迹违规、预算超限或独立业务状态失败；这些 `fail` 不会被超时的 `unknown` 覆盖。只完成部分评分不能获得 `pass`。自定义代码应可信并固定版本。

## 最终判定与指标口径

所有声明的状态/输出/规则/rubric/自定义校验按 AND 合并。`outcome` 表示这些业务预期是否满足；最终 `status` 还包含强制轨迹与预算约束。因此可能出现 `outcome:true`、`status:fail`，例如答案正确但执行越权。

| 最终状态 | 含义 |
|---|---|
| `pass` | 所有声明的必需验收标准与约束都有证据且通过 |
| `fail` | 已观察到业务结果不符，或违反强制操作/资源约束 |
| `unknown` | 缺少验收证据、预算数据或评分器有效结论 |
| `infra_error` | 执行未完成，业务结果不予判定；已观察到危险操作仍可优先判 `fail` |

报告同时展示 `pass/(pass+fail)`、`pass/全部已提供尝试`、`(pass+fail)/全部已提供尝试`。未知和基础设施错误不会计为通过。数据集覆盖率另按版本计算，未提交 Episode 的用例不能从报告分母中悄悄消失。

成本统计包含失败尝试；缺任何成本时，完整总成本和每次成功成本为空，并保留已知成本小计和覆盖率。LLM 评委调用费用没有自动计入 Episode 的 Agent 成本。时延 p50/p95 基于实际已知值，并分别报告墙钟耗时和 Agent 报告耗时。

同一 Case 重复 100 次仍是一个 Case，同一家族的多个变体仍属于同一个独立家族。发布门禁先按 Case 汇总全部 trial，再按 `family_id` 进行保守统计；不要通过修改 family_id 把重复样本变成独立证据。门禁包括每个业务切片，默认至少 30 个独立家族只是最低条件，不保证置信界已足够。

## 线上回流记录

`online ingest` 读取 JSONL，每行是带 Episode 的记录：

```json
{
  "episode":{"episode_id":"prod-001","case_id":"faq-refund","agent_version":"v1","status":"completed","events":[],"output":"退款已到账。","cost":null,"latency_ms":800},
  "observed_at":"2026-10-05T08:00:00+08:00",
  "outcome":{"mature_at":"2026-10-05T09:00:00+08:00","success":null,"source":"business-audit-pending"},
  "risk_flags":["user_complaint"],
  "review":{"verified":false,"reviewer":""},
  "replay_case":{"case_id":"faq-refund","business":"support","input":"退款需要多久？","expected_output":"退款预计在3个工作日内到账。"}
}
```

时间必须为带时区的 ISO 8601。`outcome.success` 为 `true`、`false` 或 `null`；`mature_at` 决定结果是否已成熟，`source` 记录业务标签来源。`review.verified:true` 必须有非空审核人。`risk_flags` 为非空字符串数组，可省略为空；`replay_case` 可暂缺，但没有合规回放 Case 的记录不能提升到回归集。

`annotate` 更新延迟结果和审核，不重新抽样。`promote` 只提升已成熟、已审核的失败样本，而且必须属于随机抽样或风险发现流。回流 Case 来自显式审核标准，不从失败 Agent 的答案或状态自动生成；语义去重包含输出预期、checks 和 rubric。相同 case_id 对应不同验收场景会被拒绝，需要有意识地版本化用例。

随机流用于估计成熟且已有标签样本的成功率；风险流用于发现问题，可以与随机流重叠。报告保留未成熟/未知标签和抽样覆盖信息。线上报告和回流操作不生成发布 PASS，也不自动部署。完整命令见 [本地使用指南](LOCAL_USAGE.md)。
