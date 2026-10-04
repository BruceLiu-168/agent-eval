# OpenAI 与 Anthropic：Agent 评测调研

核实日期：2026-10-04。面向支持多种业务的通用企业 Agent 平台。

本文件依据 GitHub 连接器实际读取的 OpenAI、Anthropic 官方组织仓库文件。下文日期均为连接器返回的**文件最后修改日，不是文章发布日期或产品发布日期**。链接指向可变的 `main` 分支，因此后续内容可能变化。本文将来源事实与本项目设计建议分开陈述；没有依据这些示例推断厂商内部生产架构。

## 1. OpenAI：从生产失败出发的评测飞轮

### 1.1 Analyze → Measure → Improve

来源：[Building resilient prompts using an evaluation flywheel](https://github.com/openai/openai-cookbook/blob/main/examples/evaluation/Building_resilient_prompts_using_an_evaluation_flywheel.md)。文件最后修改日：2025-10-07。

**已核实事实：**

- 飞轮明确分为 Analyze、Measure、Improve：分析错误、构建数据集与自动评分器衡量失败、进行针对性改进，再持续迭代。
- 示例从生产租赁助手的输入、输出及 trace 开始，人工阅读失败样本，先用 open coding 记录具体问题，再用 axial coding 汇总为失败类别。
- 自动评分支持 Python grader 和 LLM grader。示例按具体业务问题建立格式及可用时间准确性评分器。
- 可以通过场景维度组合和扰动补充合成数据，而不是简单要求模型生成一批相似问题。
- 文章最后明确建议将 grader 接入 CI/CD，并监控生产数据发现新失败模式。

短原文证据：

> “The flywheel consists of three phases”

> “integrating your graders into a CI/CD pipeline and monitoring production data to discover new failure modes”

**对本项目的建议：**把主流程设计为“生产证据 → 失败分类 → 可执行评测用例 → 候选修复 → 离线回归 → 受控发布 → 生产反馈”。改进对象包括 prompt、模型、检索、工具和编排。来源中的例子集中于 prompt，并不意味着企业平台只能优化 prompt。

### 1.2 评委本身需要评测

来源：同一文件的 *Aligning your LLM judge* 部分。

**已核实事实：**

- 用领域专家（SME）标注的 gold standard 数据集检验 LLM judge。
- 类别不均衡时，整体 accuracy 容易掩盖问题。文章将“失败”作为正类，使用 TPR 衡量识别失败的能力，TNR 衡量识别通过的能力。
- 示例将标注数据分为 train、validation、test，约占 20%、40%、40%；train 用于 few-shot 示例，validation 用于调整 judge，隔离的 test 用于最终测量。

短原文证据：

> “A judge that always guesses ‘pass’ might be 95% accurate but will never find a single failure.”

**对本项目的建议：**为评分器建立版本、人工校准集、误报率与漏报率，按业务和风险切片检查。固定盲测集不得用于调 prompt。20/40/40 是来源示例，不应成为所有业务的强制比例。

## 2. OpenAI：统一执行证据与评测契约

### 2.1 Agents SDK tracing

来源：[Tracing](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md)。文件最后修改日：2026-09-30。

**已核实事实：**

- SDK 内建 tracing 覆盖 LLM generation、tool call、handoff、guardrail 及自定义事件，可用于开发和生产。
- Trace 表示端到端 workflow，含 workflow name、trace ID、可选 group ID 和 metadata；span 含时间、trace ID、parent ID、事件数据。
- 可以添加额外 trace processor，也可以替换默认 processor，向其他后端导出。
- 文档明确说明生成与函数调用 span 可能包含敏感输入、输出，支持关闭敏感数据捕获。添加 processor 不会自动移除默认导出器。

短原文证据：

> “debug, visualize, and monitor your workflows during development and in production”

**对本项目的建议：**离线与线上共用 trace/span 契约，在标准字段之外关联租户、业务场景、Agent 版本、运行环境、数据集用例、评分及业务结果。通过适配器接入不同 Agent 框架。将脱敏与导出放在同一可控路径，避免把添加脱敏 observer 误认为已经阻止原始数据外发。

**范围限制：**这份来源确认的是 SDK tracing；不能据此把所有 trace grading、数据管理或评测能力都说成 SDK 原生实现。

### 2.2 生产日志监控与版本回归

来源：[Evaluations Example: Push Notifications Summarizer Monitoring](https://github.com/openai/openai-cookbook/blob/main/examples/evaluation/use-cases/completion-monitoring.ipynb)。文件最后修改日：2025-04-08。

**已核实事实：**

- 示例通过 `store=True` 保存生产 chat completions，用 metadata 区分业务及 prompt 版本。
- `Eval` 保存数据结构和 testing criteria；一个 `Eval` 可以有多个 `Run`，复用同一评分标准。
- 使用 metadata 按 prompt 版本筛选 stored completions，比较运行结果并发现回归。
- 可以复用历史输入，换模型重新生成结果并评分。

短原文证据：

> “An Eval can have many Runs, which are each evaluated using your testing criteria.”

**对本项目的建议：**分离 `EvalSpec` 与 `EvalRun`。线上已有结果的评分与离线重新执行可以共享评分契约，但运行模式必须明确记录。不同真实流量版本的观察性比较可能受用户构成、时间及场景变化影响，不能直接证明某个版本带来因果收益；发布收益仍应通过合适的受控实验判断。

### 2.3 数据集版本与 meta-eval

来源：[Building an eval](https://github.com/openai/evals/blob/main/docs/build-eval.md)。文件最后修改日：2023-06-02。

**已核实事实：**

- Eval 由数据集和评测类组成，支持基础规则与模型评分模板。
- 注册名包含 `<eval_name>.<split>.<version>`，split 可为 dev、val、test；修改 eval 应升级版本。
- 模型评分建议提供人工 `choice labels`，通过 meta-eval 检查评分器与人工判断的一致性。

**对本项目的建议：**Dataset、rubric、judge、runner 与环境分别版本化，运行结果保存完整版本组合。评分器升级时，对固定校准集和部分历史结果双跑，识别评分口径变化，防止把“尺子改变”误判为 Agent 改善。

补充来源：[OpenAI Evals README](https://github.com/openai/evals/blob/main/README.md)，文件最后修改日 2024-12-18。已核实其支持私有业务评测数据、已有评测注册表、自定义评测，以及用于 prompt chain 和 tool-using agent 的 Completion Function Protocol。这个较早的开源框架来源不等于当前所有托管平台能力的说明。

## 3. Anthropic：分层评分与工具质量

### 3.1 三种评分方式

来源：[Building Evals](https://github.com/anthropics/claude-cookbooks/blob/main/misc/building_evals.ipynb)。文件最后修改日：2026-09-02。旧仓库名 `anthropic-cookbook` 已迁移，本文链接使用当前 `claude-cookbooks`。

**已核实事实：**

- 离线评测用于判断 prompt 修改是否改善关键指标，以及系统是否适合进入生产。
- 介绍 code-based、human、model-based grading 三类方式。能可靠使用代码评分时，它具备速度和一致性优势；人工评分能力强但成本高；模型评分适用于开放文本等任务。
- 评测应贴合具体任务，并尽量反映真实问题及难度分布。
- 模型评委是否适用需要实际尝试，并阅读评分样本判断。

短原文证据：

> “try to have the distribution in your eval represent ~ the real life distribution of questions and question difficulties”

**对本项目的建议：**可验证状态变化优先用确定性断言；开放语义使用经过校准的模型评分；争议、高风险及定期抽样进入人工复核。日常分布集与失败挑战集分别报告，不把高风险过采样后的分数当作自然流量成功率。

### 3.2 工具评测需要同时观察成功和执行代价

来源：[Tool Evaluation](https://github.com/anthropics/claude-cookbooks/blob/main/tool_evaluation/tool_evaluation.ipynb)。文件最后修改日：2026-02-17。

**已核实事实：**

- 示例 Agent 运行工具调用循环，并记录工具调用次数和各次耗时。
- 评分实现包含预期输出与实际输出的 exact match、任务总耗时、总调用数、执行总结与工具反馈。
- 汇总报告包含 accuracy、average task duration、average tool calls、total tool calls。
- 反馈提示词要求检查工具名称、参数、描述、执行错误及返回信息等问题。

**对本项目的建议：**在任务成功之外，记录工具正确性、冗余调用、延迟、成本及失败恢复。工具 schema、描述、权限和返回值的改动都应触发相关回归。该 notebook 是简化教学示例，不应原样作为企业执行器；例如其中动态 `eval` 调用已有非生产用途提示。

## 4. Anthropic：独立评委与运行时自检

来源：[Outcomes: agents that verify their own work](https://github.com/anthropics/claude-cookbooks/blob/main/managed_agents/CMA_verify_with_outcome_grader.ipynb)。文件最后修改日：2026-09-24。

**已核实事实：**

- 文中明确此功能属于 Claude Managed Agents beta。
- writer 根据交付要求产出；独立 grader 在自己的上下文窗口读取 rubric 和产物，逐项评判，反馈缺口给 writer，形成有上限的修改循环。
- 示例强调 rubric 必须可核实，要求具体证据后才可判定满足；模糊的“检查是否涉及某话题”容易让评委直接放行。
- 每轮新的 grader 重新检查整个产物；示例通过抓取引用 URL、核对原文和文档类型发现问题。

短原文证据：

> “Require concrete evidence … before the grader passes anything.”

> “The grader is independent and stateless.”

**对本项目的建议：**评分器可借鉴独立上下文、逐条 evidence、可执行判定及重试预算上限。生产自检的 rubric 与发布评测可以复用部分标准，但必须保存评分来源与用途。

**范围限制：**这是 Agent 运行时“产出→自检→修改”的循环，不等于平台“线上发现→离线回归→发布验证”的质量飞轮，也不能替代独立盲测集或线上业务实验。独立上下文降低 writer 对评委判断的影响，不代表模型评分已经成为完全可靠的 ground truth。

## 5. 补充：OpenAI Cookbook 收录的 Langfuse 示例

来源：[Evaluating Agents with Langfuse](https://github.com/openai/openai-cookbook/blob/main/examples/agents_sdk/evaluate_agents.ipynb)。文件最后修改日：2026-07-20。

**已核实事实：**该示例通过 OpenAI Agents SDK、OpenTelemetry instrumentation 与 Langfuse 观察 trace；在线关注成本、延迟、用户反馈和 LLM-as-a-Judge，离线使用数据集运行并比较配置。文中把 trace 与 dataset item 关联。

**归属说明：**应称为“OpenAI Cookbook 收录的 Langfuse 集成示例”。它能够支持端到端评测工作流的参考，但不能把 Langfuse 功能表述为 OpenAI 平台全部原生提供的能力。

## 6. 本项目采用的组合与核实边界

本项目建议从上述来源提取四个可组合的设计原则：

1. **从失败证据建立评测资产：**线上 trace 和业务结果经复核后进入版本化数据集，保留来源与失败分类。
2. **执行与判定分离：**同一评分契约可以评已有 trace，也可以评隔离环境重新执行的结果；运行环境与数据条件必须显式记录。
3. **评委需要校准：**代码、模型、人工各有所长。评分器升级不能默默改变指标含义；证据不足应返回待核实，而非强行通过或失败。
4. **离线与线上各回答自己的问题：**离线负责可复现的能力与回归验证；线上负责真实分布、运行质量与业务收益；受控发布将两者连接。

这些是基于来源形成的项目建议，并非任何一家厂商完整内部实现的复述。

以下来源在本轮**未成功读取原文**，不得标为已核实：

- Anthropic 官网 *Demystifying evals for AI agents*：`https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents`。因此本文没有将 `pass@k` / `pass^k`、`transcript` / `outcome` 的具体表述归因为已读该文。若方案采用这些概念，应明确其定义及本项目用途，或补充独立核实来源。
- OpenAI 平台的 trace grading、agent evals、evaluation best practices 网页。本轮已确认 SDK tracing 与官方 cookbook 中的评测、监控方法，不能据此声称这些产品页面的全部现行能力已验证。

本轮没有调用任何厂商付费 API 运行上述示例；核实对象是公开官方文件的内容，而非对产品可用性、区域覆盖、价格或生产效果的实测。
