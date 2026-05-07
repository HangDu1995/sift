# Skill-Eval：评测方法论与代码溯源

> 本文档阐述 skill-eval 评测体系的来源——哪些是从现有系统移植的，哪些是新建的，以及背后的理论依据。
> 实现级细节（函数签名、数据模型、CLI 参数）参见 `skill-eval-design.md`。

---

## 1. 来自 skill-creator 的移植

skill-creator（位于 `/Users/layne/codes/skills/skills/skill-creator`）是 Claude Code 的 Skill 开发工具链，包含一套脚本用于创建、评测和优化 Skill。skill-eval 从中移植了以下核心代码，将其从松散脚本改造为结构化 CLI 工具：

| skill-creator 原始文件 | skill-eval 目标 | 改造内容 |
|------------------------|----------------|----------|
| `scripts/utils.py` → `parse_skill_md()` | `parser.py` | 扩展为 5-tuple 返回值（增加 frontmatter_dict + body_text） |
| `scripts/run_eval.py` → `run_single_query()` | `optimization/trigger_eval.py` → `TriggerEvaluator` | 解耦 CLI 参数，保留 stream-json 解析 + `ProcessPoolExecutor` 并行 |
| `scripts/aggregate_benchmark.py` → `generate_benchmark()` | `execution/benchmark.py` → `BenchmarkAggregator` | 迁移到 Pydantic 模型，`calculate_stats()` 逻辑不变 |
| `scripts/improve_description.py` → `_call_claude()` | `optimization/description_improver.py` → `DescriptionImprover` | 提取为类，新增 >1024 字符自动缩短 |
| `scripts/run_loop.py` → `run_loop()` + `split_eval_set()` | `optimization/loop.py` → `OptimizationLoop` | 重构为类，保留分层 train/test 分割 |
| `eval-viewer/generate_review.py` | `reporting/viewer.py` → `ReportGenerator` | 移植 `find_runs()`、`embed_file()`、HTTP server，新增历史对比 |
| `eval-viewer/viewer.html` | `reporting/templates/viewer.html` | **直接复制**——自包含 HTML 评测结果查看器 |
| `agents/grader.md` | `agents/grader.md` | **直接复制**——LLM 评分 prompt 模板 |
| `agents/comparator.md` | `agents/comparator.md` | **直接复制**——盲评 prompt 模板 |
| `agents/analyzer.md` | `agents/analyzer.md` | **直接复制**——benchmark 分析 prompt 模板 |
| `references/schemas.md` | `models.py` → 30+ Pydantic 模型 | Markdown schema 定义转为可执行的数据模型 |
| `scripts/quick_validate.py` | `static_analysis/checks.py` → 11 项检查 | 验证规则融入自动化检查函数 |

**关键设计决策**：所有 JSON 输出（`grading.json`、`benchmark.json`）的字段名严格保持与 skill-creator 兼容——`expectations[].text`、`expectations[].passed`、`expectations[].evidence`、`with_skill`、`without_skill`——因为 `viewer.html` 依赖这些字段。

**evals.json 格式**：整个 eval 评测定义格式（`skill_name` + `evals[]`，每个 eval 含 `id`、`prompt`、`expectations`、`assertions`、`fixtures`）来自 skill-creator 的 schema 定义。skill-eval 的 `EvalLoader` 加载、验证、运行这套格式，`EvalInitGenerator` 自动生成它。

---

## 2. 来自 skill-benchmark 的静态分析规则

skill-benchmark 是一个用于验证 SKILL.md 质量的 Skill，其 SKILL.md 中定义了 11 条验证规则（description 长度、kebab-case 命名、行数限制、anti-fabrication 规则等）。

skill-eval 将这 11 条**从文字规则转为可执行检查函数**：

| # | 规则 | 检查内容 |
|---|------|----------|
| 1 | description_length | 描述长度 ≥ 20 且 ≤ 1024 字符 |
| 2 | description_has_trigger | 包含触发条件关键词 |
| 3 | description_third_person | 使用第三人称 |
| 4 | name_kebab_case | name 符合 kebab-case |
| 5 | name_max_64 | name 长度 ≤ 64 |
| 6 | name_no_reserved | 非系统保留字 |
| 7 | skill_md_max_500_lines | SKILL.md 不超过 500 行 |
| 8 | has_examples | 包含示例 |
| 9 | reference_depth | references/ 扁平结构 |
| 10 | anti_fabrication | 包含反捏造指引 |
| 11 | source_documented | 路径合规 |

---

## 3. 来自 skill-vetter 的安全审查

