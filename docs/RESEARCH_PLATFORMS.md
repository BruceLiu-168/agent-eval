# Agent 评测平台调研：LangSmith、Langfuse、CozeLoop 与 AgentScope

调研访问日期：2026-10-04。

本文通过官方 GitHub 仓库及官方文档仓库核实公开能力。文中日期是 GitHub connector 返回的**文件修改时间，不是功能发布日期**。`main` 分支持续变化，实施选型时应再固定版本、部署形态与产品套餐。本文没有把功能说明视为对所有部署版本和商业套餐的保证。

## 核实结果

| 平台 | 已核实的闭环能力 | 对通用企业 Agent-eval 的启发 |
|---|---|---|
| **LangSmith** | Offline dataset/experiment 与 online run/thread 明确区分；线上问题转离线用例、离线验证修复、上线后验证改善；支持过滤、抽样、历史回填、评测预算上限；人工队列可自动入队、多人评审、纠正参考答案、加入数据集、实验成对比较 | 可作为“发现问题 → 人工确认 → 固化用例 → 比较版本”的产品流程参考 |
| **Langfuse** | 线上 observations 通过规则的过滤条件、采样率触发 code/LLM evaluator；人工队列、用户反馈、外部评分 API；生产 trace/observation 与 dataset item 保留来源关系；dataset timestamp version 支持同版本实验重跑 | 可复用观测、评分、数据集、实验基础设施；数据来源追溯与固定版本实验值得采用 |
| **字节 CozeLoop 开源版** | Evaluation set、evaluator、experiment 管理；SDK trace 上报；覆盖模型、工具调用的执行过程观测；Prompt 版本管理；支持 OpenAI、火山方舟；Apache 2.0，Docker/Helm 部署 | 国内开源底座候选；仅凭 README 尚不能确认完整自动线上评分、标注流转与自动回流的具体能力和版本范围 |
| **AgentScope 2.0** | 官方 README 定位 Agent framework 与 Agent Service，提供运行、编排、工具、沙箱、多租户服务等能力 | 可作为待测 Agent 的执行接入方；本轮所读来源未证实相应完整评测闭环，不能据此断言产品没有这些能力 |

## LangSmith

### 1. 离线与在线的对象模型及闭环

