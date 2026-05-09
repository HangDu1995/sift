# Skill-Eval 参考手册

本文档包含 Skill-Eval 的完整技术细节。README 仅保留项目介绍和用法速查。

---

## 目录

- [CLI 命令详细参数](#cli-命令详细参数)
  - [vet — 安全审查](#vet--安全审查)
  - [init — 初始化评测定义](#init--初始化评测定义)
  - [analyze — 静态分析](#analyze--静态分析)
  - [scan — L0 覆盖率扫描](#scan--l0-覆盖率扫描)
  - [generate-config — LLM 配置生成](#generate-config--llm-配置生成)
  - [run — A/B 评测](#run--ab-评测)
  - [full — 一键完整流程](#full--一键完整流程)
  - [batch — 批量评测](#batch--批量评测)
  - [Pipeline — 流水线评测](#pipeline--流水线评测)
- [配置驱动架构](#配置驱动架构)
- [评测体系](#评测体系)
- [JSON 输出格式](#json-输出格式)
- [项目结构](#项目结构)
- [与 Skill-Creator 的关系](#与-skill-creator-的关系)
- [Exit Code](#exit-code)
- [常见问题](#常见问题)

---

## CLI 命令详细参数

### `vet` — 安全审查

在安装或评测 Skill 之前，对其目录进行多扫描器安全检查。集成自 [skill-vetter](https://github.com/app-incubator-xyz/skill-vetter) 的安全扫描逻辑。

```bash
# 基本用法（终端表格输出）
skill-eval vet path/to/skill/

# JSON 输出（CI 友好）
skill-eval vet path/to/skill/ --format json

# 保存结果到文件
skill-eval vet path/to/skill/ --format json -o security-report.json

# 跳过外部扫描器（仅运行原生扫描器）
skill-eval vet path/to/skill/ --skip-external
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` | 无 | 输出文件路径 |
| `--skip-external` | 关闭 | 跳过外部扫描器（aguara、skill-scanner） |

**四个扫描器**：

| # | 扫描器 | 依赖 | 检查内容 |
|---|--------|------|----------|
| 1 | aguara | 可选（Go） | Prompt 注入、混淆、可疑 LLM 调用 |
| 2 | skill-scanner | 可选（pip） | 已知恶意模式、CVE 漏洞库 |
| 3 | secrets-scan | 内置 | 硬编码 API Key / Token / 凭证、Base64 编码字符串 |
| 4 | structure-check | 内置 | 缺失 SKILL.md、缺少 YAML frontmatter、危险 shell 命令 |

> 内置扫描器始终可用，无需额外安装。外部扫描器缺失时自动 SKIP。

**判定规则**：

| Verdict | 含义 | Exit Code |
|---------|------|-----------|
| **SAFE** | 所有扫描器通过 | 0 |
| **REVIEW_NEEDED** | 存在中等风险发现（MEDIUM） | 0 |
| **BLOCKED** | 存在严重风险发现（CRITICAL/HIGH） | 1 |

**输出示例**：

```
                           Security Scan: sample-skill
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Scanner         ┃ Status ┃ Detail                                     ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ aguara          │ SKIP   │ aguara not installed                       │
│ skill-scanner   │ SKIP   │ skill-scanner not installed                │
│ secrets-scan    │ PASS   │ No hardcoded secrets found                 │
│ structure-check │ PASS   │ Skill structure valid                      │
└─────────────────┴────────┴────────────────────────────────────────────┘

VERDICT: SAFE — All scanners passed
```

### `init` — 初始化评测定义

根据 SKILL.md 内容自动生成 `evals.json` 模板，作为 A/B 评测的输入。

**工作原理**：

1. 解析 SKILL.md 的 `##` 二级标题，每个标题对应一个 eval case（最多 50 个）
2. 从标题下的列表项中提取 expectations
3. 从代码块中检测可能的输出文件，生成基础 assertions
4. 若传入了 `EvalConfig`（即项目根目录存在 `eval-config.yaml`），自动从 `domain_templates` 注入领域断言，与基础 assertions 去重合并
5. 根据 description 中的关键词推断动作类型（create/convert/build/...）
6. 若 SKILL.md frontmatter 包含 `modes` 字段，为每个 mode 生成独立的 eval case（如 `[mode-a]`、`[mode-b]`）

生成的 `evals.json` 可以手动编辑——调整 prompt、增删 expectations、添加 assertions，然后再运行 `run` 或 `full`。

### `analyze` — 静态分析

对 SKILL.md 做 11 项自动化体检，**零 API 成本，<1s 完成**。

**11 项检查**：

| # | 检查项 | 说明 |
|---|--------|------|
| 1 | `description_length` | 描述长度 ≥ 20 字符 |
| 2 | `description_has_trigger` | 描述包含动作触发词 |
| 3 | `description_third_person` | 描述使用第三人称 |
| 4 | `name_kebab_case` | name 符合 kebab-case 格式 |
| 5 | `name_max_64` | name 长度 ≤ 64 字符 |
| 6 | `name_no_reserved` | name 非系统保留字 |
| 7 | `skill_md_max_500_lines` | SKILL.md 不超过 500 行 |
| 8 | `has_examples` | 包含示例代码或用法 |
| 9 | `reference_depth` | references/ 目录保持扁平 |
| 10 | `anti_fabrication` | 包含反捏造（anti-fabrication）指引 |
| 11 | `source_documented` | 引用了参考来源 |

输出还包含 Skill 分类：`CAPABILITY_UPLIFT`（增强型）或 `ENCODED_PREFERENCE`（编码偏好型）。

### `scan` — L0 覆盖率扫描

**检查项**：

| 检查项 | 说明 |
|--------|------|
| evals.json 存在性 | Skill 目录下是否有 evals/evals.json |
| skill_name 完整性 | evals.json 的 skill_name 非空 |
| prompt 完整性 | 每个 eval 的 prompt 非空 |
| assertion 类型合法性 | 断言类型在 9 种内置类型范围内 |
| 断言覆盖率 | 有 assertions 的 eval 占比 |

### `generate-config` — LLM 配置生成

**工作流程**：

1. `scan_skills_metadata()` — 扫描所有 SKILL.md frontmatter，提取 name/type/domain/description/tags
2. `ConfigGenerator.build_prompt()` — 将元数据 + 9 种可用断言类型组合成 prompt
3. `claude -p` — LLM 生成 eval-config.yaml 内容
4. 写入项目根目录

**生成的 `eval-config.yaml` 包含**：

```yaml
domain_templates:
  frontend:
    - name: "component structure valid"
      type: "CROSS_FILE_CONSISTENCY"
      params: {}
  backend:
    - name: "API response is valid JSON"
      type: "JSON_HAS_FIELD"
      params: { "filename": "response.json", "field": "status" }

type_templates:
  create:
    - name: "output compiles"
      type: "CODE_COMPILES"
      params: {}
  refactor:
    - name: "no regressions"
      type: "NOT_CONTAINS"
      params: { "pattern": "ERROR" }

integration_scenarios:
  - name: "create then review"
    type: "chain"
    skills: ["create-skill", "review-skill"]
    steps: [...]

skill_keywords:
  create-skill: ["generate", "scaffold"]
  review-skill: ["review", "audit"]

pipelines:
  - name: "frontend-full-flow"
    description: "Chain all frontend skills"
    project: "fixtures/projects/android-medium"
    steps:
      - name: "build-ui"
        skill: "ui-builder"
        prompt: "Build the UI components"
        expectations: ["Components created"]
        assertions:
          - name: "files exist"
            type: FILE_EXISTS
            params: { filename: "ui.ts" }
        depends_on: []
      - name: "test-ui"
        skill: "ui-tester"
        prompt: "Test the UI"
        expectations: ["Tests pass"]
        assertions: []
        depends_on: ["build-ui"]
```

> **核心理念**：框架代码零硬编码，所有领域/类型模板通过配置注入。

### `run` — A/B 评测

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找 | evals.json 路径（默认查找 skill 目录下） |
| `--runs` | 3 | 每种配置的运行次数 |
| `--model` | 用户默认模型 | claude -p 使用的模型 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--timeout` | 120 | 每次运行超时（秒） |
| `--max-turns` | 10 | claude -p 最大 agentic 轮数 |
| `--scoring` | hybrid | 评分模式：hybrid / programmatic / llm |
| `--compliance-gate` | 关闭 | 遵循性率 < 0.5 时强制失败 |
| `--security-gate` | 关闭 | 安全审查 BLOCKED/REVIEW_NEEDED 时强制失败（在评测前运行） |
| `--no-calibration` | 关闭 | 跳过评分校准 |
| `--output` | output/ | 输出目录 |
| `--format` | table | 输出格式：table / json |

**输出目录结构**：

```
output/iteration-1/
  eval-1/
    eval_metadata.json
    with_skill/
      run-1/outputs/, timing.json, grading.json
      run-2/...
    without_skill/
      run-1/outputs/, timing.json, grading.json
      run-2/...
  eval-2/...
  benchmark.json
```

### `full` — 一键完整流程

自动执行：安全审查（可选门控）→ 静态分析 → 加载 evals → A/B 测试 → 聚合 → 生成报告。

**参数**（`full` 特有）：

| 参数 | 说明 |
|------|------|
| `--security-gate` | 在评测前运行安全审查，BLOCKED 或 REVIEW_NEEDED 时立即失败（exit code 1） |
| `--compliance-gate` | 遵循性率 < 0.5 时强制失败 |
| `--open` | 运行完成后在浏览器中打开报告 |

### `batch` — 批量评测

**Manifest 格式**：

```json
{
  "name": "my-project-skills",
  "skills": [
    {"path": "skills/data-transform", "depends_on": []},
    {"path": "skills/report-gen", "depends_on": ["skills/data-transform"]},
    {"path": "skills/legacy-handler", "depends_on": [], "disabled": true}
  ],
  "integration_evals": []
}
```

**三层测试**：

| 层 | 测试内容 | 需要什么 | 产出 |
|----|----------|----------|------|
| `unit` | 每个 Skill 独立 A/B 测试 | Skill 目录 + evals.json | unit-pass-rate per skill |
| `integration` | Skill 间协作（chain/routing/contract/pipeline） | eval-config.yaml 的 `integration_scenarios` + `pipelines` | integration-report.json |
| `regression` | 与上次 benchmark 逐 Skill 对比 | `--baseline` 上次的 batch-benchmark.json | regression-delta.json |

**级联失败归因**：当上游 Skill 失败时，下游 Skill 会被标记为 `CASCADE` 而非 `ROOT_CAUSE`，帮助快速定位真正的故障源头。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--layers` | unit,integration,regression | 测试层，逗号分隔 |
| `--pipeline` | 无 | 只运行指定名称的 pipeline |
| `--pipeline-steps` | 无 | 只运行指定步骤（逗号分隔，自动解析依赖） |
| `--project` | 无 | 覆盖 pipeline 的 project 路径 |
| `--projects` | 无 | 覆盖 pipeline 的多个 project 路径（逗号分隔） |

### `Pipeline` — 流水线评测

Pipeline 是 `eval-config.yaml` 中的顶层配置，定义一条有序的 Skill 执行链，在真实项目副本上逐步执行。

**核心概念**：

- **project**：被操作的项目（如 Android 工程），每个 pipeline 指定一个或多个
- **step**：一个 Skill 执行步骤，包含 skill 名称、prompt、expectations、assertions
- **depends_on**：步骤间的依赖关系，上游输出作为下游输入
- **自动依赖解析**：选择执行某个 step 时，会自动包含其所有传递依赖

**执行流程**：

```
1. 复制项目到 workspace（shutil.copytree，不污染原始项目）
2. 按依赖拓扑排序执行 step
3. 每个 step：
   - 检查依赖 step 是否已通过
   - 在项目副本上执行 Skill（通过 claude -p）
   - 检查 assertions
   - 下一个 step 看到上游 step 的输出
4. 支持 pipeline 级和 step 级的项目覆盖
```

**配置示例**：

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
        expectations: ["Tests pass"]
        assertions:
          - name: "test results exist"
            type: FILE_EXISTS
            params: { filename: "test-results.json" }
        depends_on: ["migrate"]

  - name: "multi-project-pipeline"
    description: "Run on multiple projects"
    projects:
      - path: "fixtures/projects/android-minimal"
      - path: "fixtures/projects/android-medium"
        expectations: ["Medium project specific"]
    steps:
      - name: "lint"
        skill: "linter"
        prompt: "Run lint checks"
        expectations: []
        assertions: []
        depends_on: []
```

**CLI 用法**：

```bash
# 运行所有 pipeline（通过 batch --layers integration）
skill-eval batch manifest.json --layers integration

# 只运行特定 pipeline
skill-eval batch manifest.json --layers integration --pipeline android-build-test

# 只运行特定步骤（自动解析依赖）
skill-eval batch manifest.json --layers integration --pipeline android-build-test --pipeline-steps test

# 覆盖项目路径
skill-eval batch manifest.json --layers integration --project /path/to/my-project

# 覆盖多个项目
skill-eval batch manifest.json --layers integration --projects proj1,proj2,proj3
```

**程序化生成**：

`ConfigGenerator.build_pipeline_from_deps()` 可以从 Skill 列表和依赖图自动生成 pipeline 配置：

```python
from skill_eval.config.generator import ConfigGenerator

skills = [
    {"name": "migrator", "domain": "android"},
    {"name": "builder", "domain": "android"},
    {"name": "tester", "domain": "android"},
]
dep_graph = {
    "builder": ["migrator"],
    "tester": ["builder"],
}
pipelines = ConfigGenerator.build_pipeline_from_deps(
    skills,
    dep_graph=dep_graph,
    project="fixtures/projects/android-medium",
)
```

**测试项目**：

框架内置两个测试项目（`fixtures/projects/`）：

| 项目 | 规模 | 用途 |
|------|------|------|
| `android-minimal` | ~12 文件 | 烟雾测试 |
| `android-medium` | ~20 文件（Retrofit, ViewBinding 等） | 真实场景测试 |

---

## 配置驱动架构

Skill-Eval 是**通用框架**，不包含任何领域特定的断言模板、domain 定义或 skill 关键词。所有项目特定内容通过 `eval-config.yaml` 注入。

### 三层配置系统

```
config/defaults.yaml          ← 框架内置，通用默认值
    ↓ 深度合并
<project>/eval-config.yaml    ← 项目根目录，领域特定配置（由 LLM 生成）
    ↓ 深度合并
<skill>/eval-config.yaml      ← skill 目录内（可选），局部覆盖
```

### 插件机制

领域特定断言（如 ArkTS 结构检查）不内置在框架中，通过 `CUSTOM_PLUGIN` + `AssertionPluginRegistry` 实现：

```python
from skill_eval.scoring.plugins import AssertionPluginRegistry
from skill_eval.scoring.programmatic import ProgrammaticChecker

registry = AssertionPluginRegistry()
registry.register("ARKTS_STRUCTURE_VALID", my_arkts_checker)
checker = ProgrammaticChecker(plugin_registry=registry)
```

### 断言模板库

`AssertionTemplateLibrary` 从 `eval-config.yaml` 的 `domain_templates` 和 `type_templates` 读取模板，`EvalInitGenerator` 生成 evals 时自动注入领域相关断言。

```python
from skill_eval.scoring.templates import get_default_assertions

assertions = get_default_assertions(domain="frontend", type="create", config=eval_config)
```

### 工作流程

```
1. skill-eval generate-config <skills_dir>
   → 扫描所有 SKILL.md 元数据
   → claude -p 生成 eval-config.yaml

2. skill-eval init <skill_path>  (读取 eval-config.yaml)
   → 自动注入 domain/type 断言模板

3. skill-eval scan <skills_dir>  (读取 eval-config.yaml)
   → 检查覆盖率 + 配置前缀过滤

4. skill-eval batch <manifest> --layers integration
   → 运行 eval-config.yaml 中的 integration_scenarios + pipelines
```

---

## 评测体系

### 三层质量保障

系统围绕三个独立维度评估 Skill 质量，三者独立运行，结果汇入同一个 benchmark：

```
┌─────────────────────────────────────────────────────────────┐
│                    Skill 质量评估                            │
├─────────────┬─────────────────┬─────────────────────────────┤
│  评分引擎    │  遵循性引擎      │  评分校准                   │
│ (Scoring)   │  (Compliance)   │  (Calibration)              │
│             │                 │                             │
│ 输出正确？   │  过程合规？      │  评分可信？                  │
│             │                 │                             │
│ grading.json│ compliance.json │ calibration.json            │
└─────────────┴─────────────────┴─────────────────────────────┘
```

| 场景 | 评分 | 遵循性 | 正确判断 |
|------|------|--------|----------|
| 输出正确且过程合规 | PASS | PASS | Skill 真正有效 |
| 输出正确但过程违规 | PASS | FAIL | 输出正确是巧合，不能归功于 Skill |
| 输出错误但过程合规 | FAIL | PASS | Skill 逻辑正确但执行失败，需改进 |

### 量化公式

```
Score = StaticCompliance(0-11)
      + DeltaPassRate(%)
      + InstructionCompliance(%)
      + TriggerAccuracy(%)
      - PerformancePenalty(TimeDelta × TokenDelta)
      × CalibrationGrade(HIGH=1.0, MEDIUM=0.8, LOW=0.5, UNRELIABLE=0.2)
```

### 评分模式

| 模式 | 说明 | LLM 调用 | 适用场景 |
|------|------|----------|----------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分，冲突以 LLM 为准 | 是 | 通用场景 |
| `programmatic` | 仅确定性断言 | 否 | 输出结构化、可机械验证 |
| `llm` | 仅 LLM 语义评分 | 是 | 输出需要语义判断 |

### 程序化断言类型

| 类型 | 说明 | 参数 |
|------|------|------|
| `FILE_EXISTS` | 文件存在 | `filename` |
| `CONTAINS_PATTERN` | 正则匹配 | `pattern`, `path`(可选) |
| `JSON_HAS_FIELD` | JSON 字段 | `filename`, `field`（支持 `a.b.c`） |
| `LINE_COUNT` | 行数范围 | `min`/`max`(至少一个), `path`(可选) |
| `NOT_CONTAINS` | 反向匹配 | `pattern`, `path`(可选) |
| `CUSTOM_SCRIPT` | 自定义脚本 | `script`, `args`(可选) |
| `CROSS_FILE_CONSISTENCY` | 检查相对 import 是否解析 | `include`(可选, glob) |
| `CODE_COMPILES` | 基础语法检查（括号/花括号/方括号匹配） | `include`(可选, glob) |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | `plugin_name`, 其他由插件定义 |

### 测试层级

| 层级 | 测试什么 | 需要什么 | 运行方式 |
|------|----------|----------|----------|
| **L1**（单元级） | 单个 Skill 的规则理解是否正确 | evals.json + expectations | `skill-eval run/full` |
| **L2**（Mock 场景） | Skill 在有 fixture 的场景下能否正确产出文件 | evals.json + expectations + assertions + fixtures | `skill-eval run/full` |
| **L2 集成**（Integration） | 多个 Skill 之间的协作是否正确 | eval-config.yaml 的 `integration_scenarios` + `pipelines` | `skill-eval batch --layers integration` |

#### L1 测试（纯问答，验证规则理解）

L1 测试只验证 claude **是否理解了 SKILL.md 的规则**，不产生文件。只有 expectations，没有 assertions。

```json
{
  "id": 1,
  "prompt": "在 a2h-execute Stage 1 中，如果 Batch 1 有 3 个页面...",
  "level": "L1",
  "expectations": ["page_0001 和 page_0002 应该并行派发"],
  "assertions": []
}
```

#### L2 测试（Mock 场景，验证文件产出）

L2 测试预置 fixture 文件，验证 claude 执行后**是否产出了正确的文件**。同时有 expectations 和 assertions。

```json
{
  "id": 10,
  "prompt": "执行 a2h-execute，plans 目录已有文件...",
  "level": "L2",
  "fixtures": [{"path": "spec/baseline/plans/ui-plan.md", "content": "..."}],
  "expectations": ["生成了 ui-manifest.md"],
  "assertions": [
    {"name": "manifest 存在", "type": "FILE_EXISTS", "params": {"filename": "ui-manifest.md"}},
    {"name": "包含 converted", "type": "CONTAINS_PATTERN", "params": {"pattern": "converted"}}
  ]
}
```

#### L2 集成测试（Skill 间协作）

验证多个 Skill 之间的**编排正确性**——上游输出是否被下游消费、路由是否正确触发、制品是否符合契约。

**三种场景类型**：

**1. Chain（链式传递）**：

```yaml
integration_scenarios:
  - name: "data-transform → report-gen pipeline"
    type: "chain"
    chain: ["data-transform", "report-gen"]
    input: { source: "raw.csv" }
```

**2. Routing（关键词路由）**：

```yaml
integration_scenarios:
  - name: "keyword routing"
    type: "routing"
    test_prompts:
      - prompt: "把这些数据转换为 ArkTS 格式"
        should_trigger: ["data-transform"]
        should_not_trigger: ["report-gen"]

skill_keywords:
  data-transform: ["转换", "transform", "convert"]
```

**3. Contract（制品契约）**：

```yaml
integration_scenarios:
  - name: "transform output satisfies report-gen"
    type: "contract"
    producer: "data-transform"
    consumer: "report-gen"
    artifact: "workspace/transformed.json"
    checks:
      - type: "FILE_EXISTS"
      - type: "JSON_VALID"
```

### 输出文件说明

`full` 命令完成后，`output/iteration-1/` 目录结构：

| 文件 | 内容 |
|------|------|
| `merged_prompt.txt` | 合并后的完整 prompt |
| `benchmark.json` | 聚合结果（含 checkpoints 数据） |
| `report.html` | 自包含 HTML 报告 |
| `with_skill/run-1/grading.json` | 所有 expectations + assertions 的 pass/fail |
| `with_skill/run-1/checkpoints.json` | 每个 checkpoint 独立覆盖率 |
| `with_skill/run-1/compliance.json` | SKILL.md 指令遵循性检查 |
| `with_skill/run-1/calibration.json` | 评分可信度校准 |
| `with_skill/run-1/run_debug.json` | claude 完整回复 + tool_calls + timing |
| `with_skill/run-1/timing.json` | 执行耗时和 token 统计 |

---

## JSON 输出格式

所有 JSON 输出与 [skill-creator](https://github.com/user/skill-creator) 的 schema 兼容。

### evals.json（输入）

```json
{
  "skill_name": "my-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "用户的任务描述",
      "expected_output": "预期输出描述",
      "level": "L1",
      "fixtures": [],
      "expectations": ["可验证的预期语句1", "预期语句2"],
      "assertions": [
        {"name": "脚本存在", "type": "FILE_EXISTS", "params": {"filename": "output.py"}},
        {"name": "包含 json", "type": "CONTAINS_PATTERN", "params": {"pattern": "json.dump", "path": "output.py"}}
      ]
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 唯一标识，整数 |
| `prompt` | 是 | 发给 claude 的任务描述 |
| `expected_output` | 否 | 预期输出的文字描述（供 LLM 评分参考） |
| `level` | 否 | 测试层级：`L1`/`L2`/`L3`，默认 `L1` |
| `fixtures` | 否 | 前置文件列表 |
| `expectations` | 否 | 语义预期语句列表 |
| `assertions` | 否 | 程序化断言列表 |

> **expectations vs assertions**：expectations 检查 claude 的**回复文本**是否包含了某些语义内容（"说了什么"）；assertions 检查 claude 执行后**产生的文件**是否符合预期（"做了什么"）。

### checkpoints.json

```json
[
  {
    "eval_id": 1,
    "eval_name": "L1-输入验证-plans不存在",
    "passed": 3,
    "failed": 0,
    "total": 3,
    "pass_rate": 1.0,
    "expectations": [
      {"text": "检查了 spec/baseline/plans/ 目录是否存在", "passed": true, "evidence": "...", "source": "llm_batch"}
    ]
  }
]
```

### grading.json

```json
{
  "expectations": [
    {"text": "The output includes X", "passed": true, "evidence": "Found in transcript Step 3"}
  ],
  "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
  "execution_metrics": {"tool_calls": {"Read": 3, "Write": 2}, "total_tool_calls": 5},
  "timing": {"total_tokens": 84852, "duration_ms": 23332, "total_duration_seconds": 23.3}
}
```

### benchmark.json

```json
{
  "metadata": {"skill_name": "my-skill", "timestamp": "2026-01-15T10:30:00Z", "runs_per_configuration": 3},
  "runs": [...],
  "run_summary": {
    "with_skill": {"pass_rate": {"mean": 0.85, "stddev": 0.05}},
    "without_skill": {"pass_rate": {"mean": 0.35, "stddev": 0.08}},
    "delta": {"pass_rate": "+0.50", "time_seconds": "+13.0", "tokens": "+1700"}
  }
}
```

---

## 项目结构

```
skill-eval/
├── pyproject.toml
├── agents/                        # LLM prompt 模板
├── src/skill_eval/
│   ├── cli.py                     # typer CLI 入口（11 个子命令）
│   ├── models.py                  # 30+ Pydantic 数据模型
│   ├── parser.py                  # SKILL.md 解析器
│   ├── config/                    # 配置驱动架构
│   │   ├── loader.py              # 三层配置加载 + EvalConfig
│   │   ├── defaults.yaml          # 框架默认值（无领域内容）
│   │   └── generator.py           # LLM 配置生成器 + pipeline 生成
│   ├── security/                  # 安全审查（4 扫描器）
│   ├── static_analysis/           # 静态分析（11 项检查）
│   ├── scanning/                  # L0 覆盖率扫描
│   ├── evals/                     # evals.json 加载 + 生成
│   ├── execution/                 # Claude 执行 + A/B 测试
│   ├── scoring/                   # 评分引擎（9 种断言 + 插件 + 模板）
│   ├── compliance/                # 遵循性引擎
│   ├── calibration/               # 评分校准
│   ├── optimization/              # 触发优化
│   ├── reporting/                 # HTML 报告
│   ├── integration/
│   │   ├── runner.py              # L2 集成测试（chain/routing/contract）
│   │   ├── project_runner.py      # Pipeline 执行（项目复制 + 依赖解析）
│   │   └── impact.py              # 影响分析
│   └── batch/                     # 批量评测（DAG + 级联 + 调度）
├── fixtures/projects/             # 内置测试项目
│   ├── android-minimal/           # 最小 Android 项目（~12 文件）
│   ├── android-medium/            # 中等 Android 项目（~20 文件）
│   └── README.md
├── tests/                         # 216 个测试
└── docs/
```

---

## 与 Skill-Creator 的关系

Skill-Eval 从 [skill-creator](https://github.com/user/skill-creator) 移植并改造了以下核心代码：

| 源文件 | 移植到 | 改造内容 |
|--------|--------|----------|
| `scripts/utils.py` | `parser.py` | 扩展为 5-tuple 返回值 |
| `scripts/run_eval.py` | `optimization/trigger_eval.py` | 重构为 TriggerEvaluator 类 |
| `scripts/aggregate_benchmark.py` | `execution/benchmark.py` | 迁移到 Pydantic 模型 |
| `scripts/improve_description.py` | `optimization/description_improver.py` | 提取为 DescriptionImprover 类 |
| `scripts/run_loop.py` | `optimization/loop.py` | 重构为 OptimizationLoop 类 |
| `eval-viewer/generate_review.py` | `reporting/viewer.py` | 整合为 ReportGenerator |
| `eval-viewer/viewer.html` | `reporting/templates/viewer.html` | 直接复制 |
| `agents/grader.md` | `agents/grader.md` | 直接复制 |
| `scripts/quick_validate.py` | `static_analysis/checks.py` | 验证逻辑融入 11 项检查 |

新增模块（无对应源码）：

- `security/` — 安全审查引擎
- `compliance/` — 遵循性引擎
- `calibration/` — 评分校准
- `batch/` — 批量评测（DAG + 级联归因）
- `config/` — 三层配置系统 + LLM 配置生成
- `scanning/` — L0 覆盖率扫描
- `integration/` — L2 集成测试 + Pipeline 执行
- `scoring/plugins.py` — 插件注册机制
- `scoring/templates.py` — 配置驱动断言模板库

---

## Exit Code

| Code | 含义 |
|------|------|
| 0 | 通过 |
| 1 | 失败（检查/评测未通过/回归/安全风险） |
| 2 | 输入错误（SKILL.md 缺失、evals.json 格式错误） |
| 3 | 执行错误（claude CLI 未找到、超时） |

---

## 常见问题

### 安全审查需要安装额外的扫描器吗？

不需要。内置 2 个原生扫描器，开箱即用。安装 [aguara](https://github.com/garagon/aguara)（Go）和 [skill-scanner](https://pypi.org/project/cisco-ai-skill-scanner/)（pip）可增强能力，但未安装时自动 SKIP。

### `--security-gate` 和 `--compliance-gate` 有什么区别？

| Flag | 检查对象 | 失败条件 | 触发时机 |
|------|----------|----------|----------|
| `--security-gate` | Skill 源代码安全性 | CRITICAL/HIGH/MEDIUM 风险 | 评测执行前 |
| `--compliance-gate` | 执行过程是否遵循 SKILL.md | compliance_rate < 0.5 | 评分阶段 |

### 静态分析需要 Claude CLI 吗？

不需要。`analyze` 零 API 成本，<1s 完成。

### 运行评测时提示 "claude CLI not found"

确保 `claude` 已安装并在 PATH 中。Skill-Eval 通过 `subprocess` 调用 `claude -p`，复用 CLI 认证。

**Claude CLI 版本要求**：`>= 2.1.0, < 2.2.0`（推荐 2.1.x 系列）。skill-eval 在启动需要 claude 的命令时自动检查版本，版本过低会报错并提示升级；版本超出测试范围会打印警告但仍尝试运行。使用到的 claude flags：`--output-format stream-json`、`--verbose`、`--dangerously-skip-permissions`、`--max-turns`、`--disable-slash-commands`、`--include-partial-messages`、`--model`。详见 [Claude Code CLI 参考](https://docs.anthropic.com/en/docs/claude-code/cli-usage) 和 [Changelog](https://docs.anthropic.com/en/docs/claude-code/changelog)。

安装 Claude Code：[官方文档](https://docs.anthropic.com/en/docs/claude-code/overview)

```bash
# 检查当前版本
claude --version

# 升级
claude update
```

### evals.json 放在哪里？

推荐 `<skill_path>/evals/evals.json`。`run` 和 `full` 会自动查找，也可用 `--evals` 手动指定。

### 如何在 CI 中使用？

```bash
skill-eval vet path/to/skill/ --format json
skill-eval analyze path/to/skill/ --format json -o scorecard.json
skill-eval full path/to/skill/ --format json --runs 1 --security-gate
# exit code: 0=通过, 1=失败, 2=输入错误, 3=执行错误
```

### viewer.html 的字段名可以改吗？

不能。`viewer.html` 依赖硬编码字段名：`"with_skill"`、`"without_skill"`、`"text"`、`"passed"`、`"evidence"`。
