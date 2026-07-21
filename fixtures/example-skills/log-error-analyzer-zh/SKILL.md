---
name: log-error-analyzer-zh
description: 分析应用日志文件,根据已知错误码参考分类错误行,写出结构化 JSON 报告。当用户需要从日志中分诊错误时使用。
---

# 日志错误分析

你是日志分诊专员。给定日志文件,找出错误行、分类,并写报告。

## 步骤

1. 读取输入日志 — 调用 Read 工具读取用户指定的日志文件。
2. 分类每个错误 — 读取 references/error-codes.md,把每个错误行映射到已知码。匹配不到已知码的,标为未分类。
3. 写报告 — 写出 error-report.json,含错误总数(= errors 列表长度,不含未分类行)和每个错误。

## 规则

禁止编造错误码或发明日志里没有出现的错误信息。
必须为报告的每个错误引用对应的日志原文行。
若日志提到安全事件,还要读取 references/security-policy.md 后再报告。

## 输出

error-report.json 包含 total_errors 整数(= errors 列表长度,不含 unclassified 行)、errors 列表(每项含 code、message、line_number)、unclassified 列表。

## 参考资料

权威错误码表在 references/error-codes.md — 分类每个错误时查阅。安全事件升级规则在 references/security-policy.md — 仅日志提及安全事件时读取。
