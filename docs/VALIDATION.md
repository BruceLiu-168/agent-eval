# v0.2 本地框架验收

验证日期：2026-10-05（Asia/Shanghai）。执行环境为 Linux / Python 3.12.14。此记录验证框架机制，不代表真实业务 Agent 的质量。

## 已执行的检查

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -q` | 175 项通过 |
| 构建 wheel，并在全新 venv 中无依赖安装 | 通过，运行时没有第三方依赖 |
| 安装后从 `/tmp` 执行 `agent-eval self-test` | Python、HTTP、command 各 26 条记录，三种恢复验收均通过 |
| 安装后 `init → validate → run → resume --enforce-gate` | 生成可读报告，恢复保留同一 Run ID 和 26 条记录，门禁退出码 3 |
| 安装后旧版 `demo` 完整飞轮 | 8 条经审核失败回流，回归候选复测；严重回归策略 BLOCK |
| 数据契约与评分失败路径 | 非法 JSON/类型、缺证据、评分超时、regex 超时、日志污染及无效模型响应均有测试 |
| 真实接入的执行边界 | HTTP/命令/Python 超时、无重试、答案字段白名单、协议身份校验均有测试 |
| 实验恢复 | 并发锁、记录校验和、代码/数据变化、已完成 checkpoint 丢失拒绝重跑均有测试 |
| 报告与导入 | 缺失数据覆盖、指标缺失、HTML 转义、CSV 公式处理、多版本输出型评分均有测试 |
| 线上回流 | 延迟标注、成熟窗口、审核、脱敏、幂等与输出型用例回流均有测试 |

构建命令：

```bash
python -m pip wheel . --no-deps --no-build-isolation -w /tmp/agent-eval-dist
python -m venv /tmp/agent-eval-validation-venv
/tmp/agent-eval-validation-venv/bin/python -m pip install --no-index --no-deps /tmp/agent-eval-dist/agent_eval_local-0.2.0-py3-none-any.whl
cd /tmp
/tmp/agent-eval-validation-venv/bin/agent-eval self-test
```

`--no-build-isolation` 使用验收环境已有的 setuptools/wheel；普通用户按 README 的 `pip install .` 即可。项目提供 GitHub Actions 的 Python 3.10/3.12 矩阵；远端 CI 状态应以 GitHub 实际结果为准，不与上述本地结果混写。

## 合成结果

三种适配方式均使用相同 13 个退款/权限任务和两个版本。候选结果为 11 pass、0 fail、0 unknown、2 infra_error。两项基础设施故障仍保留，门禁为 INCONCLUSIVE；不能把 11 个可判定任务的全部通过说成整体 100% 成功。

测试 Agent 还支持 `support_routing` 的输出型评测。HTTP 服务运行于本地，没有调用真实 LLM。LLM judge 使用 mock transport 验证接口、错误和证据处理，未用付费模型衡量评分准确率；真实 judge 仍需用户用独立人工标签校准。

验收示例产物位于工作区 `artifacts/v02-local-project/runs/first/` 和 `artifacts/v02-loop/`。这些文件被 Git 忽略；克隆项目后可用 README 命令复现。

## 尚需业务方输入的部分

接入真实 Agent、标注真实业务 Case，并由独立观察器提供业务状态/标签后，才能获得真实业务成功率、真实成本和时延。生产服务的版本、知识快照、数据划分和任务家族需要由接入方固定。托管采集、多租户权限、人工审核 UI 和灰度发布不属于本次本地框架交付范围。
