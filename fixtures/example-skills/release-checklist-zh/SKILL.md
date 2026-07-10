---
name: release-checklist-zh
description: 发布前完整自检——跑测试、查 lint、检查依赖、生成 changelog、更新版本号、打 tag。当用户准备发版时使用。
---

# 发布前自检

你是发布工程师。发布前必须完成完整自检流程,任一环节不过都不允许发布。

## 流程

1. 跑测试 — 调用 Bash 运行 pytest,全绿才能继续。若有失败,停止发布并报告失败用例。
2. 查 lint — 调用 Bash 运行 ruff check,修复所有错误。警告可忽略。
3. 检查依赖 — 读取 references/dependency-policy.md,确认无新增高危依赖。
4. 生成 changelog — 读取 references/changelog-format.md,按格式生成 CHANGELOG.md 本次条目。
5. 更新版本号 — 读取 references/version-rules.md,按语义化版本规则更新 version.py。
6. breaking change 审批 — 若本次有 breaking change,必须等用户确认审批后才能继续;无 breaking change 则跳过此步。
7. 打 tag — 调用 Bash 执行 git tag v<新版本>。

## 禁令

禁止跳过测试直接发布。
禁止直接 push 到 main 分支,必须走 PR。
禁止编造测试结果或谎报 lint 通过。

## 版本号规则示例

版本号遵循语义化版本 MAJOR.MINOR.PATCH:bumping 规则见下。

- breaking change 时 MAJOR 加一
- 新功能时 MINOR 加一
- 仅 bug 修复时 PATCH 加一

## changelog 格式示例

发布条目按下面模板写进 CHANGELOG.md 顶部:

```
## [未发布]
### 新增
- <新功能>
### 修复
- <bug 修复>
```

## 参考资料

依赖策略在 references/dependency-policy.md — 发版前查阅。changelog 格式在 references/changelog-format.md — 生成条目时查阅。版本规则在 references/version-rules.md — 更新版本号时查阅。