[skill-vetter](https://github.com/app-incubator-xyz/skill-vetter) 是一个 bash 脚本工具，用于在安装 Skill 前做安全检查。

skill-eval 以**原生 Python 重新实现**其扫描逻辑（而非调用 bash 脚本），保留相同的扫描语义和判定规则：

- **secrets-scan**：正则匹配硬编码 API Key/Token/凭证
- **structure-check**：SKILL.md 存在性、frontmatter 完整性、危险 shell 命令检测
- **判定规则**：任何 CRITICAL/HIGH → BLOCKED；仅 MEDIUM → REVIEW_NEEDED；无 findings → SAFE

新增 skill-vetter 没有的能力：Pydantic 结构化输出、`--format json` CI 模式、外部扫描器集成（aguara、skill-scanner）及优雅降级。

---

## 4. 来自 skill-monitor 的数据格式

skill-monitor 是 Claude Code 的运行时监控工具，输出 JSONL 格式的 Skill 调用日志。

skill-eval 的 `MonitorImporter` 读取这种 JSONL，按 skill_name + 时间窗口匹配，将运行时的真实 token/耗时数据补充到 `benchmark.json`。数据模型（`RuntimeRecord`、`RuntimeTokenUsage`）直接对应 skill-monitor 的 TypeScript `SkillCallData` 接口。

这是可选集成——不安装 skill-monitor 也能运行所有评测。

---

## 5. 来自 Anthropic Evals 最佳实践的 LLM 评分

`agents/grader.md`（直接复制自 skill-creator）定义了 LLM 评分的 prompt 模板。其核心方法论来自 Anthropic 的 [evals 最佳实践](https://docs.anthropic.com/en/docs/build-with-claude/evaluations)：

- **LLM-as-Judge** 范式 [Zheng et al., 2023]：用 LLM 评判 LLM 输出，处理需要语义理解的情况
- **结构化输出**：要求 LLM 返回 JSON 数组 `[{"index": 0, "passed": true, "evidence": "..."}]`，可机械解析
- **Evidence 强制**：每条判定必须附带证据引用，防止无理由的 PASS/FAIL

skill-eval 在此基础上新增了**批量评估**（一次 `claude -p` 调用评估所有 checkpoint expectations）和**双通道交叉校准**（见下节）。

---

## 6. skill-eval 原创：三层评估架构

以下模块没有对应的外部源码，是 skill-eval 从零设计的。它们的理论依据来自通用方法论，而非特定代码库。

### 6.1 A/B 对照实验 + delta 量化

**来源**：skill-creator 的 `run_eval.py` 已有 with_skill / without_skill 对照执行的概念，但它是散落在脚本中的逻辑。skill-eval 将其结构化为 `ABTestRunner` 一体化引擎。

**理论**：随机对照实验（RCT）。with_skill 是实验组，without_skill 是对照组，delta 是处理效应。每组跑 N 次（默认 3 次）取 mean/stddev，排除 LLM 输出的随机方差。

### 6.2 遵循性引擎（Compliance Engine）

**来源**：原创。skill-creator 没有"过程合规"检查。

**理论**：Instruction Following 是 LLM 评估的独立维度 [Zhou et al., IFEval 2023]。仅看输出不够——输出正确但过程违规意味着结果不可归因于 Skill。对应软件测试中的过程审计思想（ISO 19011）。

**实现**：从 SKILL.md body 自动提取 6 类可检查指令（HEADING_STEP、TOOL_REQUIREMENT、CONSTRAINT、ANTI_FABRICATION、OUTPUT_FORMAT、REFERENCE_USAGE），按检查方式分为程序化（TOOL_CALL_CHECK）和 LLM 语义评判（LLM_JUDGE）。

### 6.3 评分校准（Calibration）

**来源**：原创。skill-creator 的评分是"给一个分数就完了"，没有评估"这个分数本身可不可信"。

**理论**：评分者间信度（Inter-Rater Reliability）[Cohen's Kappa, 1960]。两个独立评估者（程序化通道 vs LLM 语义通道）对同一 expectation 给出冲突判定时，分析分歧原因，输出可信度等级（HIGH / MEDIUM / LOW / UNRELIABLE）。

可信度等级作为乘法系数作用于最终得分——UNRELIABLE 会将总分大幅折扣，防止误导性结论。

### 6.4 批量评测 + 级联归因

**来源**：skill-creator 的 `run_loop.py` 有触发优化循环，但没有多 Skill 依赖编排。

**理论**：DAG 拓扑调度（Make/Bazel 模式）+ 根因分析 [Chen et al., Pinpoint 2002]。对每个失败节点追溯依赖链，分类为 ROOT_CAUSE（自身缺陷）、CASCADE（级联传播）、INDEPENDENT（独立失败）。

### 6.5 配置驱动架构

**来源**：原创。v0.2 新增。

**理论**：关注点分离（Dijkstra）+ 12-Factor App 配置外置原则。框架代码零硬编码——不包含任何 domain/type/skill 关键词。所有领域知识通过 `eval-config.yaml` 注入，三层层叠覆盖（框架默认 → 项目配置 → Skill 本地配置），类似 CSS 层叠优先级模型。

---

## 7. 可信评测的五个支柱

| 支柱 | 直接来源 | 防止的问题 |
|------|----------|-----------|
| **A/B 对照实验** | skill-creator `run_eval.py` 的 with/without 对照概念 | 将模型自身能力误归因于 Skill |
| **双通道评分** | skill-creator `grader.md` + Anthropic evals 最佳实践 | LLM 单一评分偏差 |
| **评分校准** | skill-eval 原创，理论来自 Cohen's Kappa | 评分者不一致时盲目信任 |
| **过程合规** | skill-eval 原创，理论来自 IFEval + ISO 19011 | 输出正确的假阳性 |
| **级联归因** | skill-eval 原创，理论来自 Pinpoint RCA | 将上游失败误判为下游缺陷 |

---

## 参考文献

- Zheng, L. et al. "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena" (2023)
- Zhou, J. et al. "Instruction-Following Evaluation for Large Language Models" (2023)
- Cohen, J. "A Coefficient of Agreement for Nominal Scales" (1960)
- Chen, M. et al. "Pinpoint: Problem Determination in Large, Dynamic Internet Services" (2002)
- Dijkstra, E.W. "On the Role of Scientific Thought" (1974)
- Wiggins, A. *The Twelve-Factor App* (2011), Factor III: Config
- Anthropic: [Evals Best Practices](https://docs.anthropic.com/en/docs/build-with-claude/evaluations)
