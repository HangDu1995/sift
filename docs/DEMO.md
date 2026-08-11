# Skill-Eval 演示指南

> 15 分钟演示 skill-eval 的完整能力——从安全审查到 A/B 评测，从正面案例到反面教材。

## 前置条件

```bash
pip install -e .          # 安装 skill-eval
claude --version           # 需要 Claude Code CLI >= 2.1.0, < 2.2.0
pytest tests/ -q           # 测试验证
```

**演示用 Skill**：

| 类型 | 目录 | 用途 |
|------|------|------|
| ✅ 正面 | `fixtures/test-skills/data-pipeline/skills/data-validator/` | 展示 Skill 如何**提升** Claude 表现 |
| ❌ 反面×4 | `fixtures/negative-skills/` | 展示框架如何**检测**各种 Skill 缺陷 |

---

## 第一幕：正面案例（data-validator）

data-validator 是一个 JSON 数据校验 Skill。它告诉 Claude：校验 id（正整数）、name（非空）、type（user/order/product），并检测 SQL 注入。

### 1.1 安全审查（< 1 秒，无需 Claude）

```bash
skill-eval vet fixtures/test-skills/data-pipeline/skills/data-validator/
```

**产出解读**：

```
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Scanner         ┃ Status ┃ Detail                                            ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ aguara          │ SKIP   │ aguara not installed                              │
│ skill-scanner   │ SKIP   │ skill-scanner not installed                       │
│ secrets-scan    │ PASS   │ No hardcoded secrets found                        │
│ structure-check │ PASS   │ Skill structure valid                             │
└─────────────────┴────────┴───────────────────────────────────────────────────┘
```

VERDICT 三种结果：**SAFE**（exit 0）、**REVIEW_NEEDED**（需人工审查）、**BLOCKED**（exit 1）。

### 1.2 静态分析（< 1 秒，无需 Claude）

```bash
skill-eval analyze fixtures/test-skills/data-pipeline/skills/data-validator/
```

**产出解读**（11/11 满分）：

```
Score: 11/11  Category: CAPABILITY_UPLIFT
```

两种分类：**CAPABILITY_UPLIFT**（增强型，教 Claude 新能力）和 **ENCODED_PREFERENCE**（偏好型，编码团队约定）。

### 1.3 完整 A/B 评测（需要 Claude，2-3 分钟）

```bash
skill-eval run fixtures/test-skills/data-pipeline/skills/data-validator/ --runs 1 --output output/
```

对 with_skill 和 without_skill 分别运行相同的评测，量化 Skill 的**因果贡献**。每个 eval 独立并发执行。

**实际结果**（data-validator）：

```
Results:
  with_skill:    80% pass rate
  without_skill: 33% pass rate
  delta:         +0.47
```

---

## 指标解读手册

### 核心指标：Pass Rate 与 Delta

| 指标 | 来源 | 公式 | 含义 |
|------|------|------|------|
| **with_skill pass_rate** | `grading.json` → `summary.pass_rate` | passed / total | 有 Skill 时通过率 |
| **without_skill pass_rate** | `grading.json` → `summary.pass_rate` | passed / total | 无 Skill 时通过率（基线） |
| **delta** | `benchmark.json` → `run_summary.delta` | with − without | **Skill 的因果贡献** |

**Delta 怎么读**：

| Delta | 含义 | 行动 |
|-------|------|------|
| **> +0.3** | Skill 显著提升 Claude 表现 | 发布 |
| **+0.1 ~ +0.3** | Skill 有一定帮助 | 考虑优化 |
| **-0.1 ~ +0.1** | Skill 几乎无效 | 审视设计 |
| **< -0.1** | Skill **反而干扰** Claude | 排查指令 → 重写 |

### 输出文件结构

```
output/
├── benchmark.json          # 聚合结果（核心文件）
│   ├── runs[]              #   每条 run 的 pass_rate / time / tokens
│   ├── run_summary         #   mean / stddev / delta
│   ├── comparisons[]       #   盲比较
│   ├── analysis            #   深度分析
│   └── selection           #   子集跑信息（仅 --changed-from 时：mode/ids/affected/total）
├── report.html             #   自包含 HTML 可视化报告
│
├── with_skill/
│   └── eval-<id>/run-<n>/
│       ├── grading.json    #   每条 expectation + assertion 的 pass/fail + evidence
│       ├── compliance.json #   SKILL.md 指令遵循性
│       ├── calibration.json#   结果×过程 coherence 判决
│       └── run_debug.json  #   完整 transcript + tool_calls
│
└── without_skill/
    └── eval-<id>/run-<n>/  #   同上（无 compliance.json）
```

### 单文件详解

#### grading.json

```json
{
  "expectations": [
    {
      "text": "id 必须是正整数",
      "passed": true,
      "evidence": "Transcript: 'id must be a positive integer'",
      "source": "llm_batch"
    }
  ],
  "summary": {
    "passed": 12,
    "failed": 3,
    "total": 15,
    "pass_rate": 0.8
  }
}
```