- 官方来源：[Evaluation concepts](https://github.com/langchain-ai/docs/blob/main/src/langsmith/evaluation-concepts.mdx)
- 文件修改时间：2026-10-03T06:20:55Z。

文档明确区分两种评测目标：

- 离线：datasets/examples，用于部署前 benchmark、回归、组件测试和历史数据回测；reference outputs 是可选字段。
- 在线：生产 tracing 中的 runs/threads，用于持续质量监测、发现异常、收集需要纳入离线数据集的问题。

Experiment 表示某一应用版本在指定数据集上的评测结果，包含各样本的 outputs、evaluator scores、execution traces，并支持多个 experiment 对比。

原文直接说明闭环：

> Online evaluations surface issues that become offline test cases, offline evaluations validate fixes, and online evaluations confirm production improvements.

设计启发：离线与在线共享评测定义、案例来源及证据关联，但在线通常没有参考答案，不能把在线 judge 分数直接当成真实业务任务成功率。真实任务成功需要接入业务 outcome。

### 2. 线上评测触发、抽样与成本控制

- 官方来源：[Set up LLM-as-a-judge online evaluators](https://github.com/langchain-ai/docs/blob/main/src/langsmith/online-evaluations-llm-as-judge.mdx)
- 文件修改时间：2026-10-03T06:20:55Z。

已核实能力：

- 根据用户差评、指定工具调用、业务 metadata 过滤待评测 runs。
- 通过 sampling rate 控制触发比例。
- 创建规则时可选择历史 backfill，异步执行；文档说明只能在创建规则时启用该回填方式。
- 提供 evaluator 每周 spend limit；达到限额后暂停，直到额度重置或提高。
- Run-level 与 thread-level 评测分开配置。

适用与限制：适合异步质量监控和问题挖掘。文档明确在线评测会将相关 trace 自动升级为 extended data retention，影响费用；选型估算必须同时计算 judge 调用和 trace 留存成本。

### 3. 人工标注与用例回流

- 官方来源：[Annotation queues](https://github.com/langchain-ai/docs/blob/main/src/langsmith/annotation-queues.mdx)
- 文件修改时间：2026-10-03T06:20:55Z。

已核实能力：

- 通过自动化规则把错误、低分等符合条件的 run/thread 分派到标注队列。
- 支持要求多名 reviewer 或指定 reviewer；可跟踪是否完成评审。
- 评审 run 时可纠正输入、输出，添加 assertions，并 Add to Dataset。
- Thread 可整体加入数据集。
- Pairwise annotation queue 从两个 experiments 创建，用于人工 A/B 比较。

边界：run 和 thread 不是能力完全相同的标注对象。当前文档说明 thread items 支持 rubric feedback 和 Add to Dataset，但不支持 reviewer notes 和 assertions；不能统一描述为所有对象都支持全部标注功能。

### 4. SDK 可编程回流

- 官方来源：[LangSmith Python SDK README](https://github.com/langchain-ai/langsmith-sdk/blob/main/python/README.md)
- 本轮未独立记录该文件的修改时间，不用根目录 README 日期代替。

示例明确展示 `list_runs → create_dataset → create_example` 的转换流程，以及 `evaluate_run` 对单个运行生成自动反馈。该来源验证了使用 SDK 连接 trace 数据和离线 dataset 的可行性。

## Langfuse

### 1. 生产流量评分

- 官方来源：[Evaluate Production Traffic](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/get-started/online.mdx)
- 文件修改时间：2026-09-22T17:52:12Z。

文档将 evaluator 定义为“如何评分”，rule 定义为“哪些 incoming observations 需要评分”：过滤条件、采样率、一个或多个 evaluator。

已核实能力：

- 先在代表性生产样本上调试 evaluator，再挂载线上规则。
- 支持 LLM-as-a-Judge 与 Python/TypeScript code evaluator。
- 查看最近七天的匹配流量以及 LLM judge 的预估成本，调整采样率。
- 对历史 observations 执行 batch evaluation。
- 通过人工评分、annotation queues、用户反馈、外部 API/SDK 追加 scores。

设计启发：把 evaluator 定义与线上调度规则分开，有助于离线、在线复用同一评分逻辑，同时分别管理过滤、抽样和预算。

### 2. 数据集、版本与生产来源

- 官方来源：[Datasets](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/experiments/datasets.mdx)
- 文件修改时间：2026-09-25T12:36:02Z。

已核实能力：

- 选取生产失败样本，由专家补充 expected output 后加入数据集。
- Dataset item 支持 `source_trace_id` 与 `source_observation_id`，保留生产来源。
- UI 支持批量 observations 入集，通过字段映射和 JSON path 转换输入、预期输出等字段。
- Dataset item 的新增、更新、删除、归档产生 timestamp version；可按特定时间点读取数据集，并固定该版本跑实验。
- 支持输入与 expectedOutput 的 JSON Schema 验证。

已证实限制：

- Dataset version 只覆盖 items，schema 修改不会产生新的版本。因此完整复现还需要平台额外冻结 schema、grader、Agent 和环境等版本。
- 多模态 dataset items 可用于 SDK experiments，但该文档说明 UI experiments 尚不支持这类媒体附件。
- 批量入集允许 partial success；部分 item 不满足 schema 时，合法条目仍会被导入，需要检查批处理结果。

### 3. 标注、纠正与 judge 校准

- 官方来源：[Annotation Queues](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/evaluation-methods/annotation-queues.mdx)
- 文件修改时间：2026-09-30T16:15:45Z。

已核实能力：

- 标注对象覆盖 trace、observation、session。
- Score config 定义评分维度，领域专家可添加 scores、comments、corrected outputs。
- 支持通过 Score Analytics 分析人工与模型评分 agreement，并以人工标签校准 judge。
- 提供 annotation queue API，可用于自行构建标注界面和流程。

设计启发：标注记录与自动评分应分别保留来源、版本和证据；人工标签可用于验证 judge，而不是直接把模型评分沉淀为真值。

### 4. 开源部署与可组合性

- 官方来源：[Langfuse README](https://github.com/langfuse/langfuse/blob/main/README.md)
- 文件修改时间：2026-09-24T12:12:43Z。

该来源核实开源、自托管、API/SDK、模型及框架集成、Prompt 管理、追踪、数据集与评测能力。企业选型仍需结合具体套餐和版本核实权限、审计、保留策略等要求；本文没有逐项核实这些商业能力。

## 字节 CozeLoop

- 官方来源：[CozeLoop README](https://github.com/coze-dev/coze-loop/blob/main/README.md)
- 文件修改时间：2026-03-30T07:36:35Z。

已核实能力：

- Evaluation set 管理、evaluator 管理、experiment 管理。
- 自动化多维度评测 Prompt 与 Coze Agent 输出。
- SDK trace 上报，记录输入到输出的执行阶段，包括模型调用、工具执行、中间结果和异常。
- Prompt Playground、模型输出比较与 Prompt 版本管理。
- OpenAI、火山方舟及其他模型接入。
- Apache 2.0；提供 Docker Compose 与 Kubernetes Helm 部署方式。

官方 README 说明开源版提供商业版的核心基础模块，因此不应默认商业版与开源版功能对等。本轮没有通过具体产品文档或代码核实开源版自动线上评分、人工队列、自动样本回流的完整机制，比较时应标记“待核实”，而不是“支持”或“不支持”。

README 提供的进一步核实入口：

- [评测快速开始](https://loop.coze.cn/open/docs/cozeloop/evaluation-quick-start)
- [Trace 接入](https://loop.coze.cn/open/docs/cozeloop/trace_integrate)
- [系统架构](https://github.com/coze-dev/coze-loop/wiki/3.-Architecture)

这些入口来自已读取的 README；本轮未独立读取入口正文，不能作为已核实的具体能力证据。

## AgentScope 与阿里云调研边界

- 官方来源：[AgentScope README](https://github.com/agentscope-ai/agentscope/blob/main/README.md)
- 文件修改时间：2026-09-28T12:12:37Z。

当前 README 将 AgentScope 2.0 定位为 Agent framework 与 Agent Service，描述模型、工具、事件、权限、记忆、沙箱、多租户、多会话服务、编排等能力。它可作为 Agent-eval 的执行适配对象。

该来源不足以证实其具备与 LangSmith/Langfuse 对应的 dataset → experiment → online evaluation → annotation → dataset 完整产品闭环。这里的结论是**本轮未证实**，不是断言 AgentScope 没有相关能力。

阿里云百炼商业评测能力本轮未取得可核实原文，不纳入已核实能力比较；不能以 AgentScope 的执行框架能力代替百炼产品的评测能力。

## 对企业 Agent-eval 的应用建议

以下是基于调研的设计建议，不是对任一厂商现成功能的声明。

### 可复用部分

优先复用 trace 接入与查询、评分存储、dataset、experiment、人工标注 UI、基本实验比较。不要同时采用多个完整观测底座并重复储存敏感 trace；可通过统一适配接口保留替换能力。

### 企业平台需要建设的部分

1. **业务结果关联**：将退款完成、工单解决、审批通过等延迟产生的 `business_outcome` 与 episode/trace 关联。输出质量分数不能替代任务是否完成。
2. **可重现的执行环境**：冻结 Agent、Prompt、模型配置、grader、数据集、工具契约及必要环境版本；对有状态 Agent 补充状态快照和可控工具回放。仅保存输入输出不足以重现执行。
3. **业务与风险分层采样**：保留随机基线样本，同时采集差评、高风险动作、罕见工具路径和漂移样本；记录采样策略，避免只收失败案例造成分布偏差。
4. **领域成功判定与发布门禁**：定义业务完成条件、关键动作约束、不可接受失败类别及分业务门禁；不使用一个总分替代所有业务判断。
5. **人工确认后的用例晋级**：生产样本经过脱敏、去重、确认预期结果后进入回归集，保留源 trace、标注记录、评测器版本、用例版本及修复关联。
6. **Judge 的独立验证**：用人工真值校准模型裁判，监控 agreement 与漂移，对模型裁判不确定或有争议的样本进入人工复核。

推荐闭环：

`生产 trace + 业务 outcome → 分层采样与问题发现 → 人工复核/根因归类 → 可重现 case → 固定版本离线实验 → 分业务发布门禁 → 灰度/在线结果验证 → 更新用例与评测器`

其中 case 的晋级、修复验收和真实业务结果回流，才是企业飞轮的核心；单纯把 trace 自动复制到 dataset 并不意味着已经形成质量改进闭环。
