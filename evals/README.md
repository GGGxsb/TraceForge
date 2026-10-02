# TaskBrief 澄清门控评测

运行当前 TraceForge Web 配置的模型，对 `clarification_cases.json` 中的 20 个中文请求调用真实 `TaskBriefHook`。脚本不会启动完整 Agent、执行工具或修改工作区。需要调查的样本使用预设的只读工具结果进行一次复检；这些结果是受控模拟证据，并非真实 Agent 轨迹。

```powershell
& .venv\Scripts\python.exe evals/run_clarification.py --followup
```

`--followup` 会对配置了 `oracle_answer` 且已提出问题的样本，模拟用户回答后再次运行 TaskBrief。可用 `--ids case_a,case_b` 只运行指定样本。每次运行写入 `evals/results/clarification-*.json`，报告包含用例文件 SHA-256、模型、阶段、提问和耗时，不含 API Key。

评测标签由用例人工确定：10 例必须向用户提问，10 例不应提问。脚本统计的是**用例级提问决策**的 precision、recall 和 F1；这些指标不等于逐条问题的语义精确率，也不等于端到端代码任务成功率。问题是否包含多余内容、语言是否合适，需另行复核。

报告保留在本地 `results/`，不随仓库提交。需要复现当前门控表现时，固定模型配置与用例文件后独立重复运行。

另有一组 16 条未用于修改 TaskBrief 提示词的[留出用例](clarification_holdout.json)，覆盖不同措辞、外部业务决策和不充分回答：

```powershell
& .venv\Scripts\python.exe evals/run_clarification.py --cases evals/clarification_holdout.json --followup
```

不要依据留出集输出直接修改原始标签；有争议的样本单独人工裁定，并用新样本验证修复。

## 代码任务基线

`run_code_tasks.py` 默认运行 7 个跨模块任务（配置优先级、稳定游标分页、原子 CSV 导入、依赖构建顺序、幂等重试、TTL 缓存、原子结算）。`--suite smoke` 运行原有 3 个小任务，`--suite all` 运行全部任务。每题从固定 Git 提交开始，隐藏测试不会出现在 Agent 工作区。评分器在干净的同源仓库应用 Agent 最终文件变化，再分别运行隐藏测试和现有测试；无关文件改动会判失败。

先自检题目。自检会确认空补丁不能通过、参考实现能通过隐藏测试与现有测试；仅此模式在沙箱缺失时允许用本机 Python 执行仓库内固定 fixture，绝不执行 Agent 产出的代码：

```powershell
& .venv\Scripts\python.exe evals/run_code_tasks.py --suite complex --validate-only --output evals/results/complex-fixtures.json
```

Docker Desktop Linux Engine 或 Linux Bubblewrap 可用、且模型已配置时运行正式基线：

```powershell
& .venv\Scripts\python.exe evals/run_code_tasks.py --suite complex --repeats 3
```

可用 `--task atomic_checkout` 只运行一道题，`--timeout 900` 设置每次运行时限。每次运行独立重建仓库和会话；`evals/results/code-tasks-*.json` 保存题目哈希、固定起始 commit、逐次是否通过、失败类别、耗时、token、工具/测试调用和审批。与报告同名的目录保存每次运行的 `session.jsonl`、`trajectory.json`、`patch.diff`，可据此分析定位、修改、验证及失败恢复。报告每完成一次运行就落盘，长批次中断后已完成的结果仍在。

如需补足重复次数，使用 `--resume 已有报告.json --repeats 3 --timeout 原报告时限`；脚本核对题目哈希、起始提交、模型、上下文窗口、沙箱和权限后，仅运行缺少的次数。如果发现隐藏验收过度约束，可以先修题，再用 `--regrade 原报告.json` 在干净仓库对保存的补丁重新评分，保留原始轨迹且不调用模型。原始报告不会被覆盖。

用离线分析器生成逐题汇总和需复核的轨迹信号：

```powershell
& .venv\Scripts\python.exe evals/analyze_code_results.py evals/results/code-tasks-YYYYMMDD-HHMMSS.json
```

主指标是隐藏验收与现有测试同时通过的运行比例，以及每题重复运行全部通过的数量。只对通过的运行比较耗时和 token；轨迹指标用于解释差异，不单独决定任务成功。对最终表现做判断前，固定模型、上下文窗口、权限、预算、题目 SHA 和沙箱环境，并人工复核有争议的失败。当前 7 题是初始回归基线，不代表公开 Benchmark 成绩或真实项目总体成功率。

后续应扩充真实仓库、长链路和跨会话任务。在比较成功率之外，分析对已有文件误用 `create_file`、读取不存在的 handoff、Git ownership 等可避免的工具错误，并在保持质量的同时降低用量。公开成绩应附经过复核和脱敏的可复现结果，不能以本地一次运行代表真实项目表现。

## Sub-Agent 历史回查

`run_subagent_smoke.py` 用当前网页配置的真实模型执行只读历史子任务。临时项目包含互相冲突的两个会话分支（放弃的 SQLite/滚动摘要方案与最终 JSONL/handoff 决定），检查子 Agent 是否读到并引用最终原始节点。此脚本直接发起已持久化的委派，验证子循环与结构化回传；不评估主 Agent 是否会自主选择委派，也不能代表复杂任务的整体收益。

```powershell
& .venv\Scripts\python.exe evals/run_subagent_smoke.py --output evals/results/subagent-history-smoke.json
& .venv\Scripts\python.exe -m pytest backend/tests/test_subagents.py -q
$env:TRACEFORGE_TEST_DOCKER='1'
& .venv\Scripts\python.exe -m pytest backend/tests/test_subagents.py backend/tests/test_tool_recovery.py -q
```

真实模型报告保存检查结果、引用、工具序列、用量及父/子/来源 JSONL。单元测试另外覆盖父子通信、推理回传、同项目范围、伪造引用、完全访问权限隔离、未提交代码快照、敏感 diff、报告长度、取消/超时/重启恢复和删除；Docker 可选测试验证真实只读挂载与断网。Linux Bubblewrap 的参数边界有自动测试，仍需在 Linux 环境运行实际隔离测试。

## 产物与提交

默认输出目录为 `evals/results/`，只提交其中的 [目录说明](results/README.md)。报告、截图、补丁和原始会话日志由 Git 忽略，本地文件仍可用于 `--resume`、`--regrade` 或轨迹分析。自定义 `--output` 到其他目录时，请同时配置对应的忽略规则。公开结果应先脱敏后放入独立文档目录。
