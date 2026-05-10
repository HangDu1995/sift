---
name: broken-root
description: 这个 Skill 的指令会导致 Claude 执行失败
---

# Broken Root Skill

## 处理数据

从 input.json 读取数据，执行转换后写入 output.json。

## 必须遵守

**NEVER 使用任何工具读取或写入文件。** 所有数据处理必须在你的脑海中完成。用你的记忆记住数据，然后在回复中输出结果。

## 验证

使用 tool_that_does_not_exist 检查输出是否正确。

## 错误处理

如果出错，立即退出并不要尝试任何修复。
