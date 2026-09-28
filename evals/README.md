# TaskBrief 澄清门控评测

运行当前 TraceForge Web 配置的模型，对 `clarification_cases.json` 中的 20 个中文请求调用真实 `TaskBriefHook`。脚本不会启动完整 Agent、执行工具或修改工作区。需要调查的样本使用预设的只读工具结果进行一次复检；这些结果是受控模拟证据，并非真实 Agent 轨迹。

```powershell
& .venv\Scripts\python.exe evals/run_clarification.py --followup
```

`--followup` 会对配置了 `oracle_answer` 且已提出问题的样本，模拟用户回答后再次运行 TaskBrief。可用 `--ids case_a,case_b` 只运行指定样本。每次运行写入 `evals/results/clarification-*.json`，报告包含用例文件 SHA-256、模型、阶段、提问和耗时，不含 API Key。

评测标签由用例人工确定：10 例必须向用户提问，10 例不应提问。脚本统计的是**用例级提问决策**的 precision、recall 和 F1；这些指标不等于逐条问题的语义精确率，也不等于端到端代码任务成功率。问题是否包含多余内容、语言是否合适，需另行复核。

当前修复后的重复运行结果见 [clarification-after-fixes.md](results/clarification-after-fixes.md)。

另有一组 16 条未用于修改 TaskBrief 提示词的[留出用例](clarification_holdout.json)，覆盖不同措辞、外部业务决策和不充分回答：

```powershell
& .venv\Scripts\python.exe evals/run_clarification.py --cases evals/clarification_holdout.json --followup
```

留出集的两次结果见 [clarification-holdout-summary.md](results/clarification-holdout-summary.md)。不要依据这组输出直接修改原始标签；有争议的样本单独人工裁定，并用新样本验证修复。
