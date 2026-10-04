# Google、Microsoft、AWS Agent 评测调研

调研日期：2026-10-04。本文通过官方 GitHub 仓库源文件核实，未使用未打开页面中的信息。文中日期是文档 `ms.date` 或 GitHub 返回的文件修改日期，不能解释为产品发布日期。文档证实能力存在，不代表本项目已经连接或实际测试这些服务。

## 对比

| 厂商 | 已核实能力 | 对通用企业 Agent-Eval 的借鉴 | 重要边界 |
|---|---|---|---|
| Google ADK | 同时评估 final response 与 trajectory/tool use；单会话 `.test.json` 支持开发测试，多会话、多轮 evalset 支持集成测试；ground-truth 指标和自定义 rubric 指标并存；UI 可把 session 转入 evalset；pytest、CLI 可接构建验证；Trace 页展示执行过程 | 统一 EvalCase / EvalSet 数据模型；把结果成功与过程合规、合理分开；开发小集、发布大集；真实会话转回归样本 | 此次仅核实 ADK，未核实 Vertex 托管评测最新状态。ADK 文档明确 conformance Live Mode is a work in progress，不能写成已具备生产在线巡检 |
| Microsoft Foundry | System evaluation 与 Process evaluation 分层；前者有 Task Completion/Adherence、Intent Resolution、Navigation Efficiency，后者覆盖 Tool Selection、Input Accuracy、Call Success、Output Utilization；导航匹配提供 exact / in_order / any_order；连续评测支持 fixed schedule 和 live traffic sampling；生产 traces 可自动筛选、去重并生成版本化数据集，再接 evaluation | 三家中生产 trace → 筛选 → 数据集版本 → 评测的闭环文档最明确；统一系统与步骤指标；保留事件触发与定时巡检两类调度；用 incident 时间窗建专项回归集 | Agent evaluators、监控页和 traces-to-dataset 均有 preview 标记。生成数据集需指定支持区域和权限，存在网络限制。生成的 query/response 不能默认视为人工验证的 gold |
| AWS AgentCore | On-demand 对 historical traces 评测，Online config 按比例抽样 live requests；支持 SESSION / TRACE / TOOL_CALL 三层；内置 evaluator、自定义 LLM judge、Lambda code-based evaluator；支持 ground-truth assertions / expected trajectory / expected response；在线采样率 0.01–100%，可 pause/resume；CLI 给出 CI quality gate 示例 | 同一个 evaluator 定义在历史批评测与在线抽检中复用；任务、回合、工具三级挂载；按成本配置采样；规则/代码与 LLM rubric 组合；评测配置进入代码和部署流程 | 文档写明 traces 和结果可能延迟 5–10 分钟，是异步质量观测，不能作为同步安全阻断。此次没核实自动将失败线上 trace 晋升为可信 gold 的能力，仍需人工裁决、去重和版本管理 |

## Google：ADK

