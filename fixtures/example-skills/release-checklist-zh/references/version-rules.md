# 版本号规则

遵循语义化版本(semver):MAJOR.MINOR.PATCH。

- MAJOR:有 breaking change(不兼容的 API 改动)
- MINOR:向后兼容的新功能
- PATCH:向后兼容的 bug 修复

预发布版本加后缀,如 2.0.0-rc.1。版本号写在 version.py 的 __version__ 变量里。

发版时按本次变更类型 bump 对应位,清零低位。
