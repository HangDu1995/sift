# Skill-Eval：评测方法论与代码溯源

> 本文档阐述 skill-eval 评测体系的来源——哪些是从现有系统移植的，哪些是新建的，以及背后的理论依据。

---

## 1. 来自 skill-creator 的移植

skill-creator（位于 `/Users/layne/codes/skills/skills/skill-creator`）是 Claude Code 的 Skill 开发工具链，包含一套脚本用于创建、评测和优化 Skill。skill-eval 从中移植了以下核心代码，将其从松散脚本改造为结构化 CLI 工具：

| skill-creator 原始文件 | skill-eval 目标 | 改造内容 |
|------------------------|----------------|----------|
| `scripts/utils.py` → `parse_skill_md()` | `parser.py` | 扩展为 5-tuple 返回值 |
| `eval-viewer/generate_review.py` | `reporting/viewer.py` → `ReportGenerator` | 移植 `find_runs()`、`embed_file()`、HTTP server |
| `eval-viewer/viewer.html` | `reporting/templates/viewer.html` | **直接复制** |
| `agents/grader.md` | `agents/grader.md` | **直接复制**——LLM 评分 prompt 模板 |
| `agents/comparator.md` | `agents/comparator.md` | **直接复制**——盲评 prompt 模板 |
| `agents/analyzer.md` | `agents/analyzer.md` | **直接复制**——深度分析 prompt 模板 |
| `references/schemas.md` | `models/` → 30+ Pydantic 模型 | Markdown schema 转为可执行数据模型 |
| `scripts/quick_validate.py` | `static_analysis/checks.py` → 11 项检查 | 验证规则融入自动化检查函数 |

**关键设计决策**：所有 JSON 输出字段名严格保持与 skill-creator 兼容。

---

## 2. 来自 skill-benchmark 的静态分析规则

skill-benchmark 的 11 条验证规则被转为可执行检查函数：

| # | 规则 | 检查内容 |
|---|------|----------|
| 1 | description_length | 描述长度 ≥ 20 字符 |
| 2 | description_has_trigger | 包含触发条件关键词 |
| 3 | description_third_person | 使用第三人称 |
| 4 | name_kebab_case | name 符合 kebab-case |
| 5 | name_max_64 | name 长度 ≤ 64 |
| 6 | name_no_reserved | 非系统保留字 |
| 7 | skill_md_max_500_lines | SKILL.md 不超过 500 行 |
| 8 | has_examples | 包含示例 |
| 9 | reference_depth | references/ 扁平结构 |
| 10 | anti_fabrication | 包含反捏造指引 |
| 11 | source_documented | 路径遵循 |

---

## 3. 来自 skill-vetter 的安全审查

skill-vetter 的扫描逻辑以原生 Python 重新实现：secrets-scan + structure-check，保留相同的判定规则。

---

## 4. 来自 Anthropic Evals 最佳实践的 LLM 评分

`agents/grader.md` 的核心方法论来自 Anthropic 的 evals 最佳实践：

- **LLM-as-Judge** 范式：用 LLM 评判 LLM 输出
- **结构化输出**：要求 LLM 返回 JSON 数组，可机械解析
- **Evidence 强制**：每条判定必须附带证据引用

skill-eval 在此基础上新增了批量评估、双通道交叉校准、盲比较和深度分析。

---

## 5. skill-eval 原创：三层评估架构

### 5.1 A/B 对照实验 + delta 量化

**来源**：skill-creator 的 with_skill / without_skill 对照概念，结构化为 `ABTestRunner` 一体化引擎。

**理论**：随机对照实验（RCT）。with_skill 是实验组，without_skill 是对照组，delta 是处理效应。

### 5.2 遵循性引擎（Compliance Engine）

**来源**：原创。skill-creator 没有"过程遵循"检查。

**理论**：Instruction Following 是 LLM 评估的独立维度 [Zhou et al., IFEval 2023]。输出正确但过程违规意味着结果不可归因于 Skill。

**实现**：从 SKILL.md body 自动提取 6 类可检查指令（HEADING_STEP、TOOL_REQUIREMENT、CONSTRAINT、ANTI_FABRICATION、OUTPUT_FORMAT、REFERENCE_USAGE），按检查方式分为程序化（TOOL_CALL_CHECK）和 LLM 语义评判（LLM_JUDGE）。

### 5.3 评分校准（Calibration）

**来源**：原创。skill-creator 的评分没有评估"这个分数本身可不可信"。

**理论**：评分者间信度（Inter-Rater Reliability）[Cohen's Kappa, 1960]。两个独立评估者（程序化通道 vs LLM 语义通道）对同一 expectation 给出冲突判定时，分析分歧原因，输出可信度等级。

可信度等级作为**独立信号**输出——与评分、遵循性并列，三层不合成单一总分。消费者据此判断该轮分数的权重；UNRELIABLE 提示评分本身不可信、不宜作强依据（而非自动折扣某个总分）。

### 5.4 批量评测

**来源**：skill-creator 没有多 Skill 依赖编排。

**理论**：DAG 拓扑调度，按依赖顺序同层并行执行。

### 5.5 配置驱动架构

**理论**：关注点分离 + 12-Factor App 配置外置原则。框架代码零硬编码——所有领域知识通过 `eval-config.yaml` 注入。

---

## 6. 可信评测的六个支柱

| 支柱 | 直接来源 | 防止的问题 |
|------|----------|-----------|
| **A/B 对照实验** | skill-creator 的 with/without 对照 | 将模型自身能力误归因于 Skill |
| **双通道评分** | skill-creator grader.md + Anthropic evals | LLM 单一评分偏差 |
| **评分校准** | skill-eval 原创，Cohen's Kappa | 评分者不一致时盲目信任 |
| **过程遵循** | skill-eval 原创，IFEval + ISO 19011 | 输出正确的假阳性 |
| **盲比较与深度分析** | skill-creator comparator.md + analyzer.md | 单一评分维度 |
| **配置驱动** | 12-Factor App | 硬编码领域知识 |

---

## 参考文献

- Zheng, L. et al. "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena" (2023)
- Zhou, J. et al. "Instruction-Following Evaluation for Large Language Models" (2023)
- Cohen, J. "A Coefficient of Agreement for Nominal Scales" (1960)
- Dijkstra, E.W. "On the Role of Scientific Thought" (1974)
- Wiggins, A. *The Twelve-Factor App* (2011), Factor III: Config
- Anthropic: [Evals Best Practices](https://docs.anthropic.com/en/docs/build-with-claude/evaluations)