- **expectations**（`source: "llm_batch"`）：语义期望，LLM 从 transcript 中判断
- **assertions**（`source: "programmatic"`）：程序化断言，检查文件是否存在/内容匹配

#### compliance.json —— 指令遵循性

框架从 SKILL.md 中提取 6 类指令：

| 指令类型 | 来源 | 示例 |
|----------|------|------|
| `HEADING_STEP` | `##` 标题下的步骤 | "Step 1: Read Input Data" |
| `TOOL_REQUIREMENT` | 工具使用要求 | "使用 Read 工具读取文件" |
| `CONSTRAINT` | 约束条件 | "NEVER fabricate data" |
| `ANTI_FABRICATION` | 反捏造规则 | "NEVER invent validation errors" |
| `OUTPUT_FORMAT` | 输出格式要求 | "Write to validation-report.json" |
| `REFERENCE_USAGE` | 参考文档引用 | "See references/schema-rules.md" |

每条结果是一个 `AuditFinding`，带两个正交维度——**verdict**（五态：`PASS` / `FAIL` / `N/A` 不适用 / `UNRESOLVED` 没判成 / `INDETERMINATE` 判不准）+ **method**（`PROGRAMMATIC` 程序对账 / `LLM` 语义判定）。汇总 `summary{total,pass,fail,na,unresolved,indeterminate}`，遵循度 = `pass/(pass+fail)`（排除 N/A + UNRESOLVED + INDETERMINATE）。

#### calibration.json —— 结果×过程 coherence 判决

单次执行的两轴判决：结果（优先押客观 `pass_rate_programmatic≥0.8`，无客观锚回退主观）× 过程（遵循性有无 prohibition/output 硬类违规），组合出 5 态 verdict：

| verdict | 含义 |
|---------|------|
| **可信** TRUSTED | 结果对 + 过程对（未发现过程问题） |
| **高分存疑** HIGH_SCORE_SUSPECT | 结果对 + 过程违反硬性规则（列 evidence） |
| **可修复** FIXABLE | 结果错 + 过程对 |
| **技能失效** SKILL_FAILED | 结果错 + 过程违反硬性规则 |
| **过程未知** PROCESS_UNKNOWN | compliance 缺失/无可判定 |

### 质量公式

```
Score = StaticAnalysis(0-11)
      + Delta × 100
      + ComplianceRate(%)
      - PerformancePenalty
```

---

## 第二幕：反面教材（negative-skills）

### 2.1 坏指令型：bad-instruction

**缺陷**：指令自相矛盾（"用 Python 但不要用代码"、"用 pandas 但不要用 pandas"）。

**静态分析**：11/11——静态检查**不检测语义矛盾**。

**A/B 评测（预期）**：delta 为负（矛盾指令干扰 Claude）。

```bash
skill-eval run fixtures/negative-skills/bad-instruction/ --runs 1
```

**教训**：静态分析 11/11 ≠ Skill 没问题。compliance.json 会显示指令遵循率低。

### 2.2 安全风险型：security-risk

```bash
skill-eval vet fixtures/negative-skills/security-risk/
```

```
VERDICT: BLOCKED — 1 HIGH finding（硬编码 API key）
```

### 2.3 静态分析失败型：low-quality

**SKILL.md**：描述只有 2 字 "干活"，无示例，无反捏造。

```
Score: 6/11
FAIL: description_length, description_has_trigger,
      has_examples, anti_fabrication, source_documented
```

### 2.4 空壳型：empty-shell

只有 `---\nname: empty-shell\n---`，没有内容、没有 evals。

---

## 附录 A：完整指标速查表

| 指标 | 文件 | 公式 | 好 | 警告 | 坏 |
|------|------|------|-----|------|-----|
| **delta** | benchmark.json | with − without | > +0.3 | -0.1~+0.1 | < -0.1 |
| **pass_rate** | grading.json | passed / total | > 0.8 | 0.5~0.8 | < 0.5 |
| **compliance_rate** | compliance.json | pass / (pass+fail) | > 0.7 | 0.5~0.7 | < 0.5 |
| **calibration verdict** | calibration.json | 结果×过程 coherence | 可信/高分存疑 | 可修复 | 技能失效/过程未知 |
| **static score** | analyze | 0-11 checks | 8+ | 5-7 | < 5 |

## 附录 B：从零开始的 5 步流程

```bash
# 1. 初始化 evals
skill-eval init my-skill/

# 2. 编辑 evals/evals.json，补充 L2 用例 + assertions + fixtures

# 3. 静态检查
skill-eval analyze my-skill/
skill-eval vet my-skill/

# 4. 快速验证（1 run）
skill-eval run my-skill/ --runs 1 --eval-samples 5
skill-eval run my-skill/ --runs 1 --eval-ids 3,5   # 或只跑指定 id（调试/回归某几道）
skill-eval run my-skill/ --runs 1 --changed-from v1/   # 或改 skill 后只跑受影响的题（给基线目录；回归探针）

# 5. 正式评测（3 runs，统计稳定）
skill-eval run my-skill/ --runs 3 --compliance-gate

# 6. 生成报告
skill-eval report output/ --open
```
