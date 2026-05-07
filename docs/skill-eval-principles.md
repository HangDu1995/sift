# Skill-Eval 底层原理与参考出处

> 本文档解释 skill-eval 框架的核心设计原理，以及各概念的参考来源。

---

## 1. 系统定位

skill-eval 是一个**独立 Python CLI 工具**，对 Claude Code 的 Skill（以 `SKILL.md` 为核心的技能定义文件）进行自动化评测。

**核心设计理念**：将 Skill 开发从基于直觉的"艺术"转变为可度量、可迭代、可回归验证的"工程实践"。

**系统边界**：

| 边界 | 内容 |
|------|------|
| 输入 | 一个或多个 `SKILL.md` 目录 + 可选的 `evals.json` |
| 输出 | `benchmark.json` + `grading.json` + `compliance.json` + `run_debug.json` + HTML 报告 |
| 外部依赖 | 本地 `claude` CLI（`claude -p` 子进程调用） |
| 不依赖 | Claude Code 运行时、OpenCode 运行时、第三方 API Key |

---

## 2. 核心原理

### 2.1 A/B 测试模型（with_skill vs without_skill）

**原理**：评测的核心是测量 Skill 的**增量价值**（delta），而非绝对质量。

```
with_skill    → claude -p --skill <SKILL.md>   → 测量 skill 加成后的表现
without_skill → claude -p --bare               → 测量无 skill 的基线表现
delta = with_skill.pass_rate - without_skill.pass_rate
```

每次评测运行两组配置（`with_skill` / `without_skill`），每组跑 `N` 次（默认 3 次），比较通过率的统计差异。

**参考来源**：
- **A/B 测试方法论**（通用实验设计）— 控制变量法，with_skill 是实验组，without_skill 是对照组
- `--bare` flag 来自 Claude Code CLI 的无 skill 模式（参考 `runner.py:74`）

**实现位置**：`src/skill_eval/execution/runner.py` → `ABTestRunner.run_ab_test()`

### 2.2 静态分析引擎（零 API 成本）

**原理**：不调用任何 LLM，纯文件系统 + 文本分析。

```
StaticAnalyzer.analyze(skill_path)
  → parse SKILL.md frontmatter
  → run checks (11 项)
  → return AnalysisReport (score: 0-11)
```

检查项包括：SKILL.md 存在性、frontmatter 完整性、指令结构、引用文件存在性等。

**参考来源**：
- 通用的静态代码分析思想（linter、type checker 模式）
- 具体检查项设计基于 `agentskills.io` 规范对 SKILL.md 格式的要求

**实现位置**：`src/skill_eval/static_analysis/analyzer.py` + `checks.py` + `classifier.py`

### 2.3 混合评分引擎（程序化 + LLM）

**原理**：双层评分，独立开关。

**程序化断言**（ProgrammaticChecker）：
- 6 种断言类型：`FILE_EXISTS`, `CONTAINS_PATTERN`, `JSON_HAS_FIELD`, `LINE_COUNT`, `NOT_CONTAINS`, `CUSTOM_SCRIPT`
- 确定性、可重复、零 API 成本
- 每个 assertion 有 `name`、`type`、`params`

**LLM 评分**（LLMGrader）：
- 通过 `claude -p` 调用 LLM 评判 expectation 是否被 transcript 覆盖
- 批量评估：一次 prompt 包含所有 expectations + transcript 摘要
- 输出结构化 JSON：`[{"index": 0, "passed": true, "evidence": "..."}]`

