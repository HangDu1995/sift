# fixtures — 演示与文档素材

> ⚠️ 这里是**演示 / 文档素材**,不是 pytest 测试夹具。测试用 `tmp_path` 现造临时 skill,不依赖本目录;本目录供 [docs/DEMO.md](../docs/DEMO.md)、[docs/example-skill.md](../docs/example-skill.md) 等文档演示命令用。

## 目录速查

| 目录 | 是什么 | 演示什么 | 详细文档 |
|---|---|---|---|
| [example-skills/](example-skills/) | 4 个**结构规整**的主力示例 skill(中英) | 三条评测路径(analyze / vet / run / audit)的正面用法 | [example-skills/README.md](example-skills/README.md) · [docs/example-skill.md](../docs/example-skill.md) |
| [negative-skills/](negative-skills/) | 7 个**反面教材**(故意带缺陷) | vet / analyze / 静态检查如何识别各种 skill 缺陷 | [docs/DEMO.md 第二幕](../docs/DEMO.md) |
| [test-skills/](test-skills/) | `data-pipeline`(3-skill DAG)+ `eval-config.yaml` 配置示例 | `batch` 多 skill 依赖编排评测 + config loader 配置加载机制 | [docs/DEMO.md](../docs/DEMO.md) |

## 一句话区分

- 想看「**好 skill 长什么样、怎么评测**」→ `example-skills/`
- 想看「**坏 skill 会被查出什么问题**」→ `negative-skills/`
- 想看「**多个 skill 组成 pipeline 怎么 batch 评测**」→ `test-skills/data-pipeline/`

## negative-skills 的 7 种缺陷

| 目录 | 故意的缺陷 |
|---|---|
| `bad-format` | SKILL.md 格式损坏(无有效 frontmatter) |
| `empty-shell` | 空壳(无 description / 无实质内容) |
| `low-quality` | description 过短("干活"),质量低 |
| `bad-instruction` | 指令混乱、自相矛盾 |
| `broken-root` | 根 skill 指令会导致执行失败(用于 batch DAG 级联归因) |
| `missing-dep` | manifest 声明了不存在的依赖(配合 `manifest-broken-deps.json`) |
| `security-risk` | 含内部 API 访问等安全风险(vet 应报 BLOCKED / REVIEW_NEEDED) |

## test-skills/data-pipeline 的 DAG

```
data-validator (无依赖)
       ↓
data-transform (依赖 data-validator)
       ↓
report-generator (依赖 data-transform)
```

`batch` 按 DAG 拓扑顺序同层并行评测;`eval-config.yaml` 是 [config/loader.py](../src/skill_eval/config/loader.py) 三层加载机制(defaults → project → skill)的配置示例,不是 batch 专用。
