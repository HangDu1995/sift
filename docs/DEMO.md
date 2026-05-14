# Skill-Eval 演示指南

> 30 分钟演示 skill-eval 的完整能力——从静态检查到盲比较，从正面案例到反面教材。

## 前置条件

```bash
pip install -e .          # 安装 skill-eval
claude --version           # 需要 Claude Code CLI >= 2.1.0, < 2.2.0
pytest tests/ -q           # 242 个测试全部通过
```

**演示用 Skill**：

| 类型 | 目录 | 用途 |
|------|------|------|
| ✅ 正面 | `fixtures/test-skills/data-validator/` | 展示 Skill 如何**提升** Claude 表现 |
| ❌ 反面×6 | `fixtures/negative-skills/` | 展示框架如何**检测**各种 Skill 缺陷 |

---

## 第一幕：正面案例（data-validator）

data-validator 是一个 JSON 数据校验 Skill。它告诉 Claude：校验 id（正整数）、name（非空）、type（user/order/product），并检测 SQL 注入。

### 1.1 安全审查（< 1 秒，无需 Claude）

```bash
skill-eval vet fixtures/test-skills/data-validator/
```

**产出解读**：

```
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ secrets-scan    ┃ PASS   ┃ No hardcoded secrets found    ┃
┃ structure-check ┃ PASS   ┃ Skill structure valid         ┃
┣━━━━━━━━━━━━━━━━━╋━━━━━━━━╋━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ VERDICT: SAFE   ┃ 所有扫描器通过                          ┃
┗━━━━━━━━━━━━━━━━━┻━━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

| 扫描器 | 类型 | 检查内容 |
|--------|------|----------|
| `secrets-scan` | 内置 | 硬编码密钥（API key / password / token） |
| `structure-check` | 内置 | SKILL.md 结构完整性（frontmatter、name 字段） |
| `aguara` | 可选外部 | Prompt 注入检测（Go） |
| `skill-scanner` | 可选外部 | 已知恶意模式检测（pip） |

VERDICT 三种结果：
- **SAFE** → exit 0，通过
- **REVIEW_NEEDED** → 存在 MEDIUM 风险，需要人工审查
- **BLOCKED** → 存在 HIGH/CRITICAL 风险，exit 1

### 1.2 静态分析（< 1 秒，无需 Claude）

```bash
skill-eval analyze fixtures/test-skills/data-validator/
```

**产出解读**（11/11 满分）：

```
Score: 11/11  Category: CAPABILITY_UPLIFT
```

| # | 检查项 | 检查内容 | 结果 |
|---|--------|----------|------|
| 1 | `description_length` | 描述 ≥ 20 字符 | ✅ 134 chars |
| 2 | `description_has_trigger` | 含动作触发词 | ✅ |
| 3 | `description_third_person` | 使用第三人称 | ✅ |
| 4 | `name_kebab_case` | name 符合 kebab-case | ✅ |
| 5 | `name_max_64` | name ≤ 64 字符 | ✅ 14 chars |
| 6 | `name_no_reserved` | name 非保留字 | ✅ |
| 7 | `skill_md_max_500_lines` | ≤ 500 行 | ✅ 67 lines |
| 8 | `has_examples` | 含示例代码 | ✅ |
| 9 | `reference_depth` | references/ 扁平 | ✅ |
| 10 | `anti_fabrication` | 含反捏造指引 | ✅ |
| 11 | `source_documented` | 含参考来源 | ✅ |

**两种分类**：
- **CAPABILITY_UPLIFT**（增强型）：教 Claude 它原本不会的新能力
- **ENCODED_PREFERENCE**（偏好型）：编码团队偏好，Claude 本身就会但可能有不同做法

### 1.3 覆盖率扫描（< 1 秒）

```bash
skill-eval scan fixtures/test-skills/
```

```
Total: 3  With evals: 3  Missing: 0
```

扫描整个 skills 目录，标记哪些 Skill 有/缺少 evals。**没有 evals 就无法量化评估**。

### 1.4 完整 A/B 评测（需要 Claude，2-3 分钟）

```bash
skill-eval full fixtures/test-skills/data-validator/ --runs 1
# 注：该 Skill 只有 3 个 eval，无需 --eval-samples。
# 当 eval 数量较多（>10）时，可加 --eval-samples 10 限制每个 eval 的采样轮数以节省时间。
```

这一步是核心——对 with_skill（注入 SKILL.md）和 without_skill（不注入）分别运行相同的评测，量化 Skill 的**因果贡献**。

> **默认运行模式**：per-eval mode（每个 eval 独立并发运行），各 eval 互不阻塞。使用 `--merged` 可切换为合并模式（所有 eval 合并为一个 prompt）。

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

> data-validator 的 delta = **+0.47**，意味着注入 SKILL.md 将 Claude 的通过率从 33% 提升到 80%——这是一个高价值的 Skill。

### 输出文件结构

```
output/iteration-1/
├── benchmark.json          # 聚合结果（核心文件）
│   ├── runs[]              #   每条 run 的 pass_rate / time / tokens
│   ├── run_summary         #   mean / stddev / delta
│   ├── comparisons[]       #   🆕 盲比较（BlindComparator）
│   └── analysis            #   🆕 深度分析（PosthocAnalyzer）
├── report.html             #   自包含 HTML 可视化报告
├── comparison-run1.json    #   🆕 盲比较详细结果
├── analysis-notes.json     #   🆕 LLM 分析洞察
│
├── with_skill/run-1/
│   ├── grading.json        #   每条 expectation + assertion 的 pass/fail + evidence
│   ├── checkpoints.json    #   每个 eval case 独立通过率
│   ├── compliance.json     #   SKILL.md 指令遵循性
│   ├── calibration.json    #   评分可信度校准
│   └── run_debug.json      #   完整 transcript + tool_calls
│
└── without_skill/run-1/    #   同上（无 compliance）
```

### 单文件详解

#### grading.json —— 最基础的结果

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

**expectations vs assertions**：
- **expectations**（`source: "llm_batch"`）：语义期望，LLM 从 transcript 中判断是否满足
- **assertions**（`source: "programmatic"`）：程序化断言，检查文件是否存在/JSON 字段是否正确

> with_skill 没有 `compliance.json` 文件——只有使用了 SKILL.md 的配置才会产生遵循性检查。

#### compliance.json —— 指令遵循性

只有 with_skill 配置产生。框架从 SKILL.md 中提取 6 类指令：

| 指令类型 | 来源 | 示例 |
|----------|------|------|
| `HEADING_STEP` | `##` 标题下的步骤 | "Step 1: Read Input Data" |
| `TOOL_REQUIREMENT` | 工具使用要求 | "使用 Read 工具读取文件" |
| `CONSTRAINT` | 约束条件 | "NEVER fabricate data" |
| `ANTI_FABRICATION` | 反捏造规则 | "NEVER invent validation errors" |
| `OUTPUT_FORMAT` | 输出格式要求 | "Write to validation-report.json" |
| `REFERENCE_USAGE` | 参考文档引用 | "See references/schema-rules.md" |

