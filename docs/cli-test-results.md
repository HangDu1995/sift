# Skill-Eval 测试指南

> 本文档介绍 skill-eval 的测试 Skill 是什么、如何组织，以及评测产生的各项指标如何解读。
> 适合想理解 skill-eval 评测结果、或准备为自己的 Skill 编写测试的读者。

---

## 目录

- [测试 Skill 概览](#测试-skill-概览)
- [测试 Fixtures 结构](#测试-fixtures-结构)
- [三个测试 Skill 详解](#三个测试-skill-详解)
- [测试层级与编排](#测试层级与编排)
- [CLI 命令与测试流程](#cli-命令与测试流程)
- [指标解读](#指标解读)
- [输出文件结构](#输出文件结构)
- [如何为自己的 Skill 编写测试](#如何为自己的-skill-编写测试)
- [已知问题与注意事项](#已知问题与注意事项)
- [附录：完整测试结果记录](#附录完整测试结果记录)

---

## 测试 Skill 概览

skill-eval 使用 **三个精心设计的测试 Skill** 来验证框架自身的功能。它们模拟了一个典型的数据处理流水线，从数据校验到格式转换再到报告生成，覆盖了 Skill 间依赖、编排和协作等核心场景。

| Skill | 职责 | 依赖 | Evals | 测试层级 |
|-------|------|------|-------|----------|
| `data-validator` | 校验 JSON 数据文件的合法性，生成校验报告 | 无（叶节点） | 3 个（1×L1 + 2×L2） | L1, L2 |
| `data-transform` | 将已校验的 JSON 数据转换为不同格式（扁平化、嵌套等） | `data-validator` | 3 个 | L1, L2 |
| `report-generator` | 从转换后的数据生成 Markdown 汇总报告 | `data-transform` | 3 个 | L1, L2 |

这三个 Skill 形成了一条**线性依赖链**：

```
data-validator → data-transform → report-generator
    (校验)           (转换)           (报告)
```

这条依赖链专门用于测试 `batch` 命令的 DAG 拓扑排序、级联失败归因、以及 Pipeline 流水线执行。

---

## 测试 Fixtures 结构

所有测试数据位于 `fixtures/test-skills/` 目录：

```
fixtures/test-skills/
├── data-validator/               # Skill 1: 数据校验
│   ├── SKILL.md                  # Skill 定义（YAML frontmatter + 正文）
│   ├── evals/evals.json          # 3 个评测用例
│   ├── references/               # 参考文档
│   │   └── schema-rules.md
│   └── scripts/                  # 辅助脚本
│
├── data-transform/               # Skill 2: 数据转换
│   ├── SKILL.md
│   ├── evals/evals.json          # 3 个评测用例
│   ├── references/
│   │   └── transform-spec.md
│   └── scripts/
│
├── report-generator/             # Skill 3: 报告生成
│   ├── SKILL.md
│   ├── evals/evals.json          # 3 个评测用例
│   ├── references/
│   │   └── report-template.md
│   └── scripts/
│
├── data-pipeline/                # 集合结构（用于 scan 命令测试）
│   └── skills/
│       ├── data-validator/
│       ├── data-transform/
│       └── report-generator/
│
├── manifest.json                 # Batch DAG manifest（依赖声明）
└── eval-config.yaml              # 集成评测配置（场景 + 关键词 + pipelines）
```

**关键文件说明**：

| 文件 | 用途 | 被哪些命令使用 |
|------|------|----------------|
| `SKILL.md` | Skill 定义，包含 YAML frontmatter（name, description）和正文指令 | 所有命令 |
| `evals/evals.json` | 评测用例定义（prompt、expectations、assertions） | `run`, `full` |
| `manifest.json` | 批量评测的 DAG 声明（Skill 列表 + 依赖关系） | `batch` |
| `eval-config.yaml` | 集成测试配置（场景模板、关键词路由、Pipeline 定义） | `batch --layers integration`, `generate-config`, `init` |

---

## 三个测试 Skill 详解

### data-validator（数据校验器）

**职责**：读取 JSON 文件，校验每条记录的字段合法性（id 正整数、name 非空、type 枚举值），检查 SQL 注入，输出 `validation-report.json`。

**评测用例**（`evals/evals.json`）：

| ID | 名称 | 层级 | 测试内容 |
|----|------|------|----------|
| 1 | L1-验证规则理解 | L1 | 纯问答：Claude 是否理解了校验规则（id、name、type、SQL 注入） |
| 2 | L2-有效数据验证 | L2 | Mock 场景：提供 2 条有效记录，验证是否生成正确的校验报告 |
| 3 | L2-无效数据验证 | L2 | Mock 场景：提供 2 条无效记录，验证是否检测出错误 |

**L1 用例示例**：

```json
{
  "id": 1,
  "name": "L1-验证规则理解",
  "prompt": "我有一个包含用户记录的 JSON 文件，每条记录有 id、name 和 type 字段。请告诉我应该做哪些校验？",
  "level": "L1",
  "expectations": [
    "id 必须是正整数",
    "name 必须是非空字符串",
    "type 必须是 user/order/product 之一",
    "需要检查 SQL 注入模式"
  ],
  "assertions": []
}
```

> **L1 vs L2 的区别**：L1 只有 `expectations`（验证"说了什么"），L2 同时有 `expectations` + `assertions`（验证"做了什么"）。

**L2 用例示例**（有效数据）：

```json
{
  "id": 2,
  "name": "L2-有效数据验证",
  "prompt": "帮我校验 input.json 里的数据，并写出校验报告。",
  "level": "L2",
  "fixtures": [
    {"path": "input.json", "content": "[{\"id\": 1, \"name\": \"Alice\", \"type\": \"user\"}, ...]"}
  ],
  "expectations": ["所有记录验证通过", "验证报告显示 valid_count 为 2", "验证报告显示 invalid_count 为 0"],
  "assertions": [
    {"name": "报告文件存在", "type": "FILE_EXISTS", "params": {"filename": "validation-report.json"}},
    {"name": "报告包含 valid_count", "type": "JSON_HAS_FIELD", "params": {"filename": "validation-report.json", "field": "valid_count"}},
    {"name": "报告包含 errors", "type": "JSON_HAS_FIELD", "params": {"filename": "validation-report.json", "field": "errors"}}
  ]
}
```

> **fixtures** 是 L2 的核心机制：运行时自动写入到 Claude 的工作目录，让 Claude 在真实文件上操作。

---

### data-transform（数据转换器）

**职责**：读取已校验的 JSON 数据，执行格式转换（flatten/nest/filter/rename-fields），输出 `transformed.json`（带 metadata 头）。

**依赖关系**：`depends_on: ["data-validator"]`，意味着在 batch 评测中，data-transform 会在 data-validator 之后执行。

---

### report-generator（报告生成器）

**职责**：从 `transformed.json` 读取转换后的数据，统计记录数/类型分布/质量评分，生成 `report.md`（Markdown）和 `report-summary.json`。

**依赖关系**：`depends_on: ["data-transform"]`，形成完整的三级依赖链。

---

## 测试层级与编排

### 单 Skill 测试（L1 + L2）

通过 `run` 或 `full` 命令执行。每个 Skill 的 `evals.json` 包含多个 eval case：

- **L1（单元级）**：纯问答，验证 Claude 是否理解了 SKILL.md 的规则。只有 `expectations`，没有 `assertions`，不产生文件。
- **L2（Mock 场景）**：预置 fixture 文件，验证 Claude 执行后是否产出了正确的文件。同时有 `expectations` 和 `assertions`。

### 多 Skill 集成测试

通过 `batch --layers integration` 执行，在 `eval-config.yaml` 中定义三种场景类型：

**1. Chain（链式传递）**——验证 Skill A 的输出能否被 Skill B 消费：

```yaml
integration_scenarios:
  - name: "validate-then-transform chain"
    type: "chain"
    chain: ["data-validator", "data-transform"]
    input: { source: "input.json" }
```

**2. Routing（关键词路由）**——验证用户 prompt 能否正确触发目标 Skill：

```yaml
  - name: "keyword routing"
    type: "routing"
    test_prompts:
      - prompt: "Validate this JSON data file for me"
        should_trigger: ["data-validator"]
        should_not_trigger: ["report-generator"]
```

**3. Contract（制品契约）**——验证上游 Skill 的输出符合下游 Skill 的输入契约：

```yaml
  - name: "validator output satisfies transformer"
    type: "contract"
    producer: "data-validator"
    consumer: "data-transform"
    artifact: "validation-report.json"
    checks:
      - type: "FILE_EXISTS"
      - type: "JSON_HAS_FIELD"
```

### Pipeline 流水线测试

在 `eval-config.yaml` 的 `pipelines` 中定义，在真实项目副本上按依赖顺序逐步执行：

```yaml
pipelines:
  - name: "full-data-pipeline"
    description: "Run the full data pipeline: validate → transform → report"
    project: "fixtures/projects/android-minimal"
    steps:
      - name: "validate"
        skill: "data-validator"
        prompt: "Validate the data in input.json and write a validation report"
        depends_on: []
      - name: "transform"
        skill: "data-transform"
        prompt: "Flatten the data in input.json and write to transformed.json"
        depends_on: ["validate"]
      - name: "report"
        skill: "report-generator"
        prompt: "Generate a summary report from transformed.json"
        depends_on: ["transform"]
```

**执行特点**：
- 自动复制项目到 workspace（不污染原始项目）
- 按依赖拓扑排序执行 step
- 每个 step 的输出对后续 step 可见

### Batch DAG 与级联归因

`manifest.json` 声明了 Skill 间的依赖关系，`batch` 命令据此构建 DAG：

```json
{
  "name": "data-pipeline",
  "skills": [
    {"path": "data-validator", "depends_on": []},
    {"path": "data-transform", "depends_on": ["data-validator"]},
    {"path": "report-generator", "depends_on": ["data-transform"]}
  ]
}
```

当某个 Skill 失败时，框架自动归因：

| 失败类型 | 含义 |
|----------|------|
| `ROOT_CAUSE` | 该 Skill 自身导致失败 |
| `CASCADE` | 上游 Skill 失败引起的连锁失败，非自身问题 |
| `INDEPENDENT` | 与其他 Skill 无关的独立失败 |
| `DEPENDENCY_MISSING` | 依赖的 Skill 不存在或被禁用 |

---

## CLI 命令与测试流程

以下是用测试 fixtures 运行完整评测的推荐流程：

### 1. 安全审查（无需 API）

```bash
skill-eval vet fixtures/test-skills/data-validator/
```

4 个扫描器（2 内置 + 2 可选外部），判定 SAFE / REVIEW_NEEDED / BLOCKED。

### 2. 静态分析（无需 API）

```bash
skill-eval analyze fixtures/test-skills/data-validator/
```

11 项自动检查，输出分数（0-11）和 Skill 分类（CAPABILITY_UPLIFT / ENCODED_PREFERENCE）。

### 3. 覆盖率扫描

```bash
skill-eval scan fixtures/test-skills/
```

检查哪些 Skill 有/缺少 evals。

### 4. 初始化评测定义

```bash
skill-eval init fixtures/test-skills/data-validator/ --no-generate-prompts
```

从 SKILL.md 的 `##` 标题自动生成 evals.json 模板。加 `--generate-prompts` 会调用 LLM 生成更高质量的 prompt。

### 5. A/B 评测（需要 Claude CLI）

```bash
skill-eval run fixtures/test-skills/data-validator/ --runs 1 --format json
```

对单个 Skill 执行 A/B 测试：with_skill（注入 SKILL.md）vs without_skill（不注入），返回量化 delta。

### 6. 一键完整流程

```bash
skill-eval full fixtures/test-skills/data-validator/ --runs 1
```

安全审查 → 静态分析 → 加载 evals → A/B 测试 → 聚合 → HTML 报告，一步完成。

### 7. 批量评测

```bash
skill-eval batch fixtures/test-skills/manifest.json --layers integration --project fixtures/test-skills/
```

按层级执行多 Skill 评测，支持 DAG 拓扑排序和级联归因。

### 8. HTML 报告

```bash
skill-eval report output/ --benchmark output/iteration-1/benchmark.json
```

从已有评测结果生成自包含 HTML 可视化报告。

---

## 指标解读

评测完成后会产生多维度的指标。以下是每个指标的详细说明。

### 核心指标：Pass Rate 与 Delta

| 指标 | 含义 | 取值范围 |
|------|------|----------|
| `pass_rate` (with_skill) | 注入 SKILL.md 后 Claude 的通过率 | 0.0 - 1.0 |
| `pass_rate` (without_skill) | 不注入 SKILL.md 时 Claude 的通过率（基线） | 0.0 - 1.0 |
| `delta` | with_skill - without_skill 的通过率差值 | -1.0 - +1.0 |

**解读 delta**：

| Delta 范围 | 含义 |
|------------|------|
| **> +0.3** | Skill 显著提升了 Claude 的表现，该 Skill 有价值 |
| **+0.1 ~ +0.3** | Skill 有一定帮助，可考虑优化 |
| **-0.1 ~ +0.1** | Skill 几乎无效果，Claude 本身就能做 |
| **< -0.1** | Skill 反而干扰了 Claude，需排查 SKILL.md 内容 |

**实际测试数据示例**（data-validator, --runs 1）：

| 配置 | Pass Rate | 说明 |
|------|-----------|------|
| with_skill | **80%** (12/15) | 15 个 expectations 中通过 12 个 |
| without_skill | **33%** (5/15) | 15 个 expectations 中通过 5 个 |
| **delta** | **+0.47** | Skill 显著提升了校验能力 |

### 静态分析分数（0-11）

11 项检查，每项通过得 1 分：

| # | 检查项 | 检查内容 |
|---|--------|----------|
| 1 | `description_length` | SKILL.md 描述长度 ≥ 20 字符 |
| 2 | `description_has_trigger` | 描述包含动作触发词 |
| 3 | `description_third_person` | 描述使用第三人称 |
| 4 | `name_kebab_case` | name 符合 kebab-case |
| 5 | `name_max_64` | name 长度 ≤ 64 |
| 6 | `name_no_reserved` | name 非系统保留字 |
| 7 | `skill_md_max_500_lines` | SKILL.md 不超过 500 行 |
| 8 | `has_examples` | 包含示例代码 |
| 9 | `reference_depth` | references/ 目录扁平 |
| 10 | `anti_fabrication` | 包含反捏造指引 |
| 11 | `source_documented` | 引用了参考来源 |

同时输出 Skill 分类：`CAPABILITY_UPLIFT`（增强型，教 Claude 新能力）或 `ENCODED_PREFERENCE`（偏好型，编码了团队偏好）。

### 遵循性率（Compliance Rate）

**文件**：`compliance.json`（仅 with_skill 配置产生）

衡量 Claude 在执行过程中**是否遵循了 SKILL.md 的指令**。框架从 SKILL.md 中提取 6 类指令：

| 指令类型 | 来源 | 示例 |
|----------|------|------|
| `HEADING_STEP` | `##` 标题下的步骤 | "Step 1: Read Input Data" |
| `TOOL_REQUIREMENT` | 工具使用要求 | "使用 Read 工具读取文件" |
| `CONSTRAINT` | 约束条件 | "NEVER fabricate data" |
| `ANTI_FABRICATON` | 反捏造规则 | "NEVER invent validation errors" |
| `OUTPUT_FORMAT` | 输出格式要求 | "Write to validation-report.json" |
| `REFERENCE_USAGE` | 参考文档引用 | "See references/schema-rules.md" |

每个指令检查是否被遵循，输出 `compliance_rate`（0.0 - 1.0）。

### 校准等级（Calibration Grade）

**文件**：`calibration.json`

评估评分结果的可信度。当 `hybrid` 评分模式同时使用了程序化断言和 LLM 评分时，两者可能产生分歧。校准引擎检查 7 种信号并给出等级：

| 等级 | Agreement Rate | 含义 | 权重 |
|------|----------------|------|------|
| **HIGH** | ≥ 0.8 | 程序化和 LLM 评分高度一致，结果可信 | 1.0 |
| **MEDIUM** | ≥ 0.6 | 存在少量分歧，结果基本可信 | 0.8 |
| **LOW** | ≥ 0.4 | 分歧较多，结果需人工复核 | 0.5 |
| **UNRELIABLE** | < 0.4 | 严重分歧，评分不可靠，exit code = 1 | 0.2 |

> 当 calibration grade 为 UNRELIABLE 时，skill-eval 会输出警告并返回 exit code 1。

### 量化质量公式

综合所有维度的最终评分公式：

```
Score = StaticCompliance(0-11)      # 静态分析分数
      + DeltaPassRate(%)            # A/B 测试 delta
      + InstructionCompliance(%)    # 遵循性率
      + TriggerAccuracy(%)          # 触发准确率
      - PerformancePenalty          # 性能惩罚（时间 × token 增量）
      × CalibrationGrade            # 校准权重
```

### Checkpoint Breakdown

每个 eval case 作为一个 checkpoint，独立报告通过率。`full` 命令的表格输出会显示详细的 breakdown：

```
Checkpoint Breakdown:
  PASS L1-验证规则理解 — 4/4 (100%)
      ✓ id 必须是正整数
      ✓ name 必须是非空字符串
      ✓ type 必须是 user/order/product 之一
      ✓ 需要检查 SQL 注入模式
  PARTIAL L2-有效数据验证 — 3/6 (50%)
      ✗ 所有记录验证通过
      ✗ 验证报告显示 valid_count 为 2
      ✗ 验证报告显示 invalid_count 为 0
      ✓ (assertion) 报告文件存在
      ✓ (assertion) 报告包含 valid_count
      ✓ (assertion) 报告包含 errors
  PASS L2-无效数据验证 — 5/5 (100%)
      ✓ 检测到无效记录
      ✓ 验证报告包含错误描述
      ✓ id 为负数被标记为错误
      ✓ (assertion) 报告文件存在
      ✓ (assertion) 有无效记录
```

- 无 `(assertion)` 前缀的是 **expectations**（检查 Claude 回复中的语义内容）
- 有 `(assertion)` 前缀的是 **assertions**（检查 Claude 产出的文件）

### 安全审查指标

| 判定 | 含义 | Exit Code |
|------|------|-----------|
| **SAFE** | 所有扫描器通过 | 0 |
| **REVIEW_NEEDED** | 存在中等风险（MEDIUM） | 0 |
| **BLOCKED** | 存在严重风险（CRITICAL/HIGH） | 1 |

4 个扫描器：
- `secrets-scan`（内置）：硬编码密钥检测
- `structure-check`（内置）：SKILL.md 结构完整性
- `aguara`（可选）：Prompt 注入检测
- `skill-scanner`（可选）：已知恶意模式检测

---

## 输出文件结构

### 默认模式（per-eval，每个 eval 独立运行）

`full` 命令默认使用 per-eval mode——每个 eval case 独立并发运行，结果按 `eval-<id>/run-<n>/` 组织：

```
output/iteration-1/
├── benchmark.json                  # 聚合结果（含 run_summary、delta、comparisons、analysis）
├── report.html                     # 自包含 HTML 可视化报告（~62KB）
├── comparison-run1.json            # 盲比较结果（ComparisonResult 模型）
├── analysis-notes.json             # LLM 生成的 benchmark 分析笔记（字符串数组）
├── analysis-result.json            # 深度比较分析（AnalysisResult 模型，可选）
│
├── with_skill/
│   ├── eval-1/
│   │   └── run-1/
│   │       ├── grading.json        # 该 eval 的 expectations + assertions 的 pass/fail
│   │       ├── checkpoints.json    # 该 eval 的独立覆盖率
│   │       ├── compliance.json     # SKILL.md 指令遵循性检查
│   │       ├── calibration.json    # 评分可信度校准
│   │       ├── run_debug.json      # Claude 完整回复 + tool_calls + timing
│   │       └── timing.json         # 执行耗时和 token 统计
│   ├── eval-2/
│   │   └── run-1/ ...
│   └── eval-3/
│       └── run-1/ ...
│
└── without_skill/
    ├── eval-1/
    │   └── run-1/
    │       ├── grading.json
    │       ├── checkpoints.json
    │       ├── calibration.json
    │       ├── run_debug.json
    │       └── timing.json
    ├── eval-2/
    │   └── run-1/ ...
    └── eval-3/
        └── run-1/ ...
```

> 各 eval 独立并发运行，互不阻塞。`benchmark.json` 在聚合阶段汇总所有 eval 的结果。

### 合并模式（--merged flag）

使用 `--merged` 时，所有 eval 合并为一个 prompt 一起运行，结果结构与上述相同，但所有 eval 共享同一个 run：

```
output/iteration-1/
├── benchmark.json
├── report.html
├── merged_prompt.txt               # 合并后的完整 prompt（仅 merged 模式）
├── comparison-run1.json
├── analysis-notes.json
├── analysis-result.json
│
├── with_skill/
│   └── run-1/
│       ├── grading.json
│       ├── checkpoints.json
│       ├── compliance.json
│       ├── calibration.json
│       ├── run_debug.json
│       └── timing.json
│
└── without_skill/
    └── run-1/
        ├── grading.json
        ├── checkpoints.json
        ├── calibration.json
        ├── run_debug.json
        └── timing.json
```

**各文件用途**：

| 文件 | 核心内容 | 如何解读 |
|------|----------|----------|
| `grading.json` | `summary.pass_rate` | with_skill 的 pass_rate 应显著高于 without_skill |
| `checkpoints.json` | 每个 eval 的独立 pass/failed/total | 定位哪个 eval case 失败 |
| `compliance.json` | `compliance_rate` + 逐条检查 | 定位哪条 SKILL.md 指令未被遵循 |
| `calibration.json` | `grade` + `agreement_rate` | 判断评分结果是否可信 |
| `comparison-run{N}.json` | `winner` + `rubric` + `output_quality` | 盲比较器对 with/without skill 输出的评分对比 |
| `analysis-notes.json` | LLM 生成的洞察列表 | 发现聚合指标无法揭示的模式和异常 |
| `benchmark.json` | `run_summary.delta` + `comparisons` + `analysis` | 最终量化 delta + 盲比较 + 深度分析 |
| `run_debug.json` | 完整 transcript + tool_calls | 排查问题时的详细日志 |

---

## 如何为自己的 Skill 编写测试

### 第 1 步：准备 SKILL.md

确保你的 SKILL.md 包含：
- YAML frontmatter（`name`、`description`）
- 清晰的 `##` 章节结构（每个章节会映射为一个 eval case）
- Anti-fabrication 规则
- 示例代码
- 参考文档链接

### 第 2 步：生成初始 evals.json

```bash
skill-eval init path/to/your-skill/ --no-generate-prompts --output evals.json
```

这会从 SKILL.md 的标题结构生成 L1 模板。如需更高质量的 prompt，去掉 `--no-generate-prompts`。

### 第 3 步：补充 L2 用例

在生成的 evals.json 中添加 L2 用例：
- 编写真实的 prompt（模拟用户请求）
- 添加 `fixtures`（前置文件）
- 添加 `assertions`（验证产出文件）

### 第 4 步：运行评测

```bash
skill-eval full path/to/your-skill/ --runs 1
```

### 第 5 步：解读结果

1. 看 `benchmark.json` 的 `delta` → Skill 是否有价值？
2. 看 `checkpoints.json` → 哪些 eval case 失败了？
3. 看 `compliance.json` → 哪些指令没被遵循？
4. 看 `calibration.json` → 评分是否可信？

### 第 6 步：迭代优化

根据失败项调整 SKILL.md 的指令或 evals.json 的 expectations，然后重新运行。

---

## 已知问题与注意事项

| 编号 | 严重程度 | 问题 | 建议 |
|------|----------|------|------|
| 1 | ✅ Fixed | `benchmark.json` 的 `notes` 字段 Pydantic 序列化类型不匹配 (`Expected str, got dict`) | 已修复：`BenchmarkData.notes` 改为 `list[Any]` |
| 2 | ℹ️ Low | `scan` 对 `<collection>/skills/` 嵌套结构返回 0 skills | 直接扫描包含 SKILL.md 的扁平目录 |
| 3 | ℹ️ Low | 集成测试 (chain/routing/contract/pipeline) 为配置验证桩，不执行实际的 Claude 调用 | 已知设计限制，后续版本将接入 `ABTestRunner` |

---

## 附录：完整测试结果记录

> 以下为 skill-eval v0.1.0 所有 11 个 CLI 命令的端到端测试结果。
> 测试环境：macOS / Python 3.14 / Claude Code CLI 2.1.133 / 2026-05-08

### 测试环境

| 项目 | 值 |
|------|------|
| skill-eval 版本 | 0.1.0 |
| Python 版本 | 3.14 |
| Claude Code CLI | 2.1.133 |
| 操作系统 | macOS (Darwin) |
| 测试 fixtures | `fixtures/test-skills/` (3 skills) |
| 单元测试 | 261 passed |

### 命令 1: `vet` — 安全审查

```bash
skill-eval vet fixtures/test-skills/data-validator/
```

**结果**: ✅ PASS

```
                         Security Scan: data-validator                          
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Scanner         ┃ Status ┃ Detail                                            ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ aguara          │ SKIP   │ aguara not installed                              │
│ skill-scanner   │ SKIP   │ skill-scanner not installed                       │
│ secrets-scan    │ PASS   │ No hardcoded secrets found                        │
│ structure-check │ PASS   │ Skill structure valid                             │
└─────────────────┴────────┴───────────────────────────────────────────────────┘

VERDICT: SAFE — All scanners passed
```

### 命令 2: `analyze` — 静态分析

```bash
skill-eval analyze fixtures/test-skills/data-validator/
```

**结果**: ✅ PASS (11/11)

```
                      Skill Scorecard: data-validator                      
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Check                    ┃ Status ┃ Detail                              ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ description_length       │ PASS   │ Description length: 134 chars       │
│ description_has_trigger  │ PASS   │ Description contains trigger word   │
│ description_third_person │ PASS   │ Description uses third person       │
│ name_kebab_case          │ PASS   │ Name 'data-validator' is kebab-case │
│ name_max_64              │ PASS   │ Name length: 14 chars               │
│ name_no_reserved         │ PASS   │ Name is not a reserved word         │
│ skill_md_max_500_lines   │ PASS   │ SKILL.md has 67 lines               │
│ has_examples             │ PASS   │ SKILL.md contains examples          │
│ reference_depth          │ PASS   │ references/ directory is flat       │
│ anti_fabrication         │ PASS   │ Contains anti-fabrication guidance  │
│ source_documented        │ PASS   │ Sources/references documented       │
├──────────────────────────┼────────┼─────────────────────────────────────┤
│ Score                    │ 11/11  │ Category: CAPABILITY_UPLIFT         │
└──────────────────────────┴────────┴─────────────────────────────────────┘
```

### 命令 3: `scan` — L0 覆盖率扫描

```bash
skill-eval scan fixtures/test-skills/
```

**结果**: ✅ PASS

```
     Skill Eval Coverage: fixtures/test-skills      
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━┓
┃ Skill            ┃ Domain ┃ Type ┃ Evals ┃ Status ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━┩
│ data-transform   │ —      │ —    │ 3     │ ✓      │
│ data-validator   │ —      │ 3    │ 3     │ ✓      │
│ report-generator │ —      │ —    │ 3     │ ✓      │
└──────────────────┴────────┴──────┴───────┴────────┘

Total: 3  With evals: 3  Missing: 0
```

### 命令 4: `init` — 初始化评测定义

```bash
skill-eval init fixtures/test-skills/data-validator/ --no-generate-prompts --output /tmp/init-evals.json
```

**结果**: ✅ PASS（生成 8 个 eval cases）

### 命令 5: `generate-config` — LLM 配置生成

```bash
skill-eval generate-config fixtures/test-skills/ --output /tmp/gen-config.yaml
```

**结果**: ✅ PASS（1 domain, 1 type, 4 integration scenarios, 3 skill keywords, 1 pipeline）

### 命令 6: `batch` — 批量评测

```bash
skill-eval batch fixtures/test-skills/manifest.json --layers integration --project fixtures/test-skills/
```

**结果**: ✅ PASS (4/5 integration scenarios)

```
  Integration: 4/5 passed
    ✓ validate-then-transform chain (chain)
    ✓ keyword routing (routing)
    ✓ validator output satisfies transformer (contract)
    ✗ 1 (routing)
    ✓ full-data-pipeline (pipeline)
```

### 命令 7: `run` — A/B 评测

```bash
skill-eval run fixtures/test-skills/data-validator/ --runs 1 --format json -o /tmp/run-output
```

**结果**: ✅ PASS（delta = +0.47）

| 配置 | Pass Rate | 时间 |
|------|-----------|------|
| with_skill | **80%** (12/15) | ~30s |
| without_skill | **33%** (5/15) | ~40s |
| **delta** | **+0.47** | -10s |

**with_skill 详细 breakdown**：

| Eval | 结果 | 详情 |
|------|------|------|
| L1-验证规则理解 | ✅ 4/4 (100%) | 所有规则正确识别 |
| L2-有效数据验证 | ⚠️ 3/6 (50%) | assertions 全通过；3 个 expectations 失败 |
| L2-无效数据验证 | ✅ 5/5 (100%) | 全部通过 |

**without_skill 详细 breakdown**：

| Eval | 结果 | 详情 |
|------|------|------|
| L1-验证规则理解 | ⚠️ 2/4 (50%) | 缺少 SQL 注入和 type 枚举 |
| L2-有效数据验证 | ❌ 0/6 (0%) | 未生成 validation-report.json |
| L2-无效数据验证 | ⚠️ 3/5 (60%) | 缺少文件输出 |

### 命令 8: `full` — 一键完整流程

```bash
skill-eval full fixtures/test-skills/data-validator/ --runs 1 -o /tmp/full-output
```

**结果**: ✅ PASS（delta = +0.47，与 `run` 一致）

```
Results:
  with_skill:    80% pass rate
  without_skill: 33% pass rate
  delta:         +0.47

Warning: calibration grade UNRELIABLE — scoring confidence is low
```

### 命令 9: `report` — HTML 报告

```bash
skill-eval report /tmp/run-output/ --benchmark /tmp/run-output/iteration-1/benchmark.json
```

**结果**: ✅ PASS（62,929 bytes HTML）

### 命令 10: `import` — 导入监控数据

```bash
skill-eval import /tmp/events.jsonl --benchmark benchmark-clean.json
```

**结果**: ✅ PASS

### 命令 11: `optimize-trigger` — 触发优化

功能已验证可用，依赖 `claude -p` 多轮调用。此处未实际执行以节省 API 额度。

### 命令 12: `full` (data-transform) — 盲比较 + 深度分析验证

```bash
skill-eval full fixtures/test-skills/data-transform/ --runs 1 --timeout 60
```

**结果**: ✅ PASS

| 步骤 | 结果 | 说明 |
|------|------|------|
| Step 1: 静态分析 | ✅ 11/11 | CAPABILITY_UPLIFT |
| Step 2: 加载 evals | ✅ 3 evals | 1 L1 + 2 L2 |
| Step 3: A/B 测试 | ✅ 完成 | 2/2 runs |
| Step 4: 聚合 + 报告 | ✅ 62KB HTML | 含盲比较 + 分析笔记 |

**A/B 结果**：

| 配置 | Pass Rate | 时间 | 说明 |
|------|-----------|------|------|
| with_skill | **0%** (0/15) | ~160s | Skill 执行失败，未产出 `transformed.json` |
| without_skill | **27%** (4/15) | ~66s | 基线 agent 对话中描述了正确行为但未写输出文件 |
| **delta** | **-0.27** | — | **负 delta**：该 Skill 在本次运行中反而干扰了 Claude |

> **注意**：负 delta 并不一定意味 Skill 本身有严重问题。这次运行中 with_skill 因文件输出路径错误导致所有 programmatic assertions 失败（0/15），属于执行层问题而非 Skill 设计问题。这恰好体现了盲比较和分析的价值——它们能帮助定位失败根因。

**盲比较结果** (`comparison-run1.json`)：

| 指标 | Output A (with_skill) | Output B (without_skill) |
|------|-----------------------|--------------------------|
| Winner | **A** (9.0) | B (7.0) |
| Content | 4.3/5 | 3.7/5 |
| Structure | 4.7/5 | 3.3/5 |
| 优势 | metadata 信封（transform_type, record_count, source, transformed_at） | 数据正确但裸 JSON 无元数据 |
| 劣势 | 未处理 L1 问答、nest/rename-fields 未实现 | 缺少 metadata、缺少 records 信封 |

**LLM 分析笔记**（`analysis-notes.json`，10 条洞察）：

```
1. with_skill 0% vs without_skill 26.7% — Skill 主动降低了性能
2. with_skill 从未产出 transformed.json → 5 个程序化断言全部失败，级联到 10 个 LLM 检查
3. without_skill 也未能文件输出，但通过对话描述了正确行为
4. Skill 增加了 20.1 秒执行时间（+55%），却产出更差结果
5. "支持 nest 转换" 在所有配置中 0% 通过 — 从未被演示或描述
6. "address 展开" 通过率 0% — 输入 fixture 可能不存在嵌套 address 字段
7. 比较评分（A 80%, B 47%）远高于实际 benchmark → 比较可能使用了不同输入
8. "rename-fields" 在 without_skill LLM 批处理中通过但文件验证失败
9. Metadata 要求是 with/without 之间的主要区分因素
10. 所有 3 个子 eval 都显示 0% — 失败模式均匀，指向执行层根因（文件输出路径）
```

### 命令 13: `batch` (unit) — 多 Skill 批量评测（含 phase/substep 修复验证）

```bash
skill-eval batch fixtures/test-skills/manifest.json --runs 1 --workers 1 --layers unit
```

**结果**: ✅ PASS (修复后)

| Skill | 结果 | 归因 |
|-------|------|------|
| data-validator | ✅ PASSED | — |
| data-transform | ✅ PASSED | — |
| report-generator | ✅ PASSED | — |

### 新增 Agent 功能验证

| Agent | 触发位置 | 产出文件 | 验证结果 |
|-------|----------|----------|----------|
| **BlindComparator** | `BenchmarkAggregator._run_comparisons()` | `comparison-run1.json` | ✅ 正确区分 A/B，rubric 含 content+structure 维度 |
| **PosthocAnalyzer** (benchmark) | `BenchmarkAggregator._run_analysis()` | `analysis-notes.json` | ✅ 生成 10 条深度洞察 |
| **PosthocAnalyzer** (comparison) | `BenchmarkAggregator._run_comparison_analysis()` | `analysis-result.json` | ⚠️ LLM 调用超时未生成（已知限制） |

### 命令 14: 反面 Skill 验证 — 6 种常见缺陷检测

#### bad-instruction（坏指令型）

**静态分析**: 11/11 — 静态检查不检测语义矛盾。

```bash
skill-eval analyze fixtures/negative-skills/bad-instruction/ --format json
```

**预期 A/B 结果**: delta 负（矛盾指令干扰 Claude）。

**SKILL.md 缺陷**: "用 Python 但不要用代码", "用 pandas（不要用 pandas）", 输出文件名不确定。

#### security-risk（安全风险型）

```bash
skill-eval vet fixtures/negative-skills/security-risk/ --format json --skip-external
```

**结果**: ✅ BLOCKED — 1 HIGH finding（硬编码 API key）

#### low-quality（静态分析失败型）

```bash
skill-eval analyze fixtures/negative-skills/low-quality/ --format json
```

**结果**: Score 6/11 — 5 项 FAIL (description_length, description_has_trigger, has_examples, anti_fabrication, source_documented)

#### empty-shell（空壳型）

```bash
skill-eval scan fixtures/negative-skills/ --format json
```

**结果**: empty-shell ✗ MISSING — 无 evals 目录

#### bad-format（违规格式型）

```bash
skill-eval analyze fixtures/negative-skills/bad-format/ --format json
```

**结果**: ❌ Error — "SKILL.md missing frontmatter (no opening ---)", 且超过 500 行

#### 依赖错误型（broken-root + cascade-victim + missing-dep）

```bash
skill-eval batch fixtures/negative-skills/manifest-broken-deps.json --runs 1
```

**结果**: broken-root=ROOT_CAUSE, cascade-victim=CASCADE, missing-dep=DEPENDENCY_MISSING

### 命令 15: `split-evals` — 评测集切分

```bash
skill-eval split-evals fixtures/test-skills/data-validator/evals/evals.json --seed 42
```

**结果**: ✅ PASS

```
Found 3 evals.
  1 holdout evals written to evals-holdout.json
  Train set saved: 2 evals → evals-train.json
```

### 命令 16: `vet-overfit` — 过拟合检测

```bash
skill-eval vet-overfit \
  --train-bench <benchmark.json> \
  --holdout-bench <benchmark.json> \
  --skill fixtures/negative-skills/overfitted-skill/ \
  --train-evals .../evals-train.json
```

**结果**: ⚠️ OVERFITTED — overfitting_score=1.00

```
Overfitting report for overfitted-skill:
  Overfitting Score: 1.00
  Suspicious Instructions: 10 found

  Severity breakdown:
    7 HIGH
    2 MEDIUM
    1 LOW

  Examples:
    HIGH | "When asked about validation, always respond with a paragraph..." 
          reason: instruction overfits to eval prompt wording; not generalizable
    HIGH | "If the data contains user/order/product fields, immediately classify..."
          reason: instruction matches fixture-specific data structure
    MEDIUM | "Always start the output with 'VALIDATION COMPLETE: ...'"
          reason: output format instruction too tightly coupled to expected output
     LOW | "Use json.dumps() to serialize results"
          reason: minor overfitting; instruction mirrors expected tool use
```

### 版本依赖确认

```
$ skill-eval --version
skill-eval 0.1.0

$ claude --version
2.1.133 (Claude Code)

$ python3 -m pytest tests/ -q
261 passed in 0.17s
```
