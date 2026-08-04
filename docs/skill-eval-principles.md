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

> **形态与演进**：skill-creator 是 Claude Code 的**交互式 meta-skill**（对话式建/测 skill、人评审迭代），不是 CLI。skill-eval 2026-04 从它移植评测脚本并改造成结构化 CLI 后，两者同期各自演化——skill-creator 在 2.0（2026-01）已具备成熟 Eval（A/B 对照、benchmark、盲比较、review UI、4 模式 Create/Eval/Improve/Benchmark），skill-eval 则往 CI / 批量 / 过程审计 / 客观门禁方向独立扩展。故 skill-eval 的价值不在"skill-creator 不能评测"（它能），而在**形态互补**：探索期对话式 vs 成熟期工程化 CLI。

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

skill-eval 在此基础上新增了批量评估、结果×过程 coherence 校准、盲比较和深度分析。

---

## 5. skill-eval 原创：三层评估架构

### 5.1 A/B 对照实验 + delta 量化

**来源**：skill-creator 的 with_skill / without_skill 对照概念，结构化为 `ABTestRunner` 一体化引擎。

**理论**：随机对照实验（RCT）。with_skill 是实验组，without_skill 是对照组，delta 是处理效应。

### 5.2 遵循性引擎（Compliance Engine）

**来源**：原创。skill-creator 没有"过程遵循"检查。

**理论**：Instruction Following 是 LLM 评估的独立维度 [Zhou et al., IFEval 2023]。输出正确但过程违规意味着结果不可归因于 Skill。

**实现**：从 SKILL.md body 自动提取 6 类可检查指令（HEADING_STEP、TOOL_REQUIREMENT、CONSTRAINT、ANTI_FABRICATION、OUTPUT_FORMAT、REFERENCE_USAGE），按语义归 4 类（条件性 / 禁令 / 步骤 / 输出格式）各配方向正确的判法——每条出五态 verdict（`PASS` / `FAIL` / `N/A` 不适用 / `UNRESOLVED` 没判成 / `INDETERMINATE` 判不准）+ `method` 标（`PROGRAMMATIC` 程序对账 / `LLM` 语义判定）两个正交维度。程序化（tool_trace 命中 / 输出子串匹配）是确定性判定、不调 LLM；LLM 类承担语义判定（禁令主动查违规、步骤/条件性判走过或该走没走）。

### 5.3 校准（Calibration）—— 结果×过程 coherence

**来源**：原创。skill-creator 的评分没有评估"这次执行的输出与过程协不协调"。

**理论**：跨层一致性（triangulation）。结果面（scoring，查输出）与过程面（compliance，查是否按 SKILL.md 执行）是同一技能的两个独立视角；二者矛盾（输出对了但过程违反硬性规则）→ 高分存疑。两轴都避 LLM 方差：过程轴只押 prohibition/output 硬类违规（不把有方差的 step 判决塞进判决），结果轴优先押客观 programmatic 双率（纯主观题无客观锚才回退主观率并透明标注）。

5 态 verdict（可信/高分存疑/可修复/技能失效/过程未知）作为**独立信号**输出——与评分、遵循性并列，三层不合成单一总分。gate 读它作 **soft warning**（不进 decision、不折扣某个总分；仍是分诊辅助人判断）。

### 5.4 批量评测

**来源**：skill-creator 没有多 Skill 依赖编排。

**理论**：DAG 拓扑调度，按依赖顺序同层并行执行。

### 5.5 配置驱动架构

**理论**：关注点分离 + 12-Factor App 配置外置原则。框架代码零硬编码——所有领域知识通过 `eval-config.yaml` 注入。

### 5.6 工程化扩展（2026-05 起）

三层架构之外，skill-eval 在 2026-05 后陆续新增的能力（均为 skill-eval 原创；静态分析 / 安全审查来源见 §2 / §3）：