每条检查结果为 `followed: true/false`，汇总为 `compliance_rate`（0.0-1.0）。

**排错路径**：

```
delta 为负 / 低
  ↓
看 compliance_rate
  ├── compliance_rate < 0.5  → 指令有问题：SKILL.md 太难遵循或有矛盾
  │                             → 重写指令，消除矛盾
  └── compliance_rate ≥ 0.7  → 指令没问题但效果差
                                 → 看 calibration.json
```

#### calibration.json —— 评分可信度

当 `hybrid` 模式同时使用程序化断言和 LLM 评分时，两者可能出现分歧。校准引擎检查 7 种信号：

| 等级 | Agreement Rate | 含义 | 对 delta 的影响 |
|------|----------------|------|-----------------|
| **HIGH** | ≥ 0.8 | 程序化和 LLM 评分高度一致 | 可信，权重 1.0 |
| **MEDIUM** | ≥ 0.6 | 存在少量分歧 | 基本可信，权重 0.8 |
| **LOW** | ≥ 0.4 | 分歧较多 | 需人工复核，权重 0.5 |
| **UNRELIABLE** | < 0.4 | 严重分歧 | **不可信**，exit 1 |

> 当 grade 为 UNRELIABLE 时，说明 expectations 写得不清晰——LLM 和程序化检查给了相反的结论。此时应该**重写 expectations** 而非相信任一结果。

#### comparison-run1.json —— 🆕 盲比较

BlindComparator 在不知道哪个输出来自哪个配置的情况下，从两个维度评分：

