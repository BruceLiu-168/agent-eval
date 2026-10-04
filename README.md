# Agent Eval：三种可运行方案

面向多业务企业 Agent 的研究型参考实现。建议采用 **A 离线门禁 + B 线上回流** 作为基础，在退款、授权等有副作用的关键业务加入 **C 状态仿真**。

项目只依赖 Python 标准库，在 Python 3.12 验证。当前 Agent 是脚本策略，业务、成本、延迟及线上事件均为合成数据；没有接入真实模型、厂商平台或生产系统。这里实现的是可运行核心，不是已经上线的企业评测平台。

## 快速运行

```bash
git clone https://github.com/BruceLiu-168/agent-eval.git
cd agent-eval
python -m agent_eval demo --out artifacts/demo
python -m unittest discover -s tests -v
```

`demo` 执行三种方案，并完成“模拟线上失败 → 审核后的回归案例 → 修复版本复测”。它返回 0 表示演示执行完成，**不是发布获批**。发布决策在报告的 `gate.decision` 中。

## 三种方案与取舍

| 方案 | 已实现内容 | 优点 | 代价与边界 | 适用场景 |
|---|---|---|---|---|
| A：CI 离线门禁 | JSONL 数据集、可插拔 Agent adapter、成对比较、绝对质量下限、状态与轨迹约束、版本指纹、统计门禁 | 接入简单，变更可追踪，发布前快速发现回归 | 对未知线上问题不敏感；静态集会过拟合；必须另接真实 Agent 和业务 oracle | 已有 CI，先建立质量基线 |
| B：线上 Trace 回流 | SQLite 接入、随机/风险双通道、延迟结果回填、脱敏、审核、幂等回流 | 面向真实分布持续发现问题，形成可复测样本 | 依赖业务结果与审核；标签缺失会偏置；本地 SQLite 不是高吞吐多租户服务 | 已有流量，需持续优化与事故复盘 |
| C：有状态仿真 | 退款和权限申请环境、3 个策略版本、13 个场景、多 Agent 交接、故障注入与重复运行 | 检查实际副作用，覆盖超时、重复写入、越权等难题 | 每种业务要维护状态模型；仿真偏差必须用真实轨迹和沙箱集成验证 | 工具写操作、多轮任务、关键业务 |

三种方案共享同一 Case/Episode/Grade 契约，能分别运行，也能组合。A 不负责采集生产流量，B 不证明业务仿真正确，C 不证明线上效果提升。

## 独立命令

```bash
# A：预期返回 3，因示例样本不足且存在故障场景。
python -m agent_eval offline --out artifacts/offline.json

# A：故意回归的策略，预期返回 2，阻断发布。
python -m agent_eval offline --candidate regressed --out artifacts/blocked.json

# C：重复执行有状态场景；成本和时延为模拟值。
python -m agent_eval simulate --trials 3 --out artifacts/simulation.json

# B：查看 demo 产生的本地事件与回归集。
python -m agent_eval.online --db artifacts/demo/online.sqlite report --as-of 2026-10-04T12:00:00Z
python -m agent_eval.online --db artifacts/demo/online.sqlite promote artifacts/demo/regression.jsonl --as-of 2026-10-04T12:00:00Z
```

离线命令退出码：`0=PASS`、`2=BLOCK`、`3=INCONCLUSIVE`。CI 应仅接受 0。样本不足、缺配对结果、未知判定或基础设施故障都不能获得 PASS。

新事件文件的接入语法：`python -m agent_eval.online --db artifacts/online.sqlite ingest YOUR_EVENTS.jsonl --random-rate 0.2`。事件契约见 [设计文档](docs/DESIGN.md)。延迟业务结果通过 Python API `OnlineStore.annotate(episode_id, outcome=..., review=...)` 回填。

## 如何看结果

| 文件 | 用途 |
|---|---|
| `artifacts/demo/offline.json` | 基线与候选的逐案例结果、指标、门禁理由和数据/代码指纹 |
| `artifacts/demo/blocked.json` | 越权策略的阻断证据 |
| `artifacts/demo/simulation.json` | 重复执行结果及工具事件 |
| `artifacts/demo/online.json` | 随机质量估计、风险发现、待成熟与未知标签 |
| `artifacts/demo/promotion.json` | 回流数量、重复样本及拒绝原因 |
| `artifacts/demo/regression.jsonl` | 通过审核、有独立 oracle 的失败回归案例 |
| `artifacts/demo/loop-validation.json` | 回流案例上的基线与修复版本复测 |

候选策略在 13 个演示场景中得到 **11 pass、2 infra_error**。两项故障保留为未解决依赖问题，不算成功。故意越权的策略被 BLOCK。回归集不能同时用作独立的泛化证明；真实发布还需要未参与调参的留出集。

## 扩展到真实 Agent

实现 `run_case(case: dict, version: str, seed: int) -> dict`，再指定：

```bash
python -m agent_eval offline --dataset YOUR_CASES.jsonl --adapter your_package:run_case --baseline v1 --candidate v2
```

Runner 会删除 `expected_state` 和轨迹断言后再调用 adapter，防止直接泄漏评测答案。真实 adapter 应只把业务输入和必要上下文发送给 Agent，通过独立沙箱/业务 API 读取最终状态；不要把 Agent 自述塞进 `final_state`。Python 同进程不是针对恶意被测代码的隔离边界，生产执行需独立进程/容器及最小权限。

新增业务需要定义初态、可执行工具、结果验证器、关键动作约束和超时处理。多轮和多 Agent 共用 episode，工具事件保留 `agent_id`；生产还需补充 span 父子关系和状态版本。

## 当前实现的明确限制

- 状态 oracle 和规则 grader 已实现；LLM judge 调用、人工标注 UI、judge 版本桥接未实现。`calibrate_judge` 可对外部提供的人工/机器标签计算严重错误召回与弃权率。
- 仿真覆盖两个业务的离散状态与脚本策略，没有声称覆盖任意 Agent、任意用户行为或 LLM 非确定性。
- 在线存储保留脱敏事件，抽样决定评测流归属，不是按采样率减少全部存储。风险流与随机流可能重叠，不能将数量相加作为独立样本数。
- 线上成功率是成熟且已获得标签的随机样本的逆概率加权比率；缺失标签仍可能带来偏差，尚未实现置信区间、用户级聚类及 A/B 实验统计。
- 脱敏处理敏感键和邮箱文本；邮箱以每个 store 的 HMAC 伪名保留身份相等关系，但不保证域名规则等任意业务语义。它不是完备 DLP，生产需要业务脱敏适配、密钥管理、租户隔离、加密、保留期限和访问审计。
- `as_of` 过滤观测/成熟时间；回填后报告使用当前标签，不提供历史标签快照。生产需要追加式标注事件与结果修订审计。
- SQLite/JSONL 适合本地原型。回流使用单个 store 管理目标文件，文件替换与数据库提交不是跨资源分布式事务；进程崩溃后的恢复依靠目标文件语义去重。
- 未连接生产发布、灰度或回滚系统，门禁只生成决策和退出码；未实现真实厂商 API adapter。

进一步阅读：[厂商调研](docs/RESEARCH.md) · [闭环架构与实施路线](docs/DESIGN.md)。