| 能力 | 来源 | 解决的问题 |
|------|------|-----------|
| **确定性 programmatic 断言 + 双率** | 原创 | skill-creator 只有 LLM grader 单率；skill-eval 拆出零 LLM、不可作弊的客观判定（`FILE_EXISTS` / `TOOL_CALLED` 等）与 LLM 判定分开报 |
| **post-hoc 门禁 + CI** | 原创 | skill-creator 交互式、无 exit code / 无 CI；skill-eval `gate` 出 PASS/FAIL，exit code 驱动 pipeline |
| **路径2 audit** | 原创 | 真实会话 transcript 与 SKILL.md 对账（cc / opencode 双源），skill-creator 只跑自生成测试、无生产对账 |
| **agent 对账（`--kind agent`）** | 原创 | 审被 dispatch 的 subagent（按 `agent` 归属字段切，非 Skill 调用锚点；扁平切、dual-nature 各审各的）—— audit 的 subagent 维度扩展 |
| **root / workflow 对账（`--kind root`）** | 原创 | 审顶层主 agent / 编排者（按主作用域切：cc `parent_tool_use_id` 为空 / opencode `session.info.agent`），其声明常是 workflow 级 SKILL.md——audit 的整 session / workflow 维度，与 skill/agent 对称补齐多作用域三元 |
| **路径1 真任务模式** | 原创 | `--task/--cwd` spawn 真任务 + 双执行器（cc / opencode） |
| **基准题集 freeze** | 原创 | 不可变签名 + skill_name 校验，跨 run / CI 同卷（skill-creator 的 evals.json 可复用但无冻结） |
| **refine + per-SKILL hash 缓存** | 原创 | LLM 精筛 extractor 误抽的伪指令（教程示例被当指令），按 SKILL.md hash 缓存 |
| **FAIL 噪声判别** | 原创 | 区分"工具噪声 / 疑似 skill 问题 / 待确认"，避免读者误把工具噪声当 skill 缺陷 |
| **结构化 records transcript** | 原创 | canonical 三源统一（cc / structured / opencode），供 audit 消费外部 producer |
| **trigger-accuracy（第 4 维）** | 原创 | 测 cc 仅凭 description 是否自然触发 skill（should / should-not 两类探题），而非测 body 质量——前三层 force-inject body 测不到的触发面；纯诊断无 gate。含 anti-leakage 词面命中校验（探题不照抄 description）+ 措辞 / 长度分层诊断（看 cc 真懂还是靠字面命中触发） |
| **coverage（出题漏测诊断）** | 原创 | 一次性 LLM 把 SKILL.md 内容点 × evals 做覆盖映射，`covered_by` 空 = 漏测盲区（`run` 够不到，再跑多少轮也测不着），`--augment` 复用 init 生成器自动补题；纯诊断无 gate |
| **critique（正文内容质量诊断）** | 借鉴 SkillLens | 补 static_analysis 只查格式、不查正文内容质量的空白；LLM 按 4 维 rubric 给读者反馈（met/partial/missing + evidence），纯诊断无 gate。①②③ rubric 来自 SkillLens（有数据验证）/ ④ 示例驱动经验自加 |

---

## 6. 可信评测的六个支柱

| 支柱 | 直接来源 | 防止的问题 |
|------|----------|-----------|
| **A/B 对照实验** | skill-creator 的 with/without 对照 | 将模型自身能力误归因于 Skill |
| **双通道评分** | skill-creator grader.md + Anthropic evals | LLM 单一评分偏差 |
| **校准** | skill-eval 原创，跨层一致性 triangulation | 只看输出对就信、忽略过程违规 |
| **过程遵循** | skill-eval 原创，IFEval + ISO 19011 | 输出正确的假阳性 |
| **盲比较与深度分析** | skill-creator comparator.md + analyzer.md | 单一评分维度 |
| **配置驱动** | 12-Factor App | 硬编码领域知识 |

---

## 参考文献

- Zheng, L. et al. "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena" (2023)
- Zhou, J. et al. "Instruction-Following Evaluation for Large Language Models" (2023)
- Dijkstra, E.W. "On the Role of Scientific Thought" (1974)
- Wiggins, A. *The Twelve-Factor App* (2011), Factor III: Config
- Anthropic: [Evals Best Practices](https://docs.anthropic.com/en/docs/build-with-claude/evaluations)
- Anthropic: [skill-creator skill](https://github.com/anthropics/skills/tree/main/skills/skill-creator) — 盲比较 comparator / rubric 评分设计来源（`agents/comparator.md`）
