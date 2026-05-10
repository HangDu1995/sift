# Skill-Eval

**Skill-Eval** 是一个独立 Python CLI 工具，用于对 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 的 Skill（以 `SKILL.md` 为核心的技能定义文件）进行自动化评测。

它将 Skill 开发从基于直觉的"艺术"转变为**可度量、可迭代、可回归验证**的工程实践。

---

## 快速开始

```bash
# 安装
pip install -e .

# 安全审查（无需 API，<1s）
skill-eval vet path/to/my-skill/

# 静态分析（无需 API）
skill-eval analyze path/to/my-skill/

# L0 覆盖率扫描
skill-eval scan path/to/skills-dir/

# LLM 生成配置（需要 claude CLI）
skill-eval generate-config path/to/skills-dir/ --output eval-config.yaml

# 完整评测（含盲比较 + 深度分析）
skill-eval full path/to/my-skill/ --runs 3
```

**前置条件**：Python 3.10+，Claude Code CLI >= 2.1.0, < 2.2.0（仅动态评测需要）

### 演示

```bash
skill-eval full fixtures/test-skills/data-validator/ --runs 1
# 产出: benchmark.json (delta +0.47) + comparison-run1.json (盲比较) + analysis-notes.json (分析笔记) + report.html
```

> 完整演示指南见 [`docs/DEMO.md`](docs/DEMO.md) —— 包含指标解读、正面案例和 6 个反面教材。

---

## 安装

```bash
git clone <repo-url> && cd skill-eval
pip install -e ".[dev]"
skill-eval --version
```

依赖：`typer>=0.9` / `rich>=13` / `pydantic>=2.0` / `pyyaml>=6.0`

### Claude CLI 版本要求

skill-eval 通过 `claude -p`（Agent SDK / headless 模式）调用 Claude Code CLI，依赖以下 CLI flags：

| Flag | 引入版本 | 用途 |
|------|----------|------|
| `--output-format {text,json,stream-json}` | < 1.0 | 结构化输出解析 |
| `--verbose` | < 1.0 | 完整 turn-by-turn 输出 |
| `--dangerously-skip-permissions` | < 1.0 | 无人值守执行 |
| `--max-turns` | < 1.0 | 限制 agentic turns |
| `--disable-slash-commands` | 2.0.60 | 隔离 without_skill 基线 |
| `--include-partial-messages` | 1.0.109 | 流式 token delta |
| `--model` | < 1.0 | 模型选择 |

**支持范围**：`>= 2.1.0, < 2.2.0`

- **2.1.0**（2026-01-07）：首个 2.1.x 稳定版，所有必需 flags 均可用
- **< 2.2.0**：当前测试范围；超出此版本会打印兼容性警告但仍尝试运行
- 版本检查在启动时自动执行（`run`、`full`、`batch`、`optimize-trigger`、`generate-config` 命令）

