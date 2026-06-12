# Skill-Eval 参考手册

本文档包含 Skill-Eval 的完整技术细节。

---

## 目录

- [CLI 命令详细参数](#cli-命令详细参数)
  - [vet — 安全审查](#vet--安全审查)
  - [init — 初始化评测定义](#init--初始化评测定义)
  - [analyze — 静态分析](#analyze--静态分析)
  - [run — A/B 评测](#run--ab-评测)
  - [report — HTML 报告](#report--html-报告)
  - [batch — 批量评测](#batch--批量评测)
  - [batch-report — 批量 HTML 报告](#batch-report--批量-html-报告)
- [评测体系](#评测体系)
- [JSON 输出格式](#json-输出格式)
- [项目结构](#项目结构)
- [与 Skill-Creator 的关系](#与-skill-creator-的关系)
- [Exit Code](#exit-code)
- [常见问题](#常见问题)

---

## CLI 命令详细参数

### `vet` — 安全审查

```bash
skill-eval vet path/to/skill/                # 表格输出 + 自动写 vet-report.json
skill-eval vet path/to/skill/ --format json  # JSON 输出
skill-eval vet path/to/skill/ --output report.json  # 指定输出路径
skill-eval vet path/to/skill/ --skip-external # 跳过外部扫描器
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` | 无 | 输出文件路径 |
| `--skip-external` | 关闭 | 跳过外部扫描器 |

**四个扫描器**：

| # | 扫描器 | 依赖 | 检查内容 |
|---|--------|------|----------|
| 1 | aguara | 可选（Go） | Prompt 注入、混淆 |
| 2 | skill-scanner | 可选（pip） | 已知恶意模式、CVE |
| 3 | secrets-scan | 内置 | 硬编码 API Key / Token / 凭证、Base64 |
| 4 | structure-check | 内置 | 缺失 SKILL.md、缺少 frontmatter、危险 shell 命令 |

**判定规则**：

| Verdict | 含义 | Exit Code |
|---------|------|-----------|
| **SAFE** | 所有扫描器通过 | 0 |
| **REVIEW_NEEDED** | 存在中等风险（MEDIUM） | 0 |
| **BLOCKED** | 存在严重风险（CRITICAL/HIGH） | 1 |

### `init` — 初始化评测定义

根据 SKILL.md 内容自动生成 `evals.json`。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--num-evals` | 无 | 最多生成的 eval case 数量 |

### `analyze` — 静态分析

11 项自动化体检，**零 API 成本，<1s 完成**。

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
| 10 | `anti_fabrication` | 包含反捏造指引 |
| 11 | `source_documented` | 引用了参考来源 |

输出还包含 Skill 分类：`CAPABILITY_UPLIFT` 或 `ENCODED_PREFERENCE`。

### `run` — A/B 评测

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找 | evals.json 路径 |
| `--runs` | 3 | 每种配置的运行次数 |
| `--model` | 用户默认模型 | 模型名 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--timeout` | 300 | 每次运行超时（秒） |
| `--max-turns` | 10 | claude -p 最大轮数 |
| `--scoring` | hybrid | 评分模式：hybrid / programmatic / llm |
| `--compliance-gate` | 关闭 | 遵循性率 < 0.5 时强制失败 |
| `--no-calibration` | 关闭 | 跳过评分校准 |
| `--output` | output/ | 输出目录 |
| `--eval-samples` | 无 | 运行 eval 的数量或比例 |
| `--format` | table | 输出格式：table / json |

**输出目录结构**（per-eval 模式）：

```
output/iteration-1/
  with_skill/
    eval-<id>/
      run-1/
        outputs/        ← Claude 产生的文件
        grading.json
        compliance.json
        calibration.json
        run_debug.json
        timing.json
  without_skill/
    eval-<id>/
      run-1/
        outputs/
        grading.json
        calibration.json
        run_debug.json
        timing.json
   benchmark.json
   scorecard.json
   vet-report.json
   report.html
```

### `report` — HTML 报告

```bash
skill-eval report output/iteration-1/        # 生成静态 report.html
skill-eval report output/iteration-1/ --open # 生成并打开
```

自动发现 `benchmark.json`、`scorecard.json`、`vet-report.json`。

两个标签页：
- **输出** — 浏览 eval 的 prompt、输出文件、评分详情
- **基准测试** — 安全面板 + 评分卡 + 异常检测 + 通过率对比 + 盲评对比 + 深度分析 + 检查点明细

### `batch-report` — 批量 HTML 报告

```bash
skill-eval batch-report batch-workspace/ --open
```

展示多 skill 对比、通过率对比、各 Skill 详情。

### `batch` — 批量评测

**Manifest 格式**：

```json
{
  "name": "my-project-skills",
  "skills": [
    {"path": "skills/data-transform", "depends_on": []},
    {"path": "skills/report-gen", "depends_on": ["skills/data-transform"]}
  ]
}
```

**测试层**：

| 层 | 测试内容 | 需要什么 |
|----|----------|----------|
| `unit` | 每个 Skill 独立 A/B 测试 | Skill 目录 + evals.json |

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--layers` | unit | 测试层，逗号分隔 |
| `--runs` | 3 | 每种配置的运行次数 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--timeout` | 300 | 每次运行超时（秒） |

---

## 评测体系

### 三层质量保障

```
┌─────────────────────────────────────────────────────────────┐
│                    Skill 质量评估                            │
├─────────────┬─────────────────┬─────────────────────────────┤
│  评分引擎    │  遵循性引擎      │  评分校准                   │
│ (Scoring)   │  (Compliance)   │  (Calibration)              │
│             │                 │                             │
│ 输出正确？   │  过程遵循？      │  评分可信？                  │
│             │                 │                             │
│ grading.json│ compliance.json │ calibration.json            │
└─────────────┴─────────────────┴─────────────────────────────┘
```

| 场景 | 评分 | 遵循性 | 正确判断 |
|------|------|--------|----------|
| 输出正确且过程遵循 | PASS | PASS | Skill 真正有效 |
| 输出正确但过程违规 | PASS | FAIL | 输出正确是巧合，不能归功于 Skill |
| 输出错误但过程遵循 | FAIL | PASS | Skill 逻辑正确但执行失败，需改进 |

### 量化公式

```
Score = StaticCompliance(0-11)
      + DeltaPassRate(%)
      + InstructionCompliance(%)
      - PerformancePenalty(TimeDelta × TokenDelta)
      × CalibrationGrade(HIGH=1.0, MEDIUM=0.8, LOW=0.5, UNRELIABLE=0.2)
```

### 评分模式

| 模式 | 说明 | LLM 调用 | 适用场景 |
|------|------|----------|----------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分 | 是 | 通用场景 |
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
| `CROSS_FILE_CONSISTENCY` | 检查相对 import | `include`(可选, glob) |
| `CODE_COMPILES` | 基础语法检查 | `include`(可选, glob) |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | `plugin_name` |

### 测试层级

| 层级 | 测试什么 | 需要什么 |
|------|----------|----------|
| **L1**（单元级） | 单个 Skill 的规则理解是否正确 | evals.json + expectations |
| **L2**（Mock 场景） | Skill 在有 fixture 的场景下能否正确产出文件 | evals.json + assertions + fixtures |

---

## JSON 输出格式

所有 JSON 输出与 skill-creator 的 schema 兼容。

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
| `expected_output` | 否 | 预期输出的文字描述 |
| `level` | 否 | 测试层级：`L1`/`L2`，默认 `L1` |
| `fixtures` | 否 | 前置文件列表 |
| `expectations` | 否 | 语义预期语句列表 |
| `assertions` | 否 | 程序化断言列表 |

### grading.json

```json
{
  "expectations": [
    {"text": "The output includes X", "passed": true, "evidence": "Found in transcript Step 3"}
  ],
  "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
  "execution_metrics": {"tool_calls": {"Read": 3, "Write": 2}, "total_tool_calls": 5},
  "timing": {"total_tokens": 84852, "duration_ms": 23332, "total_duration_seconds": 23.3},
  "error": ""
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
  },
  "notes": [...],
  "comparisons": [...],
  "analysis": {...}
}
```

### 输出文件说明

`run` 命令完成后，`output/iteration-1/` 目录结构：

| 文件 | 内容 |
|------|------|
| `benchmark.json` | 聚合结果 |
| `report.html` | 自包含 HTML 报告 |
| `with_skill/eval-<id>/run-1/grading.json` | expectations + assertions 的 pass/fail |
| `with_skill/eval-<id>/run-1/compliance.json` | SKILL.md 指令遵循性检查 |
| `with_skill/eval-<id>/run-1/calibration.json` | 评分可信度校准 |
| `with_skill/eval-<id>/run-1/run_debug.json` | 完整回复 + tool_calls + timing |
| `with_skill/eval-<id>/run-1/timing.json` | 执行耗时和 token 统计 |

---

## 项目结构

```
skill-eval/
├── pyproject.toml
├── agents/                        # LLM prompt 模板
├── src/skill_eval/
│   ├── cli/                       # typer CLI 入口（7 个子命令）
│   ├── models/                    # 30+ Pydantic 数据模型
│   ├── parser.py                  # SKILL.md 解析器
│   ├── config/                    # 配置驱动架构
│   ├── security/                  # 安全审查（4 扫描器）
│   ├── static_analysis/           # 静态分析（11 项检查）
│   ├── evals/                     # evals.json 加载 + 生成
│   ├── execution/                 # Claude 执行 + A/B 测试
│   ├── scoring/                   # 评分引擎（程序化 + LLM + 盲比较）
│   ├── compliance/                # 遵循性引擎
│   ├── calibration/               # 评分校准
│   ├── reporting/                 # HTML 报告
│   └── batch/                     # 批量评测（DAG + 调度）
├── tests/
└── docs/
```

---

## 与 Skill-Creator 的关系

Skill-Eval 从 skill-creator 移植并改造了以下核心代码：

| 源文件 | 移植到 | 改造内容 |
|--------|--------|----------|
| `scripts/utils.py` | `parser.py` | 扩展为 5-tuple 返回值 |
| `scripts/aggregate_benchmark.py` | `execution/benchmark.py` | 迁移到 Pydantic 模型 |
| `eval-viewer/generate_review.py` | `reporting/viewer.py` | 整合为 ReportGenerator |
| `eval-viewer/viewer.html` | `reporting/templates/viewer.html` | 直接复制 |
| `agents/grader.md` | `agents/grader.md` | 直接复制 |
| `agents/comparator.md` | `scoring/comparator.py` | 实现为 BlindComparator |
| `agents/analyzer.md` | `scoring/analyzer.py` | 实现为 PosthocAnalyzer |
| `references/schemas.md` | `models/` | 转为 Pydantic 数据模型 |
| `scripts/quick_validate.py` | `static_analysis/checks.py` | 融入 11 项检查 |

新增模块（无对应源码）：

- `security/` — 安全审查引擎
- `compliance/` — 遵循性引擎
- `calibration/` — 评分校准
- `batch/` — 批量评测（DAG）
- `config/` — 配置系统

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

不需要。内置 2 个原生扫描器，开箱即用。

### `--security-gate` 和 `--compliance-gate` 有什么区别？

| Flag | 检查对象 | 失败条件 | 触发时机 |
|------|----------|----------|----------|
| `--security-gate` | Skill 源代码安全性 | CRITICAL/HIGH/MEDIUM 风险 | 评测执行前 |
| `--compliance-gate` | 执行过程是否遵循 SKILL.md | compliance_rate < 0.5 | 评分阶段 |

### 静态分析需要 Claude CLI 吗？

不需要。`analyze` 零 API 成本，<1s 完成。

### evals.json 放在哪里？

推荐 `<skill_path>/evals/evals.json`。`run` 会自动查找，也可用 `--evals` 手动指定。

### 如何在 CI 中使用？

```bash
skill-eval vet path/to/skill/ --format json
skill-eval analyze path/to/skill/ --format json -o scorecard.json
skill-eval run path/to/skill/ --format json --runs 3 --compliance-gate
# exit code: 0=通过, 1=失败, 2=输入错误, 3=执行错误
```
