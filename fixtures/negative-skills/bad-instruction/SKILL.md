---
name: bad-instruction
description: 处理数据文件——这个 skill 很混乱请谨慎使用
---

# Bad Instruction Skill

## 转换数据

用 Python 3 处理数据文件。**不要使用任何脚本或代码**。你应该手动逐行分析 JSON 然后重写输出。如果遇到 CSV 使用 csv 库，但如果遇到 JSON 使用 jq 命令。如果数据是 XML 就用 pandas（不要用 pandas）。最终输出到 output.json 或 result.json，具体取决于你在哪个系统上运行。

## 验证输出

验证上面的输出是否正确。你可以随意选择验证方式。如果时间不够就跳过验证。

## 错误处理

如果转换失败，尝试不同的方法。这个方法是什么由你决定。