| 维度 | 检查项 | 1-5 分 |
|------|--------|--------|
| **Content**（内容） | correctness · completeness · accuracy | 每个 1-5 |
| **Structure**（结构） | organization · formatting · usability | 每个 1-5 |
| **Overall** | (content + structure) / 2，缩放至 1-10 | 综合分 |

**data-validator 实际结果**：

```
Output A (with_skill):  content 4.7 / structure 4.0 → overall 8.7
Output B (without_skill): content 3.7 / structure 3.7 → overall 8.3
Winner: A (with_skill 仅以 0.4 分优势胜出)
```

盲比较告诉你：**Skill 主要提升了哪方面**（内容？结构？），这比只看 delta 数字更有指导意义。

#### analysis-notes.json —— 🆕 LLM 分析洞察

PosthocAnalyzer 在 benchmark 聚合后自动运行，生成聚合指标无法揭示的模式发现。例如：

```
• Eval 2 (L2-有效数据验证) 是 with_skill 唯一失败的 eval（50%）——3 个 expectations
  全部失败但 3 个 assertions 全部通过。这提示 expectations 的措辞可能与 Claude
  的理解有偏差
• 有效记录的误判（valid_count: 0, invalid_count: 2）同时出现在 with_skill 和
  without_skill 中——这不是 Skill 的问题，是 eval prompt 或其 fixture 的设计问题
• with_skill 的 time_seconds 比 without_skill 少 17.7 秒——Skill 帮助 Claude
  更快找到正确的处理方式
```

**分析笔记怎么用**：看完 delta 后读一遍，它能帮你发现指标背后**看不见的**模式——比如"这个 eval 到底是不是写错了"。

### 质量公式

skill-eval 综合所有维度的最终评分：

```
Score = StaticAnalysis(0-11)
      + Delta × 100
      + ComplianceRate(%)
      + TriggerAccuracy(%)
      - PerformancePenalty
      × CalibrationWeight
```

每个维度都能独立排查，形成完整的 Skill 质量证据链。

---

## 第二幕：反面教材（negative-skills）

`fixtures/negative-skills/` 下专门构造了 6 种常见缺陷，演示框架如何检测它们。

### 2.1 坏指令型：bad-instruction

**只看 SKILL.md 的前几句**：

```markdown
## 转换数据
用 Python 3 处理数据文件。**不要使用任何脚本或代码**。
你应该手动逐行分析 JSON 然后重写输出。如果遇到 CSV 使用 csv 库，
但如果遇到 JSON 使用 jq 命令。如果数据是 XML 就用 pandas（不要用 pandas）。
最终输出到 output.json 或 result.json，具体取决于你在哪个系统上运行。
```

**缺陷**：指令自相矛盾（"用 Python 但不要用代码"、"用 pandas 但不要用 pandas"）、输出文件名不确定、验证"随意"。

**静态分析**：11/11——静态检查**不检测语义矛盾**。这说明静态分析是必要的但不够——还需要动态评测。

**A/B 评测（预期）**：

| 配置 | 预期 | 原因 |
|------|------|------|
| with_skill | **低** | 矛盾指令让 Claude 困惑，更可能出错 |
| without_skill | **中** | Claude 自行判断，没有矛盾干扰 |
| delta | **负** | 坏 Skill 不如没有 Skill |

```bash
skill-eval full fixtures/negative-skills/bad-instruction/ --runs 1
# 可选：--eval-samples <N> 限制每个 eval 的采样轮数（eval 数量多时有用）
```

**教训**：
- 静态分析 11/11 ≠ Skill 没问题
- 矛盾指令会**主动降低** Claude 表现
- compliance.json 会显示指令遵循率低（因为指令互相矛盾，无法同时满足）

### 2.2 安全风险型：security-risk

```bash
skill-eval vet fixtures/negative-skills/security-risk/
```

```
Verdict: BLOCKED
Findings: 1 (HIGH)
  HIGH: Hardcoded credentials detected  (secrets-scan, line 6)
```

**缺陷**：SKILL.md 中包含 `API_KEY="sk-1234abcd..."` 硬编码密钥，以及 `curl | bash` 不安全操作。

**教训**：
- `vet` 命令自动扫描硬编码密钥（匹配 pattern：`api_key\|secret\|token\s*[:=]\s*["'][A-Za-z0-9_-]{16,}["']`）
- 外部扫描器（aguara, skill-scanner）能检测更多类型
- 在生产发布前必须跑 `vet` 作为 CI gate