安装 Claude Code：[官方文档](https://docs.anthropic.com/en/docs/claude-code/overview) | [CLI 参考](https://docs.anthropic.com/en/docs/claude-code/cli-usage) | [Changelog](https://docs.anthropic.com/en/docs/claude-code/changelog)

可选安全扫描器：[aguara](https://github.com/garagon/aguara)（Go）、[skill-scanner](https://pypi.org/project/cisco-ai-skill-scanner/)（pip），未安装时自动跳过。

---

## CLI 用法

### `vet` — 安全审查

```bash
skill-eval vet path/to/skill/                # 终端表格输出
skill-eval vet path/to/skill/ --format json  # JSON 输出（CI 友好）
skill-eval vet path/to/skill/ --skip-external # 跳过外部扫描器
```

4 个扫描器（2 内置 + 2 可选），判定 SAFE / REVIEW_NEEDED / BLOCKED。

### `analyze` — 静态分析

```bash
skill-eval analyze path/to/skill/                # 11 项检查 + 分类
skill-eval analyze path/to/skill/ --format json  # JSON 输出
```

### `init` — 初始化评测定义

```bash
skill-eval init path/to/skill/                  # 自动生成 evals.json
skill-eval init path/to/skill/ --generate-prompts  # LLM 生成 prompt
```

### `scan` — L0 覆盖率扫描

```bash
skill-eval scan path/to/skills-dir/
skill-eval scan path/to/skills-dir/ --domain frontend --type create
```

### `generate-config` — LLM 配置生成

```bash
skill-eval generate-config path/to/skills-dir/ --output eval-config.yaml
skill-eval generate-config path/to/skills-dir/ --dry-run  # 仅扫描，不调 LLM
```

生成的 `eval-config.yaml` 包含 domain/type 模板、integration_scenarios、skill_keywords 和 pipelines。

### `run` — A/B 评测

```bash
skill-eval run path/to/skill/ --runs 3
skill-eval run path/to/skill/ --scoring programmatic  # 仅程序化评分
skill-eval run path/to/skill/ --compliance-gate       # 遵循性门控
```

### `full` — 一键完整流程

```bash
skill-eval full path/to/skill/ --runs 3
skill-eval full path/to/skill/ --security-gate --open
```

安全审查 → 静态分析 → 加载 evals → A/B 测试 → 聚合 → HTML 报告。

### `batch` — 批量评测

```bash
# 全部层
skill-eval batch path/to/skills-directory/

# 只跑特定层
skill-eval batch manifest.json --layers unit
skill-eval batch manifest.json --layers integration
skill-eval batch manifest.json --layers unit,integration

# Pipeline 控制
skill-eval batch manifest.json --layers integration --pipeline my-pipeline
skill-eval batch manifest.json --layers integration --pipeline my-pipeline --pipeline-steps step1,step2
skill-eval batch manifest.json --layers integration --project /path/to/project
skill-eval batch manifest.json --layers integration --projects proj1,proj2
```

三层测试：`unit`（独立 A/B）→ `integration`（协作 + pipeline）→ `regression`（基线对比）

### 其他命令

```bash
skill-eval report output/ --benchmark benchmark.json --open  # HTML 报告
skill-eval optimize-trigger path/to/skill/ --eval-set triggers.json  # 触发优化
```

---

## Pipeline 流水线评测

Pipeline 定义在 `eval-config.yaml` 中，在真实项目副本上按依赖顺序逐步执行 Skill。

```yaml
pipelines:
  - name: "android-build-test"
    description: "Build and test Android project"
    project: "fixtures/projects/android-medium"
    steps:
      - name: "migrate"
        skill: "arkts-migrator"
        prompt: "Migrate Java files to ArkTS"
        expectations: ["All Java files migrated"]
        assertions:
          - name: "no java files"
            type: NOT_CONTAINS
            params: { pattern: "\\.java$", path: "app/src" }
        depends_on: []
      - name: "test"
        skill: "arkts-tester"
        prompt: "Run tests on migrated code"
        depends_on: ["migrate"]
```

**核心特性**：
- **自动项目复制**（`shutil.copytree`，不污染原始项目）
- **依赖解析**（`--pipeline-steps test` 自动包含前置步骤）
- **多项目支持**（`--projects proj1,proj2`）
- **程序化生成**（`ConfigGenerator.build_pipeline_from_deps()`）

内置测试项目：`fixtures/projects/android-minimal`（~12 文件）、`fixtures/projects/android-medium`（~20 文件）

---

## 测试

```bash
pytest tests/ -v           # 242 个测试
pytest tests/ --cov=skill_eval
```

---

## 文档

| 文档 | 内容 |
|------|------|
| [演示指南](docs/DEMO.md) | 30 分钟完整演示：正面案例 + 6 个反面教材 + 指标速查 + 排错决策树 |
| [参考手册](docs/skill-eval-reference.md) | 完整 CLI 参数、配置架构、评测体系、JSON 格式、FAQ |
| [架构设计](docs/skill-eval-arch-design.md) | 设计决策、模块契约、质量公式（架构师向） |
| [实现设计](docs/skill-eval-design.md) | Pydantic 模型、函数签名、伪代码（工程师向） |
| [原理](docs/skill-eval-principles.md) | 底层原理、参考出处 |
| [测试指南](docs/cli-test-results.md) | 测试 Skill 介绍、指标解读、如何编写测试 |
| [示例报告](docs/demo-output/data-validator/report.html) | data-validator 的完整 HTML 报告示例 |
| [代码审查修复记录](docs/code-audit-fixes.md) | 七轮代码审查与修复记录（67 个问题） |

---

## License

Private — Internal Use Only
