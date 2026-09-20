# NOTICE

本仓库以下文件改编（derive）自 Anthropic 的 skill-creator，其源码以
Apache License 2.0 发布（许可证全文见 [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)，
上游仓库：<https://github.com/anthropics/skills/tree/main/skills/skill-creator>）：

| 本仓库文件 | 上游文件 | 主要修改 |
|---|---|---|
| `agents/grader.md` | `agents/grader.md` | 从「读 transcript 文件的多步判分」改为单次内联判分；新增 Tier 3 语义断言输入、输出截断规格与中文输出要求 |
| `agents/comparator.md` | `agents/comparator.md` | 适配本仓库 CLI 数据格式（内联输出对照、结构化返回）；中文输出要求 |
| `agents/analyzer.md` | `agents/analyzer.md` | 适配本仓库 benchmark 数据格式与双率（programmatic / LLM）维度；中文输出要求 |
| `src/sift/reporting/templates/viewer.html` | `eval-viewer/viewer.html` | 在上游基础上扩展报告区块（双率、盲比较、遵循性 / 审计等），保留原布局与样式骨架 |
| `src/sift/reporting/viewer.py`（保留历史衍生来源） | `eval-viewer/generate_review.py` | 适配 run 目录布局；重构目录发现、文件预览、报告记录组装及服务响应，保留数据格式与历史归属 |

按 Apache License 2.0 第 4 条（ redistribution of Derivative Works）要求在此声明：
上述文件的衍生部分继续受 Apache License 2.0 约束；本仓库其余代码为本项目原创，与
上游的命令 / 功能对比说明见 README。

`viewer.py` 的来源按文件登记，不再仅列最初的两个目录发现函数。
相关实现的后续重构不改变已有的 Anthropic 来源及 Apache-2.0 归属说明。