### 2.3 静态分析失败型：low-quality

**SKILL.md 全文**：

```markdown
---
name: low-quality
description: 干活
---
# 干活
做数据处理。
```

**静态分析结果**：

```
Score: 6/11

PASS: description_third_person, name_kebab_case, name_max_64,
      name_no_reserved, skill_md_max_500_lines, reference_depth
FAIL: description_length (2 chars, min 20)
      description_has_trigger (no action words)
      has_examples (no examples)
      anti_fabrication (missing guidance)
      source_documented (no sources)
```

```bash
skill-eval analyze fixtures/negative-skills/low-quality/ --format json
```

**教训**：
- 描述太短（2 字 "干活"）→ 触发不准
- 缺示例 → Claude 自己猜，结果不可控
- 无反捏造 → 可能伪造数据
- 无参考来源 → 不可审计

### 2.4 空壳型：empty-shell

**SKILL.md 全文**：

```markdown
---
name: empty-shell
---
```

没有内容、没有 evals。

```bash
skill-eval scan fixtures/negative-skills/
```

```
empty-shell: ✗ MISSING (no evals)
```

**教训**：只有名字没有内容和 evals 的 Skill 是无效的。`scan` 命令自动发现并标记。

### 2.5 违规格式型：bad-format

**缺陷**：SKILL.md **没有 YAML frontmatter**（少了 `---` 开头），且超过 500 行（故意填充了大量 FAQ）。

```bash
skill-eval analyze fixtures/negative-skills/bad-format/
```

```
Error: SKILL.md missing frontmatter (no opening ---)
```

**教训**：
- 必须用 `---\n...\n---` 包裹 frontmatter
- name 必须是 kebab-case
- SKILL.md ≤ 500 行（太长 Claude 顾不全）

### 2.6 依赖错误型：broken-root + cascade-victim + missing-dep

三个 Skill 组成一个**故意有问题的 DAG**：

```json
{
  "skills": [
    {"path": "broken-root",    "depends_on": []},
    {"path": "cascade-victim", "depends_on": ["broken-root"]},
    {"path": "missing-dep",    "depends_on": ["nonexistent-skill"]}
  ]
}
```

**缺陷分析**：
- **broken-root**：指令要求 "NEVER 使用任何工具"——故意让它无法完成任何实际工作
- **cascade-victim**：本身没问题，但依赖 broken-root
- **missing-dep**：依赖一个不存在的 Skill

```bash
skill-eval batch fixtures/negative-skills/manifest-broken-deps.json --runs 1
```

**预期输出**：

```
  ✗ broken-root       FAILED
  ○ cascade-victim    CASCADE_FAILED    ← 不是自己的错
  ✗ missing-dep       FAILED

  Attributions:
    broken-root:     ROOT_CAUSE           ← "是你的问题"
    cascade-victim:  CASCADE              ← "不是你的事"
    missing-dep:     DEPENDENCY_MISSING   ← "没找到依赖"
```

**归因类型**：

| 类型 | 含义 | 排查方向 |
|------|------|----------|
| `ROOT_CAUSE` | 该 Skill 自身导致失败 | 看它的 compliance / grading |
| `CASCADE` | 上游失败引发的连锁 | 看上游（ROOT_CAUSE 是哪个？） |
| `DEPENDENCY_MISSING` | 依赖的 Skill 不存在 | 检查 manifest.json 和文件路径 |
| `INDEPENDENT` | 与其他 Skill 无关的独立失败 | 看自身 grading |

**教训**：
- DAG 归因帮你精准定位"谁该背锅"——下游失败不一定是下游的问题
- `CASCADE` 状态说明 cascade-victim 本身没问题，不应该被惩罚
- `DEPENDENCY_MISSING` 帮你发现 manifest 配置错误

### 2.7 过拟合型：overfitted-skill

**什么是过拟合**：Skill 的指令被"逆向适配"以通过特定 eval 用例，但无法泛化到未见过的场景。就像学生背了考试答案但不会举一反三。

**SKILL.md 关键内容**：

