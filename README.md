# Skill-Eval

**Skill-Eval** 是一个独立 Python CLI 工具，用于对 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 的 Skill（以 `SKILL.md` 为核心的技能定义文件）进行自动化评测。

它将 Skill 开发从基于直觉的"艺术"转变为**可度量、可迭代、可回归验证**的工程实践。

---

## 目录

- [核心能力](#核心能力)
- [快速开始](#快速开始)
- [安装](#安装)
- [CLI 命令参考](#cli-命令参考)
  - [vet — 安全审查](#vet--安全审查)
  - [init — 初始化评测定义](#init--初始化评测定义)
  - [analyze — 静态分析](#analyze--静态分析)
  - [scan — 覆盖率扫描](#scan--覆盖率扫描)
  - [generate-config — 生成配置](#generate-config--生成配置)
  - [run — A/B 评测](#run--ab-评测)
  - [full — 一键完整流程](#full--一键完整流程)
  - [report — HTML 报告](#report--html-报告)
  - [optimize-trigger — 触发优化](#optimize-trigger--触发优化)
  - [batch — 批量评测](#batch--批量评测)
  - [import — 数据导入](#import--数据导入)
- [配置驱动架构](#配置驱动架构)
- [评测体系](#评测体系)
  - [三层质量保障](#三层质量保障)
  - [量化公式](#量化公式)
  - [评分模式](#评分模式)
- [JSON 输出格式](#json-输出格式)
- [项目结构](#项目结构)
- [测试](#测试)
- [设计文档](#设计文档)
- [与 Skill-Creator 的关系](#与-skill-creator-的关系)
- [Exit Code](#exit-code)
- [常见问题](#常见问题)

---

## 核心能力

| 能力 | 说明 |
|------|------|
| **安全审查** | 在安装/评测前检测恶意代码、硬编码凭证、Prompt 注入等安全风险（<1s） |
| **静态分析** | 对 SKILL.md 做 11 项零成本自动化体检（<1s），无需 API 调用 |
| **A/B 评测** | 对每个 eval 用例分别执行 with_skill / without_skill，量化 Skill 的真实增量价值 |
| **混合评分** | 程序化断言（确定性、快速）+ LLM 语义评分（深度分析），可独立开关 |
| **遵循性检查** | 验证执行**过程**是否遵循 SKILL.md 定义的指令，而非仅看输出结果 |
| **评分校准** | 交叉验证程序化与 LLM 评分的一致性，检测漏判/误判，输出可信度等级 |
| **触发优化** | 迭代优化 Skill 的触发描述，自动找到最佳 description |
| **批量评测** | 支持多 Skill 依赖关系的 DAG 调度，含级联失败归因 |
| **HTML 报告** | 自包含 HTML 报告，支持键盘导航、历史对比、反馈保存 |
| **CI 就绪** | 所有命令支持 `--format json` + exit code，可直接集成 CI 流水线 |
| **配置驱动** | 三层配置系统 + LLM 配置生成 + 插件机制，框架零硬编码，适配任意 Skill 集合 |
| **L0 覆盖率扫描** | 扫描 evals.json 结构完整性和断言覆盖率，无需 API |
| **L2 集成测试** | Chain/Routing/Contract 三类集成测试 + 影响分析 |

---

## 快速开始

```bash
# 安装
pip install -e .

# 安全审查（无需 API，<1s 完成）
skill-eval vet path/to/my-skill/

# 对 Skill 做静态分析（无需 API）
skill-eval analyze path/to/my-skill/

# L0 覆盖率扫描（无需 API）
skill-eval scan path/to/skills-dir/

# 为 Skill 集合生成配置（需要 claude CLI）
skill-eval generate-config path/to/skills-dir/ --output eval-config.yaml

# 运行完整评测（需要本地安装 claude CLI）
skill-eval full path/to/my-skill/ --runs 3

# 查看评分详情
skill-eval analyze path/to/my-skill/ --format json
```

### 前置条件

- **Python 3.10+**
- **Claude CLI**（仅动态评测需要）— 运行 `claude` 命令需已在 PATH 中并完成认证
- 静态分析（`analyze`）和安全审查（`vet`）**不需要** Claude CLI 或 API 额度

---

## 安装

```bash
# 从源码安装（开发模式）
git clone <repo-url> && cd skill-eval
pip install -e ".[dev]"

# 验证安装
skill-eval --version

# 运行测试
pytest tests/ -v
```

### 依赖

| 依赖 | 用途 |
|------|------|
| `typer>=0.9` | CLI 框架 |
| `rich>=13` | 终端美化输出 |
| `pydantic>=2.0` | 数据模型与 JSON Schema |
| `pyyaml>=6.0` | SKILL.md frontmatter 解析 |

### 可选安全扫描器

安全审查（`vet`）内置 2 个原生扫描器（secrets-scan + structure-check），无需额外安装即可使用。以下 2 个外部扫描器为可选增强，未安装时自动跳过：

| 扫描器 | 安装方式 | 检查内容 |
|--------|----------|----------|
| [aguara](https://github.com/garagon/aguara) | `go install github.com/garagon/aguara/cmd/aguara@latest` | Prompt 注入、混淆、可疑 LLM 调用 |
| [skill-scanner](https://pypi.org/project/cisco-ai-skill-scanner/) | `pip install cisco-ai-skill-scanner` | 已知恶意模式、CVE 漏洞库 |

> **注意**：aguara 需要安装 [Go](https://go.dev/dl/)（>= 1.21）。安装后确保 `~/go/bin` 在 PATH 中。

---

## CLI 命令参考

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

**在 CI 中使用**：

```bash
# 安全审查 + JSON 输出（BLOCKED 时 exit code 1）
skill-eval vet path/to/skill/ --format json

# 在评测前启用安全门控
skill-eval full path/to/skill/ --security-gate
```

---

### `init` — 初始化评测定义

根据 SKILL.md 内容自动生成 `evals.json` 模板，作为 A/B 评测的输入。

```bash
# 自动生成（无需 API 调用，<1s 完成）
skill-eval init path/to/skill/

# 指定输出路径
skill-eval init path/to/skill/ -o path/to/evals.json

# 使用 LLM 生成更智能的 prompt（需要 claude CLI）
skill-eval init path/to/skill/ --generate-prompts
```

**工作原理**：

1. 解析 SKILL.md 的 `##` 二级标题，每个标题对应一个 eval case（最多 50 个）
2. 从标题下的列表项中提取 expectations
3. 从代码块中检测可能的输出文件，生成 assertions
4. 根据 description 中的关键词推断动作类型（create/convert/build/...）

**生成示例**：

```
my-skill/evals/evals.json  ← 默认输出位置
```

生成的 `evals.json` 可以手动编辑——调整 prompt、增删 expectations、添加 assertions，然后再运行 `run` 或 `full`。

> **提示**：`--generate-prompts` 模式会调用 `claude -p` 根据 SKILL.md 全文生成更贴近真实用户场景的 prompt，但需要 API 额度。

---

### `analyze` — 静态分析

对 SKILL.md 做 11 项自动化体检，**零 API 成本，<1s 完成**。

```bash
# 终端表格输出
skill-eval analyze path/to/skill/

# JSON 输出（CI 友好）
skill-eval analyze path/to/skill/ --format json

# 保存结果到文件
skill-eval analyze path/to/skill/ --format json -o scorecard.json
```

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

---

### `scan` — L0 覆盖率扫描

扫描 Skill 集合的 evals.json 结构完整性和断言覆盖率，**零 API 成本，<1s 完成**。

```bash
# 扫描目录下所有 Skill 的 evals.json
skill-eval scan path/to/skills-dir/

# 指定配置前缀（只检查特定配置下的断言）
skill-eval scan path/to/skills-dir/ --config-prefix arkts

# 按领域/类型过滤
skill-eval scan path/to/skills-dir/ --domain frontend --type create

# JSON 输出
skill-eval scan path/to/skills-dir/ --format json
```

**检查项**：

| 检查项 | 说明 |
|--------|------|
| evals.json 存在性 | Skill 目录下是否有 evals/evals.json |
| skill_name 完整性 | evals.json 的 skill_name 非空 |
| prompt 完整性 | 每个 eval 的 prompt 非空 |
| assertion 类型合法性 | 断言类型在 9 种内置类型范围内 |
| 断言覆盖率 | 有 assertions 的 eval 占比 |

---

### `generate-config` — LLM 配置生成

通过 `claude -p` 扫描 Skill 集合的元数据，自动生成 `eval-config.yaml` 配置文件。

```bash
# 为 Skill 集合生成配置
skill-eval generate-config path/to/skills-dir/ --output eval-config.yaml

# 指定模型
skill-eval generate-config path/to/skills-dir/ --model claude-sonnet-4-20250514
```

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
```

> **核心理念**：框架代码零硬编码，所有领域/类型模板通过配置注入。不同 Skill 集合（如 ArkTs-Core）各自运行此命令生成自己的配置。

---

### `run` — A/B 评测

对 Skill 执行完整的 A/B 对照实验。

```bash
# 基本用法
skill-eval run path/to/skill/ --evals path/to/evals.json

# 自定义参数
skill-eval run path/to/skill/ \
  --runs 5 \
  --model claude-sonnet-4-20250514 \
  --workers 8 \
  --timeout 180 \
  --scoring hybrid \
  --compliance-gate \
  -o output/

# 仅程序化评分（不调用 LLM）
skill-eval run path/to/skill/ --scoring programmatic

# 仅 LLM 语义评分
skill-eval run path/to/skill/ --scoring llm
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找 | evals.json 路径（默认查找 skill 目录下） |
| `--runs` | 3 | 每种配置的运行次数 |
| `--model` | 用户默认模型 | claude -p 使用的模型 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--timeout` | 120 | 每次运行超时（秒） |
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

---

### `full` — 一键完整流程

自动执行：安全审查（可选门控）→ 静态分析 → 加载 evals → A/B 测试 → 聚合 → 生成报告。

```bash
# 一键运行
skill-eval full path/to/skill/ --runs 3

# 运行后打开报告
skill-eval full path/to/skill/ --open

# 评测前先做安全审查（BLOCKED/REVIEW_NEEDED 时停止）
skill-eval full path/to/skill/ --security-gate

# CI 模式
skill-eval full path/to/skill/ --format json --runs 1
```

**参数**（`full` 特有）：

| 参数 | 说明 |
|------|------|
| `--security-gate` | 在评测前运行安全审查，BLOCKED 或 REVIEW_NEEDED 时立即失败（exit code 1） |
| `--compliance-gate` | 遵循性率 < 0.5 时强制失败 |
| `--open` | 运行完成后在浏览器中打开报告 |

> 使用 `--security-gate` 后，`full` 流程从 4 步变为 5 步（Step 0: 安全审查 → Step 1-4: 原有流程）。

---

### `report` — HTML 报告

从评测结果生成自包含 HTML 报告。

```bash
# 生成静态 HTML
skill-eval report output/iteration-1/ --benchmark output/iteration-1/benchmark.json

# 启动 HTTP 服务器（支持实时刷新和反馈保存）
skill-eval report output/iteration-1/ --open --port 8080

# 对比历史结果
skill-eval report output/iteration-2/ --previous output/iteration-1/benchmark.json
```

---

### `optimize-trigger` — 触发优化

迭代优化 Skill 的触发描述（frontmatter `description` 字段）。

```bash
skill-eval optimize-trigger path/to/skill/ \
  --eval-set trigger-evals.json \
  --max-iterations 5 \
  --model claude-sonnet-4-20250514
```

优化过程使用 60/40 训练/测试分层采样，自动选择测试集上表现最好的描述。

---

### `batch` — 批量评测

对多个相互依赖的 Skill 执行分层评测：Unit（独立 A/B 测试）→ Integration（Skill 间协作）→ Regression（与基线对比）。

```bash
# 从目录自动发现，运行所有层
skill-eval batch path/to/skills-directory/

# 从 manifest 运行
skill-eval batch manifest.json --runs 3 --workers 8

# 只跑 Unit 测试（每个 Skill 独立 A/B）
skill-eval batch manifest.json --layers unit

# 只跑 Integration 测试（Skill 间协作，需 eval-config.yaml）
skill-eval batch manifest.json --layers integration

# 跑 Unit + Integration
skill-eval batch manifest.json --layers unit,integration

# 跑 Unit + Regression（需上次 baseline）
skill-eval batch manifest.json --layers unit,regression --baseline batch-workspace-20260427/batch-benchmark.json
```

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
| `integration` | Skill 间协作（chain/routing/contract） | eval-config.yaml 的 `integration_scenarios` | integration-report.json |
| `regression` | 与上次 benchmark 逐 Skill 对比 | `--baseline` 上次的 batch-benchmark.json | regression-delta.json |

**级联失败归因**：当上游 Skill 失败时，下游 Skill 会被标记为 `CASCADE` 而非 `ROOT_CAUSE`，帮助快速定位真正的故障源头。

---

### `import` — 数据导入

将 [skill-monitor](https://github.com/user/skill-monitor) 的运行时 JSONL 数据导入 benchmark。

```bash
skill-eval import monitor-data.jsonl --benchmark output/iteration-1/benchmark.json
```

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

每个项目在初始化时注册自己的领域插件。

### 断言模板库

`AssertionTemplateLibrary` 从 `eval-config.yaml` 的 `domain_templates` 和 `type_templates` 读取模板，`EvalInitGenerator` 生成 evals 时自动注入领域相关断言。

```python
from skill_eval.scoring.templates import get_default_assertions

# 根据 skill 的 domain 和 type，从配置获取推荐断言
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
   → 运行 eval-config.yaml 中的 integration_scenarios
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

`CalibrationGrade` 是**乘法系数**：如果评分不可信（UNRELIABLE），整个分数会被大幅折扣。

### 评分模式

| 模式 | 说明 | LLM 调用 | 适用场景 |
|------|------|----------|----------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分，冲突以 LLM 为准 | 是 | 通用场景 |
| `programmatic` | 仅确定性断言 | 否 | 输出结构化、可机械验证 |
| `llm` | 仅 LLM 语义评分 | 是 | 输出需要语义判断 |

**程序化断言类型**：

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

### 测试设定指南

#### 两条评分路径如何工作

```
evals.json 中的 eval_case
     │
     ├── expectations[] ──→ LLM 批量评判 ──→ 逐条 pass/fail + evidence
     │                     (检查 transcript 文本)
     │
     └── assertions[]  ──→ 程序化检查 ──→ 逐条 pass/fail + evidence
                           (检查 output_dir/ 文件)

两者结果合并 → grading.json
```

**评分流程**（`full` 命令内部）：

1. 所有 eval 的 prompt 合并为 `merged_prompt`，一次 `claude -p` 执行
2. `_score_checkpoints()` 对每个 checkpoint：
   - 有 assertions → `ProgrammaticChecker` 检查 `output_dir/` 中的文件
   - 有 expectations → 一次批量 LLM 调用评估所有 expectations
3. 结果写入 `grading.json`（总体）+ `checkpoints.json`（per-checkpoint）
4. `CalibrationChecker` 交叉验证两种来源的 agreement rate

#### 测试层级总览

skill-eval 支持三层测试，对应从"单个 Skill 规则理解"到"Skill 间协作"的不同粒度：

| 层级 | 测试什么 | 需要什么 | 运行方式 |
|------|----------|----------|----------|
| **L1**（单元级） | 单个 Skill 的规则理解是否正确 | evals.json + expectations | `skill-eval run/full` |
| **L2**（Mock 场景） | Skill 在有 fixture 的场景下能否正确产出文件 | evals.json + expectations + assertions + fixtures | `skill-eval run/full` |
| **L2 集成**（Integration） | 多个 Skill 之间的协作是否正确 | eval-config.yaml 的 `integration_scenarios` | `skill-eval batch --layers integration` |

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

集成测试通过 `eval-config.yaml` 的 `integration_scenarios` 定义，由 `skill-eval batch --layers integration` 运行。

**三种场景类型**：

**1. Chain（链式传递）**：验证 Skill 链的顺序执行和失败传播。

```yaml
integration_scenarios:
  - name: "data-transform → report-gen pipeline"
    type: "chain"
    chain: ["data-transform", "report-gen"]
    input: { source: "raw.csv" }
```

如果 `data-transform` 失败，`report-gen` 应被标记为 SKIPPED（而非独立失败）。

**2. Routing（关键词路由）**：验证用户 prompt 是否触发了正确的 Skill。

```yaml
integration_scenarios:
  - name: "keyword routing"
    type: "routing"
    test_prompts:
      - prompt: "把这些数据转换为 ArkTS 格式"
        should_trigger: ["data-transform"]
        should_not_trigger: ["report-gen"]
      - prompt: "生成项目报告"
        should_trigger: ["report-gen"]
        should_not_trigger: ["data-transform"]

skill_keywords:
  data-transform: ["转换", "transform", "convert"]
  report-gen: ["报告", "report", "生成"]
```

路由通过 `skill_keywords` 配置的关键词匹配实现——prompt 中出现关键词就认为触发了对应 Skill。

**3. Contract（制品契约）**：验证 Skill 间传递的制品是否符合消费者期望。

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
      - type: "PATTERN_MATCH"
        pattern: '"status":\\s*"ok"'
```

**运行集成测试**：

```bash
# 生成配置（包含 integration_scenarios）
skill-eval generate-config path/to/skills-dir/ --output eval-config.yaml

# 编辑 eval-config.yaml，添加 integration_scenarios 和 skill_keywords

# 运行集成测试
skill-eval batch manifest.json --layers integration

# 同时跑 Unit + Integration
skill-eval batch manifest.json --layers unit,integration
```

产出 `integration-report.json`：

```json
{
  "total": 3,
  "passed": 2,
  "failed": 1,
  "scenarios": [
    {"name": "data pipeline", "type": "chain", "passed": true, ...},
    {"name": "keyword routing", "type": "routing", "passed": true, ...},
    {"name": "contract check", "type": "contract", "passed": false, ...}
  ]
}
```

#### 输出文件说明

`full` 命令完成后，`output/iteration-1/` 目录结构：

| 文件 | 内容 |
|------|------|
| `merged_prompt.txt` | 合并后的完整 prompt |
| `benchmark.json` | 聚合结果（含 checkpoints 数据） |
| `report.html` | 自包含 HTML 报告 |
| `with_skill/run-1/grading.json` | 所有 expectations + assertions 的 pass/fail |
| `with_skill/run-1/checkpoints.json` | 每个 checkpoint 独立覆盖率 |
| `with_skill/run-1/compliance.json` | SKILL.md 指令遵循性检查 |
| `with_skill/run-1/compliance.log` | 逐条遵循性日志 |
| `with_skill/run-1/calibration.json` | 评分可信度校准 |
| `with_skill/run-1/run_debug.json` | claude 完整回复 + tool_calls + timing |
| `with_skill/run-1/timing.json` | 执行耗时和 token 统计 |
| `without_skill/run-1/grading.json` | 无 Skill 的评分结果 |
| `without_skill/run-1/error.txt` | 错误信息（如果 claude 执行失败） |

---

## JSON 输出格式

所有 JSON 输出与 [skill-creator](https://github.com/user/skill-creator) 的 schema 兼容，共享 `viewer.html` 工具链。

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

**字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 唯一标识，整数 |
| `prompt` | 是 | 发给 claude 的任务描述 |
| `expected_output` | 否 | 预期输出的文字描述（供 LLM 评分参考） |
| `level` | 否 | 测试层级：`L1`（单元级规则理解）、`L2`（Mock 场景）、`L3`（全流程），默认 `L1` |
| `fixtures` | 否 | 前置文件列表，`[{"path": "spec/plan.md", "content": "..."}]` |
| `expectations` | 否 | 语义预期语句列表，由 LLM 判断是否被满足 |
| `assertions` | 否 | 程序化断言列表，对 `output_dir/` 中的文件做确定性检查 |

> **expectations vs assertions 的区别**：expectations 检查 claude 的**回复文本**是否包含了某些语义内容（"说了什么"）；assertions 检查 claude 执行后**产生的文件**是否符合预期（"做了什么"）。L1 单元级测试通常只有 expectations（因为是问答型），L2/L3 应同时包含 assertions。

### checkpoints.json（per-checkpoint 明细）

`full` 命令运行后，`with_skill/run-1/checkpoints.json` 包含每个 eval（checkpoint）的独立覆盖率：

```json
[
  {
    "eval_id": 1,
    "eval_name": "L1-输入验证-plans不存在",
    "passed": 3,
    "failed": 0,
    "total": 3,
    "pass_rate": 1.0,
    "assertions": [],
    "expectations": [
      {"text": "检查了 spec/baseline/plans/ 目录是否存在", "passed": true, "evidence": "...", "source": "llm_batch"}
    ]
  }
]
```

### grading.json（评分结果）

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

### benchmark.json（聚合结果）

```json
{
  "metadata": {"skill_name": "my-skill", "timestamp": "2026-01-15T10:30:00Z", "runs_per_configuration": 3},
  "runs": [
    {"eval_id": 1, "configuration": "with_skill", "run_number": 1, "result": {"pass_rate": 0.85}, "expectations": [...]}
  ],
  "run_summary": {
    "with_skill": {"pass_rate": {"mean": 0.85, "stddev": 0.05}},
    "without_skill": {"pass_rate": {"mean": 0.35, "stddev": 0.08}},
    "delta": {"pass_rate": "+0.50", "time_seconds": "+13.0", "tokens": "+1700"}
  }
}
```

> **重要**：`configuration` 必须是 `"with_skill"` 或 `"without_skill"`，`expectations` 必须使用 `"text"`、`"passed"`、`"evidence"` 字段名 — viewer.html 依赖这些字段。

---

## 项目结构

```
skill-eval/
├── pyproject.toml
├── agents/                        # LLM prompt 模板
│   ├── grader.md                  # LLM 评分 agent prompt
│   ├── analyzer.md                # 分析 agent prompt
│   └── comparator.md              # 盲评 agent prompt
├── src/skill_eval/
│   ├── __init__.py
│   ├── cli.py                     # typer CLI 入口（11 个子命令）
│   ├── models.py                  # 30+ Pydantic 数据模型
│   ├── parser.py                  # SKILL.md 解析器
│   ├── errors.py                  # 自定义异常 + 错误码
│   ├── config/                    # 配置驱动架构
│   │   ├── loader.py              # 三层配置加载 + EvalConfig
│   │   ├── defaults.yaml          # 框架默认值（无领域内容）
│   │   └── generator.py           # LLM 配置生成器 + scan_skills_metadata()
│   ├── security/
│   │   └── vetter.py              # SecurityVetter（4 扫描器，集成 skill-vetter）
│   ├── static_analysis/
│   │   ├── analyzer.py            # StaticAnalyzer（11 项检查 + 分类）
│   │   ├── checks.py              # 11 个检查函数
│   │   └── classifier.py          # Skill 分类器
│   ├── scanning/
│   │   └── scanner.py             # L0 覆盖率扫描（evals 验证 + 断言覆盖）
│   ├── evals/
│   │   ├── loader.py              # EvalLoader（加载+验证 evals.json）
│   │   └── generator.py           # EvalInitGenerator（config + mode 增强）
│   ├── execution/
│   │   ├── runner.py              # ClaudeRunner + ABTestRunner
│   │   └── benchmark.py           # BenchmarkAggregator（统计+delta）
│   ├── scoring/
│   │   ├── programmatic.py        # ProgrammaticChecker（9 种断言 + 插件调度）
│   │   ├── llm_grader.py          # LLMGrader（语义评分）
│   │   ├── orchestrator.py        # ScoringOrchestrator（合并结果）
│   │   ├── plugins.py             # AssertionPluginRegistry（领域插件注册）
│   │   └── templates.py           # AssertionTemplateLibrary（配置驱动模板）
│   ├── compliance/
│   │   ├── extractor.py           # InstructionExtractor（6 类指令提取）
│   │   └── checker.py             # ComplianceChecker（过程合规验证）
│   ├── calibration/
│   │   └── calibrator.py          # CalibrationChecker（7 类信号+4 级等级）
│   ├── optimization/
│   │   ├── trigger_eval.py        # TriggerEvaluator（stream-json 触发检测）
│   │   ├── description_improver.py# DescriptionImprover（LLM 描述优化）
│   │   └── loop.py                # OptimizationLoop（迭代优化主循环）
│   ├── reporting/
│   │   ├── viewer.py              # ReportGenerator（HTML 生成+HTTP 服务）
│   │   └── templates/
│   │       └── viewer.html        # 自包含 HTML 模板
│   ├── integration/
│   │   ├── monitor_importer.py    # JSONL 数据导入
│   │   ├── runner.py              # L2 集成测试（chain/routing/contract）
│   │   └── impact.py              # 影响分析（爆炸半径）
│   └── batch/
│       ├── manifest.py            # ManifestLoader + 自动发现
│       ├── dag.py                 # DAG 构建 + 拓扑排序 + 环检测
│       ├── cascade.py             # CascadeAnalyzer（级联归因）
│       └── scheduler.py           # BatchScheduler（多 Skill 编排 + L2 集成）
├── tests/
│   ├── conftest.py                # 共享 fixture
│   ├── fixtures/
│   │   ├── sample-skill/          # 测试用 Skill fixture
│   │   ├── sample-evals.json
│   │   └── sample-grading.json
│   ├── test_parser.py
│   ├── test_static_analysis.py
│   ├── test_models.py
│   ├── test_scoring_programmatic.py
│   ├── test_extended_assertions.py  # 3 种新断言 + 插件注册
│   ├── test_assertion_templates.py  # 配置驱动断言模板
│   ├── test_compliance.py
│   ├── test_security.py
│   ├── test_execution.py
│   ├── test_batch.py
│   ├── test_config_loader.py       # 三层配置加载
│   ├── test_config_generator.py    # LLM 配置生成
│   ├── test_eval_generator.py      # config + mode 增强
│   ├── test_scanner.py             # L0 覆盖率扫描
│   ├── test_integration.py         # L2 集成测试
│   └── test_impact.py              # 影响分析
└── docs/
    ├── skill-eval-design.md       # 实现级设计文档
    ├── skill-eval-arch-design.md  # 架构师版设计文档
    └── skill-eval-principles.md   # 底层原理与参考出处
```

---

## 测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行特定模块
pytest tests/test_static_analysis.py -v
pytest tests/test_compliance.py -v

# 查看覆盖率
pytest tests/ --cov=skill_eval
```

当前共 **190 个测试**，覆盖：

| 测试文件 | 覆盖模块 | 测试数 |
|----------|----------|--------|
| `test_parser.py` | SKILL.md 解析（正常/异常/边界） | 7 |
| `test_static_analysis.py` | 11 项检查 + 分类器 + 分析器 | 30 |
| `test_models.py` | Pydantic 模型验证 + JSON 往返 | 10 |
| `test_scoring_programmatic.py` | 6 种基础断言类型 | 11 |
| `test_extended_assertions.py` | 3 种新断言 + 插件注册表 | 13 |
| `test_assertion_templates.py` | 配置驱动断言模板库 | 8 |
| `test_compliance.py` | 指令提取 + 遵循性检查 + 校准 | 16 |
| `test_security.py` | 4 扫描器 + 判定逻辑 + 外部扫描器降级 | 21 |
| `test_execution.py` | EvalLoader + BenchmarkAggregator | 13 |
| `test_batch.py` | DAG + 级联分析 + Manifest | 12 |
| `test_config_loader.py` | 三层配置加载 + 模板展开 | 8 |
| `test_config_generator.py` | 元数据扫描 + prompt 构建 + YAML 清洗 | 9 |
| `test_eval_generator.py` | domain 断言注入 + mode 支持 | 5 |
| `test_scanner.py` | L0 evals 验证 + 覆盖率 + 过滤 | 13 |
| `test_integration.py` | L2 chain/routing/contract 集成测试 | 8 |
| `test_impact.py` | 影响分析（爆炸半径） | 4 |

---

## 设计文档

| 文档 | 读者 | 内容 |
|------|------|------|
| `docs/skill-eval-arch-design.md` | 架构师/技术决策者 | 设计决策、模块契约、质量公式、12 章 |
| `docs/skill-eval-design.md` | 实现工程师 | Pydantic 模型、函数签名、CLI 参数、伪代码、6 个附录 |
| `docs/skill-eval-principles.md` | 所有开发者 | 底层原理、参考出处、配置驱动架构 v0.2 |

---

## 与 Skill-Creator 的关系

Skill-Eval 从 [skill-creator](https://github.com/user/skill-creator) 移植并改造了以下核心代码：

| 源文件 | 移植到 | 改造内容 |
|--------|--------|----------|
| `scripts/utils.py` | `parser.py` | 扩展为 5-tuple 返回值（增加 frontmatter_dict + body_text） |
| `scripts/run_eval.py` | `optimization/trigger_eval.py` | 重构为 TriggerEvaluator 类 |
| `scripts/aggregate_benchmark.py` | `execution/benchmark.py` | 迁移到 Pydantic 模型 |
| `scripts/improve_description.py` | `optimization/description_improver.py` | 提取为 DescriptionImprover 类 |
| `scripts/run_loop.py` | `optimization/loop.py` | 重构为 OptimizationLoop 类 |
| `eval-viewer/generate_review.py` | `reporting/viewer.py` | 整合为 ReportGenerator |
| `eval-viewer/viewer.html` | `reporting/templates/viewer.html` | 直接复制 |
| `agents/grader.md` | `agents/grader.md` | 直接复制 |
| `scripts/quick_validate.py` | `static_analysis/checks.py` | 验证逻辑融入 11 项检查 |

新增模块（无对应源码）：

- `security/` — 安全审查引擎（4 扫描器，集成 [skill-vetter](https://github.com/app-incubator-xyz/skill-vetter)）
- `compliance/` — 遵循性引擎（指令提取 + 过程合规检查）
- `calibration/` — 评分校准（交叉验证 + 可信度等级）
- `batch/` — 批量评测（DAG 调度 + 级联归因）
- `config/` — 三层配置系统 + LLM 配置生成（v0.2 新增）
- `scanning/` — L0 覆盖率扫描（v0.2 新增）
- `integration/runner.py` — L2 集成测试（chain/routing/contract）（v0.2 新增）
- `integration/impact.py` — 影响分析/爆炸半径（v0.2 新增）
- `scoring/plugins.py` — 插件注册机制（v0.2 新增）
- `scoring/templates.py` — 配置驱动断言模板库（v0.2 新增）

---

## Exit Code

| Code | 含义 |
|------|------|
| 0 | 通过（所有检查/评测通过） |
| 1 | 失败（存在未通过的检查或评测回归） |
| 2 | 输入错误（SKILL.md 缺失、evals.json 格式错误等） |
| 3 | 执行错误（claude CLI 未找到、超时等） |

---

## 常见问题

### 安全审查需要安装额外的扫描器吗？

不需要。`vet` 命令内置 2 个原生扫描器（secrets-scan + structure-check），开箱即用。安装 [aguara](https://github.com/garagon/aguara)（Go）和 [skill-scanner](https://pypi.org/project/cisco-ai-skill-scanner/)（pip）可获得 Prompt 注入检测和 CVE 漏洞库扫描能力，但未安装时自动 SKIP，不影响其他扫描器运行。

### `--security-gate` 和 `--compliance-gate` 有什么区别？

| Flag | 检查对象 | 失败条件 | 触发时机 |
|------|----------|----------|----------|
| `--security-gate` | Skill 源代码安全性 | 存在 CRITICAL/HIGH/MEDIUM 风险 | 评测执行前（Step 0） |
| `--compliance-gate` | 执行过程是否遵循 SKILL.md | compliance_rate < 0.5 | 评测执行后（评分阶段） |

两者可以同时使用：`skill-eval full path/to/skill/ --security-gate --compliance-gate`

### 静态分析需要安装 Claude CLI 吗？

不需要。`analyze` 命令只做本地文件分析，零 API 成本，<1s 完成。

### 运行评测时提示 "claude CLI not found"

确保 `claude` 已安装并在 PATH 中。Skill-Eval 通过 `subprocess` 调用 `claude -p`，复用你的 Claude CLI 认证，不需要单独的 API Key。

### evals.json 放在哪里？

推荐放在 Skill 目录下的 `evals/evals.json`。`run` 和 `full` 命令会自动查找：
1. `<skill_path>/evals/evals.json`
2. `<skill_path>/evals.json`

也可以用 `--evals` 参数手动指定。

### viewer.html 的字段名可以改吗？

不能。`viewer.html` 依赖硬编码字段名：`"with_skill"`、`"without_skill"`、`"text"`、`"passed"`、`"evidence"`。所有 JSON 输出必须保持这些字段名。

### 如何在 CI 中使用？

```bash
# 安全审查（BLOCKED 时 exit code 1）
skill-eval vet path/to/skill/ --format json

# 静态分析 + JSON 输出
skill-eval analyze path/to/skill/ --format json -o scorecard.json

# 完整评测 + 安全门控 + JSON 输出
skill-eval full path/to/skill/ --format json --runs 1 --security-gate

# exit code 自动反映结果：
# 0 = 通过, 1 = 失败/回归/安全风险, 2 = 输入错误, 3 = 执行错误
```

### Skill Monitor 集成是必须的吗？

不是。`skill-monitor` JSONL 导入是完全可选的。不安装 skill-monitor 也能正常运行所有评测。

---

## License

Private — Internal Use Only