来源：[Why evaluate agents — ADK documentation](https://github.com/google/adk-docs/blob/main/docs/evaluate/index.md)，工具返回文件修改日期 **2026-10-01**。

已读内容包括：

- 明确分开 “Evaluate Trajectory and Tool Use” 与 “Evaluate the Final Response”。
- Evalset 包含 `session_input`、多个 turn、expected tool use、intermediate agent response、reference response，可表示多轮任务。
- 既有 ground-truth 工具轨迹比较，也有 `rubric_based_final_response_quality_v1`、`rubric_based_tool_use_quality_v1` 等自定义 rubric 指标，并提供多轮质量、hallucination、安全指标。
- UI 可以捕获 session 并转为 evalset；pytest 和 CLI 可纳入开发、集成与构建验证。
- 原文限制：conformance 的 “Live Mode” 属于 “work in progress”。这一限制针对该模式，不应扩展为 Google 所有产品都无在线评测，也不能反向宣称该模式已是生产在线巡检。

此次没有成功核实 Vertex 托管评测的最新文档，故不对其当前上线能力、地域覆盖或产品状态作判断。

## Microsoft：Foundry

### Agent evaluators

来源：[Agent evaluators](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/foundry/concepts/evaluation-evaluators/agent-evaluators.md)，文档 `ms.date: 2026-09-25`。

已核实系统评测和过程评测的分类，以及 Task Completion、Task Adherence、Intent Resolution、Task Navigation Efficiency、Tool Selection、Tool Input Accuracy、Tool Call Success、Tool Output Utilization 等指标。

Tool Input Accuracy 涵盖 groundedness、类型、格式、必填项、意外参数和值适用性六方面。Task Navigation Efficiency 支持 exact_match、in_order_match、any_order_match：后两种允许额外步骤，不应把所有可接受方案锁成唯一轨迹。

页面包含 preview 标记，部分指标也明确标注 preview。多数质量判断依赖 judge，不能替代数据库状态、订单状态、支付结果等业务事实验证。

### 在线监控与连续评测

来源：[Monitor agents with the Agent Monitoring Dashboard](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/foundry/observability/how-to/how-to-monitor-agents-dashboard.md)，文档 `ms.date: 2026-09-25`，页面包含 preview 标记。

文档明确区分：

- “Scheduled evaluation runs on a fixed schedule”。
- “Continuous evaluation samples live traffic as it occurs”。
- 示例以 `RESPONSE_COMPLETED` 事件触发连续评测，使用 `max_hourly_runs=100`。100 是文档示例及默认上限，不能直接当本项目的容量设计。
- 监控展示 token、latency、run success rate 和评价结果。run 技术执行成功不等于业务任务成功，两者应保留独立指标。
- 可接入在其他环境运行、按 OpenTelemetry GenAI 约定埋点并向同一 Application Insights 发送 traces 的 Agent。

### Trace 回流评测数据集

来源：[Convert agent traces into evaluation datasets (preview)](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/foundry/observability/how-to/traces-to-dataset.md)，文档 `ms.date: 2026-09-28`。

已核实：

- 自动过滤低信息 trace，通过 MinHash 选择多样化样本、降低近重复，并处理敏感内容。
- 产出注册于项目中的 versioned dataset，后续可启动 evaluation；生产轨迹与合成场景互补。
- 建议固定 `agent_version`，否则会混合多个活跃版本，削弱评价可解释性。
- 建议使用有代表性的时间窗；事故时间窗适合形成专项回归集。
- `max_samples` 是上限，去重筛选后实际样本可能更少，应检查 `generated_samples`。

明确限制：Application Insights 查询需要允许 public network access；连接自有 storage 时，数据集生成要求该存储允许 public network access。该能力标记为 preview；区域、权限、网络前提必须在具体落地时核对。

本项目应借鉴生产证据回流流程，仍保留脱敏审核、业务事实校验和标签裁决。官方所谓 evaluation-ready 数据集并不等于已验证的任务成功真值。

## AWS：AgentCore

### Evaluations

来源：[Evaluations — AgentCore CLI](https://github.com/aws/agentcore-cli/blob/main/docs/evals.md)，工具返回文件修改日期 **2026-08-12**。

已核实：

- On-demand evaluator 对历史 trace 按 lookback window 评测；online eval config 按比例采样实时请求。
- 粒度包含 SESSION（整段会话）、TRACE（单回合响应）、TOOL_CALL（工具选择与使用）。
- 支持内置 evaluator、自定义 LLM-as-a-Judge，以及以现有 Lambda 实现的 code-based evaluator。
- On-demand 命令支持 ground-truth assertion、expected trajectory、expected response。
- Online sampling rate 支持 0.01–100%，可暂停/恢复，可按 runtime endpoint 限定监控范围。
- Evaluator 和 online config 写入项目 `agentcore.json`，部署时创建/更新，适合纳入版本管理。
- 文档含结果历史查询和 CI/CD quality gate 示例。其示例只检查单一 aggregate score，本项目不应照搬为多业务上线门禁，应补充分切片、硬约束、置信区间和缺失值处理。

文档明确 traces 和评价结果可能在调用后 5–10 分钟出现。因此在线评测应按异步观测设计，不应宣称它提供请求同步安全拦截。本文未核实自动将线上失败晋升为可信 gold 的能力。

### Observability 与旧工具状态

来源：[Bedrock AgentCore Starter Toolkit README](https://github.com/aws/bedrock-agentcore-starter-toolkit/blob/main/README.md)，工具返回文件修改日期 **2026-06-29**。

文档明确 Observability 支持 OpenTelemetry-compatible telemetry、统一运行看板和工作流步骤可视化；Evaluation 支持按需和在线评测。

该 README 同时声明 Starter Toolkit CLI 已不再受支持，新项目应使用 AgentCore CLI。Starter Toolkit 保留给现有 Python 工作流，本项目不应将旧 CLI 当作新集成基线。

## 对企业平台设计的归纳

以下是本项目依据调研作出的设计建议，不是宣称每个厂商已完整提供自动飞轮：

**统一 Trace → 线上抽样/规则筛选 → 故障聚类 → 人工验证业务事实与期望 → 版本化 EvalCase → 离线多次回放与候选对比 → 分切片质量门禁 → 灰度线上验证 → 新问题回流。**

值得复用的共同能力是结果与过程分层、可复用 evaluator、分级 trace、离线批评测和线上抽检。企业平台仍应独立实现：可信标签形成、业务状态断言、环境快照与工具仿真、风险切片覆盖、评测器校准，以及版本和证据链管理。