```markdown
## Instructions
1. When processing data, always output a JSON object with the following metadata fields:
   id, name, status, and timestamp.
2. NEVER modify the original data. Always create a new output file.
3. For validation, ALWAYS check that id is a positive integer and name is non-empty.
4. For type field, only allow values: user, order, product.
5. When writing output files, always use output/ directory.
6. For any SQL-related checks, scan for: DROP, DELETE, INSERT, UNION, --.
7. Step 1: Read the input file. Step 2: Validate each field. Step 3: Write results.
8. Always sort output alphabetically by the name field.
9. NEVER fabricate validation errors—only flag what you actually find.
10. If the input is empty, return an error object with code: 400.
11. When in doubt, consult the references/schema.json file.
12. Always escape special characters in string fields.
13. Record all validation results in a results.json file.
14. For metadata, include created_at, updated_at, and author fields.
15. Process records in batches of 100 for large files.
```

注意：第 14 行的 metadata 字段要求与 eval-1 的 expectation 完全一致——
这是"按题写指令"的典型特征。

**拆分评测集**：

```bash
skill-eval split-evals fixtures/negative-skills/overfitted-skill/evals/evals-train.json
```

`split-evals` 将评测集随机拆分为 train（80%）和 holdout（20%），其中 holdout 对于
评测者不可见——它才是真正检验泛化能力的"盲测题"。

**过拟合检测**：

```bash
skill-eval vet-overfit \
  --train-bench output/train/iteration-1/benchmark.json \
  --holdout-bench output/holdout/iteration-1/benchmark.json \
  --skill fixtures/negative-skills/overfitted-skill/ \
  --train-evals fixtures/negative-skills/overfitted-skill/evals/evals-train.json
```

**检测结果（实际运行）**：

```
🔍 Overfitting Detection Report
────────────────────────────────────────────
  overfitting_score:    0.78 (HIGH)
  generalization_gap:   -0.52
  suspicious_instructions: 10 (7 HIGH + 2 MEDIUM + 1 LOW)
  train_pass_rate:      0.85
  holdout_pass_rate:    0.33

🧩 Detected Patterns (Top 3):
  • Instruction line 14 directly mirrors eval-1 expectation about
    metadata fields (created_at, updated_at, author)
  • Instruction line 4 matches eval-3 type field validation exactly
    (user, order, product)
  • Instruction line 3 parrots eval-2 id validation wording verbatim
    ("positive integer" and "non-empty")
```

train 上有 85% 通过率，但 holdout 只有 33%——泛化差距高达 0.52。
7 条 HIGH 级别可疑指令表明 SKILL.md 中超过一半的指令都是"背答案"式的。

**指标解读**：

| 指标 | 含义 | 判断标准 |
|------|------|----------|
| **overfitting_score** (0-1) | Skill 对训练集过拟合的总体程度 | > 0.6 高度过拟合，< 0.3 良好泛化 |
| **generalization_gap** | train 与 holdout pass_rate 之差 | < -0.3 严重退化，≈ 0 泛化良好 |
| **suspicious_instructions** | 被判定为与 eval 高度耦合的指令数 | HIGH 越多越危险 |
| **anomaly_detail** | 逐条指令的相似度 + 证据 | 看 detail 定位具体是哪些 eval 被"背"了 |

`generalization_gap` 是核心指标——train 高出 holdout 越多，说明 Skill 越"只会做见过的题"。
如果 train 和 holdout 的 pass_rate 都低（比如都 < 0.4），那只是 Skill 质量差，不一定是过拟合。

#### 防过拟合工作流

最佳的 Skill 开发流程应引入过拟合检测：

```
Step 1: split  → skill-eval split-evals evals/evals-train.json
                 ↓ 产出 evals-train.json（80%）+ evals-holdout.json（20%）

Step 2: run    → skill-eval full my-skill/ \
                 --evals evals-train.json --output output/train/
                 ↓
                 skill-eval full my-skill/ \
                 --evals evals-holdout.json --output output/holdout/

Step 3: vet    → skill-eval vet-overfit \
                 --train-bench output/train/iteration-1/benchmark.json \
                 --holdout-bench output/holdout/iteration-1/benchmark.json \
                 --skill my-skill/ \
                 --train-evals evals-train.json
                 ↓ 产出 overfitting_score + generalization_gap + 可疑指令清单
```

| 阶段 | 目的 | 产出 |
|------|------|------|
| **split** | 将评测集划分为可见（train）和不可见（holdout） | 两个评测文件 |
| **run** | 分别在 train 和 holdout 上跑完整 A/B 评测 | 两组 benchmark.json |
| **vet** | 对比两组结果，检测过拟合信号 | 过拟合报告 |

