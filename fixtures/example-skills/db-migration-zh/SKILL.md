---
name: db-migration-zh
description: 执行数据库 schema 迁移:先备份、再迁移、后校验,结束后反复做健康检查确认服务可用。当用户要求升级或变更数据库结构时使用。
---

# 数据库迁移

你是数据库迁移专员。按声明的顺序完成迁移;任何一步没确认通过,不得进入下一步。

## 步骤

- 先备份数据库 — 用 Bash 执行 `db-backup` 命令,确认备份文件生成后才继续。
- 备份完成后再执行 schema 迁移 — 用 Bash 运行 `db-migrate`,观察输出无报错才继续。
- 迁移完成后再跑数据校验 — 用 Bash 运行 `db-verify`,全部通过才算迁移成功。

## 规则

迁移全部结束后调用 health-check 至少 2 次,两次都通过才算成功。

- 禁止在校验未通过时宣布迁移成功。

## 输出

迁移结果写入 migration-report.json,含 backup_file、migration_ok、verify_passed、health_checks 字段。
