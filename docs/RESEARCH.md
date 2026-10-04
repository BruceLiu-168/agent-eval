# Agent 评测调研总览

调研日期：2026-10-04。覆盖 OpenAI、Anthropic、Google、Microsoft、AWS、字节 CozeLoop，以及 LangSmith、Langfuse；另单独分析美团方法。范围是与企业 Agent 离线/在线闭环有关的公开资料，不声称穷尽所有厂商或完成实际产品压测。

## 来源与可信边界

除美团部分外，已通过连接器实际读取各厂商/项目官方 GitHub 仓库中的文档或示例。正文使用原始链接，子报告标明日期为文件修改日、文档日期还是文章自述发布日期，避免混作功能发布时间。产品功能与方法建议分开，不把 sample/cookbook 当成全部已托管的产品能力。

美团官网直连未成功，已读取第三方保存的完整文本，并通过独立周刊交叉确认文章标题和原网址。**它属于第三方存档证据，不能称为本轮直接核验了官网原文**。文中图片未单独读取；不依据未读图片补造细节，也不把文中案例收益数字当作本项目效果承诺。

## 对比与设计决策

| 来源 | 本轮证实的重点 | 本项目借鉴 | 不能直接推导的结论 |
|---|---|---|---|
| [OpenAI 飞轮](https://github.com/openai/openai-cookbook/blob/main/examples/evaluation/Building_resilient_prompts_using_an_evaluation_flywheel.md) 与 [Agents SDK tracing](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md) | Analyze→Measure→Improve；生产失败分类、grader、专家校准、CI/CD；模型/工具/交接等 trace | 统一任务证据与评测契约，失败分类后再优化，judge 单独校准 | Trace 本身不能证明业务成功；本轮未直接读托管 trace-grading 产品页 |
| [Anthropic eval cookbook](https://github.com/anthropics/claude-cookbooks/blob/main/misc/building_evals.ipynb) | code/human/model 三类 grader，真实任务分布；工具评测记录准确性、耗时、调用情况 | 可确定的状态用规则，开放语义才用经过校准的模型；任务与工具并评 | 运行时自检不是独立发布门禁，也不能代替线上 A/B |
| [Google ADK](https://github.com/google/adk-docs/blob/main/docs/evaluate/index.md) | final response 与 trajectory/tool use；单会话测试、多会话多轮 evalset，pytest/CLI，session 转用例 | Case/Set 分层，结果与过程独立判定，可接 CI | 文档里的 conformance Live Mode 仍是 work in progress |
| [Microsoft Foundry](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/foundry/concepts/evaluation-evaluators/agent-evaluators.md) | 系统级/过程级指标；定时与流量抽样评测；trace 转版本化数据集 | 分层评分、按事故窗口构建回归、显式版本绑定 | 相关能力带 preview；自动生成的数据集不自然等于可信 gold |
| [AWS AgentCore](https://github.com/aws/agentcore-cli/blob/main/docs/evals.md) | 历史按需与在线抽样；SESSION/TRACE/TOOL_CALL；代码和 LLM evaluator；CI gate | 离线/在线共用 evaluator 定义，控制评分成本 | 异步结果有延迟，不能用作请求内同步授权阻断 |
| [字节 CozeLoop](https://github.com/coze-dev/coze-loop/blob/main/README.md) | 评测集、评估器、实验、SDK Trace、Prompt 版本、开源部署 | 国内开源底座候选，复用观测和实验层 | README 不足以证明完整自动线上评分、人工流转与回流 |
| [LangSmith](https://github.com/langchain-ai/docs/blob/main/src/langsmith/evaluation-concepts.mdx) | 线上问题→离线案例→验证修复→线上确认；抽样、预算、人工队列 | 产品流程参照，清楚分离 dataset experiment 与 online run/thread | 模型评分不能自动等同成熟业务结果 |
| [Langfuse](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/experiments/datasets.mdx) | 生产评分、人工队列、来源 trace 关联、数据集版本、自托管 | 开放观测与评测基础设施候选 | 业务状态回放/归因/门禁需要自行补齐，数据版本覆盖有边界 |
| [美团《Agent评测漫谈》](https://tech.meituan.com/2026/08/07/Agent-Evaluation.html)（第三方存档） | 观测+评测；结果/过程/效率/风险；人人与人机对齐；二元/unknown rubric；采集→清洗→评测→质检→归因配合线上 A/B | 用业务指标到任务/动作的映射组织评测；先运转闭环、再扩大指标；设置标准负责人 | 不把文中对未来基建的建议视为所有能力均已上线；没有迁移文中收益数字 |

阿里 AgentScope 官方 README 仅能证明本轮读取的执行框架/服务能力，不能据此代替阿里云百炼的评测产品调研。百炼、腾讯、百度等未完成最新原文核验，因此不填造能力格子。

## 推荐方案

采用 **A（离线门禁）+ B（线上回流）** 作为通用基础，在关键业务加上 **C（有状态仿真）**。这是一项工程选择，不是哪个厂商的原样复制。

- Trace、dataset、experiment、评分存储和标注界面优先复用已有 Langfuse/LangSmith/CozeLoop 或所属云平台，选型还需验证数据驻留、框架兼容、费用与运维要求。
- 自建业务成功验证器、成熟结果关联、状态快照、工具沙箱、严重约束和发布策略，它们决定平台能否真实服务业务。
- 保留有已知采样概率的随机流估计质量，另设风险发现流挖失败。标签缺失和延迟本身也要监控。
- 冻结 case、模型、prompt、工具、知识、环境和 judge 的版本，再比较候选；规则/评委改动不能被当作产品效果提升。
- 报告始终保留 unknown 和 infrastructure failure；同样很差的两个版本不能仅凭非劣性获得发布许可。

## 美团方法如何具体落地

以退款为例，把业务目标“问题解决且不产生错误资金操作”映射到任务指标“退款金额正确、只发生一次、无越权”，再映射到可判定 rubric“身份检查通过”“策略与审批通过”“超时后查状态并复用幂等键”。结果、过程、效率、风险分别出证据，不能互相抵消。

评测标准由一位明确负责人主持对齐，多名专家背靠背标注；分歧先检查标准，必要时裁决。模型判定再对照专家标签测误报、漏报和 unknown，不能只追求总体一致率。Good Case 用于说明预期成功行为，Bad Case 用于暴露边界；原型的自动回流仅处理审核失败，Good Case 由版本化种子集补充。

美团文章的五个环节被展开为本项目的可实现链路：采集→脱敏去重与状态补齐→规则/模型/人工判定→judge 质检→失败归因→用例回归→门禁→灰度成熟结果→继续采样。本文的采样统计、绝对质量门槛和安全重放等细节是我方设计，不能归称美团内部实现。

详细证据：[模型厂商](RESEARCH_MODELS.md) · [云厂商](RESEARCH_CLOUDS.md) · [评测平台](RESEARCH_PLATFORMS.md) · [美团来源与方法](RESEARCH_MEITUAN.md)。实现对应 [DESIGN.md](DESIGN.md) 与项目 [README](../README.md)。