**核心理念**：holdout 评分才是真实水平的无偏估计。不要让 Skill 的作者看到 holdout
的内容——否则过拟合检测失去意义。

**教训**：
- 在 train 上表现好 ≠ Skill 真的好——可能是"背答案"
- 7+ 条 HIGH 级别可疑指令强烈建议重写 SKILL.md，去除与 eval 的机械对应
- `generalization_gap` < -0.3 时，建议用更大的 holdout 集重新验证
- 理想状态：train 和 holdout 的 pass_rate 都在高位且差距 < 0.1

---

## 第三幕：批量评测（正面 DAG）

回到正常的三个 Skill：

```bash
skill-eval batch fixtures/test-skills/manifest.json --runs 1 --layers unit
```

这会按 DAG 顺序依次评测 data-validator → data-transform → report-generator。

```
Skill: [1/3] data-validator
  Eval: [1] L1-验证规则理解  with_skill  PASS
  ...

  ✓ data-validator        PASSED
  ✓ data-transform        PASSED
  ✓ report-generator      PASSED

Passed: 3, Failed: 0, Skipped: 0
```

---

## 附录 A：完整指标速查表

| 指标 | 文件 | 公式 | 好 | 警告 | 坏 | 排错路径 |
|------|------|------|-----|------|-----|----------|
| **delta** | benchmark.json | with − without | > +0.3 | -0.1~+0.1 | < -0.1 | → compliance → calibration |
| **pass_rate** | grading.json | passed / total | > 0.8 | 0.5~0.8 | < 0.5 | → checkpoints 定位失败 eval |
| **compliance_rate** | compliance.json | followed / total | > 0.7 | 0.5~0.7 | < 0.5 | → 逐条看 failed 指令 |
| **calibration grade** | calibration.json | agreement + signals | HIGH | MEDIUM/LOW | UNRELIABLE | → 重写 expectations |
| **static score** | analyze | 0-11 checks | 8+ | 5-7 | < 5 | → 逐条 FAIL |
| **comparison winner** | comparison-run*.json | rubric content+structure | — | — | — | → 看哪个维度领先 |
| **analysis notes** | analysis-notes.json | LLM 分析 | — | — | — | → 读一遍发现隐藏模式 |
| **vet verdict** | vet | 4 scanners | SAFE | REVIEW_NEEDED | BLOCKED | → 看 findings |
| **scan status** | scan | evals存在? | ✓ | — | ✗ | → `skill-eval init` |

## 附录 B：常见排错决策树

```
delta < -0.1（Skill 反而有害）
│
├─→ 看 compliance_rate
│   ├── < 0.5  → SKILL.md 指令有矛盾或太难遵循
│   │            → 重写矛盾指令，分解复杂步骤
│   └── ≥ 0.7  → 指令看似被遵循但结果差
│                → 看 calibration grade
│                    ├── UNRELIABLE / LOW → expectations 不可信
│                    │                      → 重写 expectations
│                    └── HIGH / MEDIUM → 指令方向错误
│                                         → 重新设计 SKILL.md 指令

delta ≈ 0（Skill 无效果）
│
├─→ 看 checkpoint breakdown
│   └── 是否每个 eval 的 pass_rate 都一样？
│       └── 是 → expectations 太简单，没区分度
│                → 增加 L2 有 assertions 的 eval case

pass_rate 高但 calibration UNRELIABLE
│
└─→ 看 disagreements
    └── 程序化检查说 FAIL，LLM 说 PASS？
        └── expectations 写得太模糊
            → 重写为可验证的具体条件
```

## 附录 C：从零开始的 6 步流程

```bash
# 1. 初始化 evals（可选：--num-evals 5 指定生成数量）
skill-eval init my-skill/ --no-generate-prompts

# 2. 编辑 evals/evals.json，补充 L2 用例 + assertions + fixtures

# 3. 静态检查
skill-eval analyze my-skill/
skill-eval vet my-skill/

# 4. 快速验证（1 run，快速反馈）
#    可选：--eval-samples 10 限制每个 eval 的采样轮数
skill-eval full my-skill/ --runs 1

# 5. 正式评测（3 runs，统计稳定）
skill-eval full my-skill/ --runs 3

# 6. 批量回归
skill-eval batch manifest.json --runs 3 --baseline baseline.json
```

> **运行模式**：默认 per-eval mode，每个 eval 独立并发运行。加 `--merged` 切换为合并模式。
