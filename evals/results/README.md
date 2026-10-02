# 本地评测结果

本目录由评测脚本创建，用于保存报告、原始 JSONL、执行轨迹、最终补丁及截图。产物可能含本地路径、模型输出与任务代码，默认由 `.gitignore` 排除；仅此说明文件随仓库提交。

从仓库根目录运行 `evals/run_clarification.py`、`evals/run_code_tasks.py` 或 `evals/run_subagent_smoke.py` 可生成相应报告。方法与参数见 [评测说明](../README.md)。

需要公开展示的结果，应先人工复核并脱敏，放入单独的公开文档目录。本目录不作为已发布 Benchmark 成绩来源。