**参考来源**：
- 程序化断言设计参考通用测试框架的 matcher 模式（pytest、Jest）
- LLM 评分参考 Anthropic 的 [evals 最佳实践](https://docs.anthropic.com/en/docs/build-with-claude/evaluations)
- `CUSTOM_SCRIPT` 类型允许用户自定义 Python 脚本作为断言，参考了 CI 系统的自定义 step 模式

**实现位置**：
- `src/skill_eval/scoring/programmatic.py` → `ProgrammaticChecker`
- `src/skill_eval/scoring/llm_grader.py` → `LLMGrader`
- `src/skill_eval/scoring/orchestrator.py` → `ScoringOrchestrator`（混合协调）

### 2.4 遵循性引擎（Compliance Engine）

**原理**：检查 LLM 执行过程是否**真正遵循** SKILL.md 中定义的指令，而非仅看输出结果。

```
InstructionExtractor.extract(skill_body)
  → 解析 SKILL.md 正文
  → 提取 InstructionCheck 列表
  → 每个 check 有 source（HEADING_STEP/TOOL_REQUIREMENT/CONSTRAINT/...）
  → 每个 check 有 check_method（PATTERN_MATCH/TOOL_CALL_CHECK/FILE_PRESENCE/LLM_JUDGE）

ComplianceChecker.check(instructions, transcript, output_dir)
  → 对每个 instruction 执行对应检查
  → 返回 ComplianceReport（compliance_rate）
```

**参考来源**：
- **指令遵循度**（instruction following）评估是 LLM 评测的独立维度，参考了 Anthropic 的 helpfulness/faithfulness 评估框架
- `ANTI_FABRICATION` 类型参考了 hallucination detection 的思想

**实现位置**：`src/skill_eval/compliance/extractor.py` + `checker.py`

### 2.5 评分校准（Calibration）

**原理**：评估评分本身的可信度。

```
CalibrationChecker.calibrate(grading, compliance)
  → 比较 programmatic 和 LLM 评分的一致性
  → 识别分歧（disagreements）
  → 生成置信度信号（confidence_signals）
  → 输出 CalibrationReport（grade: HIGH/MEDIUM/LOW/UNRELIABLE）
```

校准等级的含义：
- `HIGH`：程序化和 LLM 评分高度一致
- `MEDIUM`：有少量分歧但可解释
- `LOW`：分歧较多，结果存疑
- `UNRELIABLE`：严重分歧，评分不可信

**参考来源**：
- **评估者的评估者**（meta-evaluation）思想，参考了机器学习中 inter-annotator agreement 的概念
- `agreement_rate` 类似 Cohen's Kappa 的简化版

**实现位置**：`src/skill_eval/calibration/calibrator.py`

### 2.6 批量评测与级联归因

**原理**：当多个 skill 之间存在依赖关系时，一个 skill 的失败可能是另一个 skill 的根因。

```
ManifestLoader.load/discover → SkillsManifest（声明 skills + depends_on）
BatchScheduler.run_unit_tests() → 执行所有 skill 的单元测试
BatchScheduler.generate_cascade_report() → 级联失败归因
```

DAG 依赖图 + 级联归因：
- `dag.py`：构建 skill 间的依赖 DAG
- `cascade.py`：分析失败是否为级联传播
- 失败类型：`ROOT_CAUSE`（根因）、`CASCADE`（级联）、`INDEPENDENT`（独立）

**参考来源**：
- DAG 调度是通用构建系统（Make、Bazel）的标准模式
- 级联归因参考了微服务故障分析中的 root cause analysis

**实现位置**：`src/skill_eval/batch/scheduler.py` + `dag.py` + `cascade.py` + `manifest.py`

### 2.7 安全审查引擎（Security Vetting）

**原理**：在执行 skill 之前检查其安全性，防止恶意 skill 执行危险操作。

```
SecurityVetter.vet(skill_path)
  → 运行多个扫描器（scanners）
  → 返回 SecurityReport（verdict: SAFE/REVIEW_NEEDED/BLOCKED）
```

**参考来源**：
- Agent 安全审查参考了 OWASP 和 AI Agent 安全指南
- 多扫描器架构参考了防御纵深（defense in depth）原则

**实现位置**：`src/skill_eval/security/vetter.py`

### 2.8 触发优化循环（Optimization Loop）

**原理**：自动优化 Skill 的 description（触发描述），提升触发准确性。

```
OptimizationLoop.run(skill_path, queries)
  → 用当前 description 测试触发率
  → LLM 生成改进版 description
  → 在 holdout 集上验证
  → 迭代 max_iterations 次
  → 返回 LoopResult（best_description, best_score）
```

**参考来源**：
- prompt optimization / description optimization 思想
- train/test split 防止过拟合（holdout_fraction）

**实现位置**：`src/skill_eval/optimization/loop.py` + `trigger_eval.py` + `description_improver.py`

---

## 3. 技术选型

| 选型 | 原因 |
|------|------|
| **Python 3.10+** | Claude Code 生态原生语言 |
| **Pydantic v2** | 数据模型校验 + JSON 序列化，models.py 524 行全是 Pydantic BaseModel |
| **typer** | CLI 框架，声明式命令定义，自动生成 help |
| **rich** | 终端美化，Live 进度条，Table 格式化输出 |
| **`claude -p --output-format stream-json`** | Claude Code CLI 的非交互模式，流式 JSON 输出便于解析 |
| **pytest** | 测试框架 |

---

## 4. 数据模型设计

所有数据模型定义在 `src/skill_eval/models.py`，使用 Pydantic BaseModel。

关键模型层次：

```
EvalSet → EvalCase → Assertion (AssertionType + params)
                    → FixtureFile
                    → expectations (list[str])

RunOutput → tool_calls, transcript, result_text, num_turns

GradingResult → expectations (list[ExpectationResult])
              → summary (GradingSummary)
              → claims, user_notes

BenchmarkData → metadata (BenchmarkMetadata)
              → runs (list[RunResultEntry])
              → run_summary (stats with mean/stddev)

ComplianceReport → checks (list[ComplianceResultItem])
                 → compliance_rate

CalibrationReport → disagreements, confidence_signals
                  → grade (ConfidenceGrade)

SecurityReport → scanners, findings → verdict

SkillsManifest → skills (list[SkillEntry] with depends_on)
               → integration_evals
```

**参考来源**：
- Pydantic v2 BaseModel 用于数据校验是 Python 社区最佳实践
- Schema 与 `skill-creator` 保持兼容（设计约束 C2），复用 viewer.html 等工具链

---

## 5. 执行流程

### 5.1 单 skill 评测（`skill-eval run`）

```
load evals.json → EvalSet
                ↓
for config in [with_skill, without_skill]:
    for run in range(runs_per_config):
        setup_fixtures()
        claude -p (with/without skill)
        → RunOutput (parse stream-json)
        score_checkpoints():
            ProgrammaticChecker.check(assertions)
            LLMGrader.batch_judge(expectations + transcript)
        ComplianceChecker.check(instructions + transcript) [with_skill only]
        CalibrationChecker.calibrate(grading, compliance)
        write run_debug.json + grading.json + calibration.json
                ↓
BenchmarkAggregator.aggregate()
→ benchmark.json + report.html
```

### 5.2 批量评测（`skill-eval batch`）

```
ManifestLoader.load() → SkillsManifest
                      ↓
BuildDAG(skills, depends_on)
TopologicalSort(dag)
                      ↓
for skill in sorted_order:
    run_ab_test(skill)
    record result (PASSED/FAILED/SKIPPED/CASCADE_FAILED)
                      ↓
generate_cascade_report()
→ cascade-report.json
```

### 5.3 完整流水线（`skill-eval full`）

```
Step 0: Security vetting (optional)
Step 1: Static analysis → scorecard
Step 2: Load evals.json
Step 3: A/B test
Step 4: Aggregate + report
Step 5: Exit code based on delta + calibration grade
```

---

## 6. Claude Code CLI 集成细节

### 6.1 `claude -p` 调用方式

```python
# runner.py ClaudeRunner.run()
cmd = ["claude", "-p", prompt, "--output-format", "stream-json",
       "--verbose", "--dangerously-skip-permissions",
       "--max-turns", str(max_turns)]
```

- `--output-format stream-json`：流式 JSON 输出，每行一个 JSON 事件
- `--verbose`：包含 tool calls 等详细信息
- `--dangerously-skip-permissions`：跳过权限确认（自动化必须）
- `--max-turns`：限制 agentic 循环轮次

**with_skill 模式**：通过在 `.claude/commands/` 下创建临时 `.md` 文件注入 skill（`runner.py:53-69`），用完后删除。

**without_skill 模式**：`--bare` flag 禁用所有 skill 加载。

### 6.2 stream-json 解析

```python
# runner.py ClaudeRunner._parse_stream_json()
for line in stdout.splitlines():
    event = json.loads(line)
    if event["type"] == "result":
        total_tokens, result_text, stop_reason = ...
    elif event["type"] == "assistant":
        for content_item in message["content"]:
            if content_item["type"] == "text": transcript += ...
            elif content_item["type"] == "tool_use": tool_calls += ...
```

**参考来源**：Claude Code CLI stream-json 格式来自 Anthropic 官方文档。

---

## 7. 与外部系统的关系

| 外部系统 | 关系 |
|---------|------|
| Claude Code CLI | 唯一的外部执行引擎，通过 subprocess 调用 |
| `skill-creator` | Schema 兼容，共享 viewer.html 等工具链 |
| `skill-monitor` | 可选导入 JSONL 数据到 benchmark（`import` 命令） |
| SKILL.md 格式 | 遵循 agentskills.io 开放标准 |
| OpenCode | 不依赖（设计约束 C3） |

---

## 8. 设计约束的出处

| 约束 | 出处 |
|------|------|
| C1: 仅支持 `claude -p` | 收窄范围，复用 CLI 认证 |
| C2: Schema 兼容 skill-creator | 共享工具链，避免重复开发 |
| C3: 独立 CLI，不依赖 subagent 运行时 | 可独立发布，无内部耦合 |
| C4: 静态分析零 API 成本 | 用户无 API 也能获得即时价值 |
| C5: 混合评分可独立开关 | 灵活权衡成本与覆盖度 |
| C6: Python 3.10+ | 与 skill-creator 生态一致 |
| C7: `--format json` + exit code | CI 就绪 |
| C8: 全局 UTF-8 | 跨平台一致性 |

---

## 9. 配置驱动架构（v0.2 新增）

### 9.1 设计原则：零硬编码

skill-eval 是**通用项目**，不包含任何领域特定的断言模板、domain 定义或 skill 关键词。所有项目特定内容通过 `eval-config.yaml` 注入。

```
skill-eval (通用框架)
    ↓ 读取
eval-config.yaml (项目级配置，由 LLM 生成)
    ↓ 定义
domain_templates, type_templates, integration_scenarios, skill_keywords
```

### 9.2 三层配置系统

```
config/defaults.yaml          ← 框架内置，通用默认值
    ↓ 深度合并
<project>/eval-config.yaml    ← 项目根目录，领域特定配置
    ↓ 深度合并
<skill>/eval-config.yaml      ← skill 目录内（可选），局部覆盖
```

加载器 `config/loader.py` 实现 `EvalConfig.from_dict()` + `load_config()` 三层合并。

**参考来源**：CSS 层叠覆盖机制 + 十二因素应用配置外置原则。

### 9.3 LLM 配置生成

```bash
skill-eval generate-config <skills_dir> --output eval-config.yaml
```

工作流程：
1. `scan_skills_metadata()` 扫描所有 SKILL.md frontmatter，提取 name/type/domain/description/tags
2. `ConfigGenerator.build_prompt()` 将元数据 + 可用断言类型组合成 prompt
3. `claude -p` 生成 eval-config.yaml
4. 项目根目录写入配置文件

**不同 skills 集合**（如 ArkTs-Core、其他项目）各自运行此命令生成自己的 `eval-config.yaml`。

### 9.4 插件机制

领域特定断言（如 ArkTS 的 `arkts_structure_valid`）不内置在框架中。通过 `CUSTOM_PLUGIN` 类型 + `AssertionPluginRegistry` 实现：

```python
registry = AssertionPluginRegistry()
registry.register("ARKTS_STRUCTURE_VALID", my_arkts_checker)
checker = ProgrammaticChecker(plugin_registry=registry)
```

每个项目在初始化时注册自己的领域插件。

### 9.5 新增模块

| 模块 | 位置 | 职责 |
|------|------|------|
| `config/loader.py` | `src/skill_eval/config/` | 三层配置加载 + `EvalConfig` model |
| `config/defaults.yaml` | `src/skill_eval/config/` | 框架默认值（无领域内容） |
| `config/generator.py` | `src/skill_eval/config/` | LLM 配置生成器 |
| `scoring/plugins.py` | `src/skill_eval/scoring/` | 插件注册机制 |
| `scoring/templates.py` | `src/skill_eval/scoring/` | 断言模板库（从 config 读取） |

### 9.6 扩展的 AssertionType

| 类型 | 描述 | 来源 |
|------|------|------|
| `CROSS_FILE_CONSISTENCY` | 检查相对 import 是否解析 | 通用 |
| `CODE_COMPILES` | 基础语法检查（括号匹配） | 通用 |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | 扩展点 |

加上原有 6 种（FILE_EXISTS, CONTAINS_PATTERN, JSON_HAS_FIELD, LINE_COUNT, NOT_CONTAINS, CUSTOM_SCRIPT），共 9 种内置断言类型。

### 9.7 Eval 生成增强

`EvalInitGenerator` 新增：
- `config=` 参数：从 config 的 `domain_templates` 自动注入领域断言
- `modes=` 参数：支持多 mode skill（如 create/refactor），每个 mode 生成独立 eval
- `EvalCase.mode` 字段：标记 eval 所属 mode
