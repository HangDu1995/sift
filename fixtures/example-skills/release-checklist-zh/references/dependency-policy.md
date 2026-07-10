# 依赖策略

发版前检查依赖变更。

- 禁止引入新的高危依赖(已知 CVE、无人维护、或 license 不兼容)
- 升级 major 版本的运行时依赖必须记进 changelog
- 开发依赖(devDependencies)的变更不阻塞发布

无新增高危依赖即可放行。
