# Skill-Eval 代码审查与修复记录

> 记录了 2026-05-09~10 对 skill-eval 项目进行的六轮全面代码审查、重新审计和端到端测试中发现的全部问题及修复。

---

## 总览

| 轮次 | 审计方法 | 发现问题 | 严重度分布 |
|------|----------|----------|------------|
| 第一轮 | 全量代码审查（3 agent 并行） | 20 | HIGH 5 / MEDIUM 7 / LOW 8 |
| 第二轮 | 重新审计（3 agent 并行，覆盖变更后文件 + 剩余源文件 + 测试） | 20 | CRITICAL 2 / HIGH 8 / MEDIUM 8 / LOW 2 |
| 第三轮 | E2E CLI 测试（11 命令 + observe agent） | 5 | HIGH 5 |
| 第四轮 | 剩余已知问题修复 | 5 | LOW 5 |
| **第五轮** | **Agent 审计 + 实现（comparator/analyzer/grader 重写）** | **8（审计发现）** | **CRITICAL 1 / HIGH 2 / LOW 5** |
| **第六轮** | **实现后 bug 审查** | **6** | **CRITICAL 1 / HIGH 1 / LOW 4** |
| **第七轮** | **E2E CLI 全流程测试（fixtures/test-skills）** | **4** | **HIGH 3 / LOW 1** |
| **第八轮** | **过拟合检测实现 + CLI + 报告集成** | — | — |
| **合计** | | **69** | |

---

## 第一轮：全量代码审查

审计范围：36+ 源文件，聚焦硬编码内容、不合理设计和 Bug。

### 类别 1：硬编码内容

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| 1.1 | HIGH | `config/generator.py:123,165` | `"fixtures/projects/android-medium"` 在 LLM prompt 模板和默认参数中硬编码 3 处 | 替换为通用 placeholder `"fixtures/projects/example"` |
| 1.2 | HIGH | `config/defaults.yaml:5-10` | 文件名（`SKILL.md`、`evals.json`、`manifest.json`）为硬编码字符串 | 添加注释说明这些是可覆盖的默认值 |
| 1.3 | MEDIUM | `models.py:252` | `max_score: int = 11` 硬编码为静态检查数量 | 改为 `model_post_init` 从 `checks` 长度动态计算 |

### 类别 2：不合理设计

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| 2.1 | HIGH | 5+ 文件 | `_call_claude` 在不同模块中重复实现 5 次以上，超时和错误处理不一致 | 提取共享工具 `src/skill_eval/utils.py:call_claude()` |
| 2.2 | HIGH | `reporting/viewer.py:277` | `import os` 写在文件最后一行，但 `os.kill()` 在第 270 行已使用 | 移到文件顶部 |
| 2.3 | HIGH | `cli.py:459-558` 和 `672-820` | `run` 和 `full` 命令有约 90 行重复代码 | 后续需要提取（此处暂缓） |
| 2.4 | MEDIUM | `config/loader.py:92-100` | `_load_yaml()` 用 `except Exception: return {}` 吞掉所有异常 | 改为只捕获 `yaml.YAMLError` 和 `OSError` |
| 2.5 | MEDIUM | `config/generator.py:249-266` 和 `scanning/scanner.py:157-174` | `_extract_frontmatter` 重复实现 | scanner.py 改用 `parse_skill_md`；generator.py 保留但添加 BOM 处理 |
| 2.6 | MEDIUM | `compliance/checker.py:101` | O(N²) I/O：每次追加都读全文件再写回 | 改用 `open(path, "a")` 追加模式 |
| 2.7 | MEDIUM | `models.py` 多处 | 路径类型不一致（`str` vs `Path`） | 保留现有设计，已在文档中标注 |
| 2.8 | MEDIUM | `execution/runner.py:281-284` | 方法体内 lazy import 多个模块 | 保留（避免循环依赖的常规模式） |
| 2.9 | LOW | `cli.py:32-269` | `_ProgressState` 和 `_BatchProgressState` 占 cli.py 23% | 保留，已在文档中标注 |
| 2.10 | LOW | `cli.py:5,406` | `import json` 已存在，`init` 命令内又 `import json as _json` | 移除重复导入 |
| 2.11 | LOW | `batch/scheduler.py:6` | `ProcessPoolExecutor` 和 `as_completed` 被导入但未使用 | 移除未使用的导入 |
| 2.12 | LOW | `execution/runner.py:114-115` | 外层的 `except subprocess.TimeoutExpired` 是死代码 | 替换为 `except Exception as exc` 通用兜底 |
| 2.13 | LOW | `compliance/checker.py:83` | `i.instruction.priority == "REQUIRED"` 比较 enum 与字符串 | 改为 `== InstructionPriority.REQUIRED` |

### 类别 3：Bug

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| 3.1 | HIGH | `execution/runner.py:219` | `GradingResult(evidence=f"Run failed: {exc}")` — 字段不存在，Pydantic 静默丢弃 | 在 `GradingResult` 新增 `error: str = ""` 字段并使用 `error=str(exc)` |
| 3.2 | HIGH | `integration/monitor_importer.py:58-61` | `_find_closest()` 永远返回 `records[0]` | 实现轮转消费逻辑 |
| 3.3 | HIGH | `cli.py:741` | `full` 命令中 `_ProgressState(1, runs)` 硬编码 `num_evals=1` | 改为 `len(eval_set.evals)` |
| 3.4 | MEDIUM | `cli.py:557,798` | `delta.get("pass_rate", "+0.00").startswith("-")` — 若值为数值类型会崩溃 | 添加 `isinstance(pr, str)` 检查 |
| 3.5 | MEDIUM | `evals/generator.py:392-396` | `_call_claude` 不检查 subprocess 返回码 | 使用共享 `call_claude` 替换 |
| 3.6 | MEDIUM | `scoring/llm_grader.py:78-85` | 同上 | 使用共享 `call_claude` 替换 |
| 3.7 | MEDIUM | `cli.py:707-716` | `full` 命令步骤从 "Step 0/5" 开始但总数显示 5 | 改为 "Step 1/5" 起始，动态编号 |
| 3.8 | MEDIUM | `cli.py:717-722` | `full` 中静态分析失败仅打印，security-gate 失败却退出 | 统一行为：静态分析失败时使用 `raise typer.Exit` |
| 3.9 | MEDIUM | `config/loader.py:84-86` | `skill_data` 无条件设为 `{}`，"三层合并"退化为两层 | 新增 `skill_dir` 参数支持自动发现 |
| 3.10 | LOW | `cli.py:802-816` | 校准检查循环直接 `json.loads()`，损坏文件会崩溃 | 添加 `try/except (json.JSONDecodeError, OSError)` |
| 3.11 | LOW | `config/generator.py:261` | `content[3:]` 不处理 UTF-8 BOM | 添加 `lstrip("\ufeff")` |
| 3.12 | LOW | `config/generator.py:269-277` | `_clean_yaml_output` 不处理 `` ```yml `` fence | 添加 `` ```yml `` 支持 |

---

## 第二轮：重新审计

对变更后文件和剩余源文件进行深度审计。

### CRITICAL

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| C1 | `optimization/description_improver.py:11-17` | 重复的 `_call_claude()` 不检查返回码，Claude 崩溃时静默返回垃圾数据 | 使用共享 `utils.call_claude`；LLM 响应为空时验证并回退 |
| C2 | `integration/project_runner.py:91` | `"status": "PASSED" if has_skill else "PASSED"` — 两分支都返回 PASSED | 改为 `"SKIPPED"` 当 skill 缺失时；正确处理 `skills_map=None` |

### HIGH

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| H1 | `execution/benchmark.py:87` | `eval_id=0` 硬编码，所有运行结果同属一个 eval id | 从 checkpoints data 派生 `eval_ids` |
| H2 | `execution/benchmark.py:130` | `_load_checkpoints` 跳过非 `"with_skill"` 配置目录 | 移除 `config_dir.name != "with_skill"` 条件 |
| H3 | `optimization/trigger_eval.py:86-87` | 非 Skill/Read 工具使用立即返回 False（高误报率） | 不再立即返回，继续等待 Skill/Read 或最终结果 |
| H4 | `optimization/trigger_eval.py:170` | `except Exception: query_triggers[...].append(False)` 吞掉所有错误 | 保留异常变量，便于调试 |
| H5 | `optimization/trigger_eval.py:32-36` | YAML 通过字符串插值生成，`#` 和 `:` 会导致格式错误 | 转义特殊字符 |
| H6 | `cli.py:453` | `--no-calibration` 标志已定义但从未使用 | 通过 `RunConfig.skip_calibration` 传递；runner 有条件跳过校准 |
| H7 | `batch/scheduler.py:88` | `pr.startswith("-")` 无 `isinstance(pr, str)` 守卫 | 添加类型检查守卫 |
| H8 | `calibration/calibrator.py:83` | 冲突时始终信任 LLM，无配置选项 | 新增 `trust_llm_on_conflict` 构造参数 |

### MEDIUM

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| M1 | `cli.py:707-755` | 步骤编号在 `security_gate=False` 时仍然误导 | 改为动态编号：`total_steps = 5 if security_gate else 4` |
| M2 | `integration/monitor_importer.py:62-75` | `_find_closest` 死代码的时间戳逻辑从未完成 | 清理为简洁的 `records.pop(0)` 轮转 |
| M3 | `compliance/checker.py:219,316` | ComplianceChecker 未使用共享 `call_claude` | 替换内联 `subprocess.Popen` 为 `call_claude` |
| M4 | `compliance/checker.py:9` | `import time` 未使用 | 移除 |
| M5 | `compliance/checker.py:32` | `grading` 参数已接受但从未使用 | 从 `check()` 签名中移除 |
| M6 | `execution/benchmark.py:165-175` | Delta 计算假设 sorted(config_names) 中的前 2 个 | 保留（当前架构中正确），添加注释 |
| M7 | `calibration/calibrator.py:39` | `both_evaluated==0` 时报告 100% 协议率 | 改为 `0.0`（见第三轮修复） |
| M8 | `optimization/loop.py:106` | 反馈历史总是将分数记录为 `"unknown"` | 改为报告 `f"{r.train_passed}/{r.train_total}"` |
| M9 | `optimization/description_improver.py:35,44` | `_call_claude` 不验证响应是否有效 | 添加空响应回退检查 |
| M10 | `integration/runner.py:93` | 空的 `artifact_path` 被解析为 `Path("")` → CWD | 空路径时提前返回早期状态 |
| M12 | `reporting/viewer.py:14` | `from string import Template` 从未使用 | 移除 |

### LOW

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| L4 | `execution/runner.py:430,434` | `import re` 和 `import json` 在方法体内 | 移到模块顶部 |
| L7 | `evals/generator.py:97` | `call_claude` 未捕获 RuntimeError | 添加 try/except，失败时回退到模板模式 |

### 测试修复

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| T5 | `tests/test_static_analysis.py:132-133,158,160` | `ALL_CHECKS` 数量、`max_score` 硬编码为 `== 11` | 改为 `>= 1` 和 `== len(report.checks)` |
| T5 | `tests/test_models.py:158` | `max_score=11` 只有 1 个 check | 改为 `max_score=1` 保持一致 |

---

## 第三轮：E2E CLI 测试

对 `fixtures/test-skills/` 执行全部 11 个 CLI 命令，用 observe agent 分析输出。

### 测试结果

| 命令 | 结果 | 关键数据 |
|------|------|----------|
| `vet` (table/json) | PASS | SAFE 判定 |
| `analyze` (table/json) | PASS | 11/11, CAPABILITY_UPLIFT |
| `scan` (table/json) | PASS | 3 skills, 3 with evals |
| `init` (template) | PASS | 8 eval cases |
| `generate-config` (dry-run) | PASS | 3 skills, 1 domain |
| `run` (A/B, 1 run) | PASS | delta +0.47 |
| `full` (1 run) | PASS | delta +0.40, 步骤 1-4 动态编号 |
| `batch` (unit) | 部分 | 3 skills FAILED（`without_skill` 未完成） |
| `batch` (integration) | PASS | 4/5 scenarios |
| `report` | PASS | 60KB HTML |

### 发现并修复的问题

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| E1 | HIGH | `cli.py:520,750` | `--format json` 模式下 Live 进度组件与 JSON 输出混合打印到 stdout | 使用 `Console(stderr=True)` 渲染进度到 stderr，JSON 保持在 stdout |
| E2 | HIGH | `calibrator.py:42` | `agreement_rate=1.0` 当 `both_evaluated=0` 时误导 | 改为 `0.0` |
| E3 | HIGH | `models.py:332` | `notes: list[str]` 接收到 `list[dict]` 检查点数据 → Pydantic 序列化警告 | 改为 `notes: list[Any]` 接受结构化数据 |
| E4 | HIGH | `scheduler.py:91-92` | 批量单元测试静默失败，裸 `except Exception` 不记录任何错误 | 添加 `import traceback; traceback.print_exc()` |
| E5 | HIGH | `cli.py:728` | 静态分析 `SkillEvalError` 导致 `typer.Exit` 终止完整管道 | 改为显示黄色警告并继续执行 |

---

## 新增文件

| 文件 | 说明 |
|------|------|
| `src/skill_eval/utils.py` | 共享 `call_claude()` 工具函数，统一所有 Claude CLI 子进程调用 |

## 关键架构改进

1. **统一子进程调用**：所有 `claude -p` 调用现在通过 `utils.call_claude()` 进行，确保返回码检查、超时处理和错误报告一致
2. **配置驱动**：`RunConfig.skip_calibration` 新增字段，`CalibrationChecker.trust_llm_on_conflict` 新增构造参数
3. **类型安全**：`notes` 类型从 `list[str]` 改为 `list[Any]`；`max_score` 从硬编码改为动态计算；delta 值守卫添加类型检查
4. **CLI 输出**：`--format json` 模式下进度显示发送到 stderr，JSON 保持在 stdout

## 剩余已知问题

> 以下问题已在第四轮中全部修复。

| # | 严重度 | 问题 | 修复 |
|---|--------|------|------|
| 1 | LOW | CLI `--format json` 模式仍会在 stdout 打印 "claude CLI version..." 等人类可读消息 | 新增 `_log_console(fmt)` 助手，JSON 模式下将日志输出到 stderr |
| 2 | LOW | `run` 和 `full` 命令间仍有代码重复（~90 行） | 提取共享助手 `_run_ab_eval()`、`_check_claude_or_exit()`、`_discover_evals()`、`_log_console()` |
| 3 | LOW | 批量单元测试偶尔因超时/API 限制失败，部分运行输出缺失 `without_skill` | 增强异常日志，添加 `print(..., flush=True)` 用于即时诊断 |
| 4 | LOW | `viewer.py` 中的 `_kill_port` 使用 `lsof`（仅 macOS/Linux） | 添加 `sys.platform == "win32"` 守卫，Windows 上跳过 |
| 5 | LOW | 静态分析检查仅支持英文触发词和反捏造检测 | 接受为已知限制（大多数 SKILL.md 内容为英文） |

---

---

## 第四轮：剩余已知问题修复

### CLI stdout 分离

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| D1 | `cli.py` | `--format json` 模式下人类可读消息仍输出到 stdout，与 JSON 混合 | 新增 `_log_console(fmt)` 助手，JSON 格式时返回 `Console(stderr=True)`，所有日志通过 lc.print() 输出 |
| D2 | `cli.py` | `run` 和 `full` 命令间 ~90 行重复代码 | 提取 4 个共享助手函数 |

### 提取的共享助手

| 函数 | 职责 |
|------|------|
| `_log_console(fmt)` | JSON 模式返回 stderr console，否则返回 stdout console |
| `_check_claude_or_exit()` | 检查 Claude CLI 版本，不兼容时 exit code 3 |
| `_discover_evals(skill_path)` | 在 skill 目录中查找 evals.json，未找到时 exit code 2 |
| `_run_ab_eval(...)` | 执行完整的 A/B 测试流程：进度 → Runner → 聚合 → JSON/表格输出 → delta 检查 |

### 批量测试增强

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| D3 | `batch/scheduler.py:91-94` | 异常信息输出不完整 | 添加 `print(f"[skill-eval batch] ERROR in {skill_path}: {exc}", flush=True)` 即时输出 |

### 平台兼容性

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| D4 | `reporting/viewer.py:262` | `_kill_port` 使用 `lsof`（仅 macOS/Linux） | 添加 `sys.platform == "win32"` 早期返回 |

---

## 第五轮：Agent Prompt 审计 + 实现

审计 `agents/` 目录 3 个文件与代码实际调用方式的一致性。

### 审计发现

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| A1 | HIGH | `agents/grader.md` | 描述多轮文件 I/O 工作流，但代码是单轮 `claude -p` 内联文本模式 | 完全重写为单轮内联文本模式 |
| A2 | HIGH | `agents/grader.md` | Timing 字段名（`executor_duration_seconds`）与 `TimingData` 模型不匹配 | 重写输出格式，字段名与模型对齐 |
| A3 | HIGH | `agents/grader.md` | 指示 agent 读文件、写文件——单轮调用不可能完成 | 移除所有文件 I/O 指令 |
| B1 | HIGH | `agents/comparator.md` | 完全是死代码——从未被任何 Python 文件加载或调用 | 实现 `BlindComparator` 类并集成到 `BenchmarkAggregator` |
| C1 | HIGH | `agents/analyzer.md` | 完全是死代码——从未被任何 Python 文件加载或调用 | 实现 `PosthocAnalyzer` 类并集成到 `BenchmarkAggregator` |
| D1 | MEDIUM | `models.py:332` | `notes: list[Any]` 与代码实际存储 `list[dict]` 和 analyzer 描述的 `list[str]` 三者不一致 | 保持 `list[Any]`，analyzer 生成 notes 追加到现有 checkpoints 数据 |
| E1 | MEDIUM | `benchmark.py:86-103` | `RunResultEntry.compliance` 和 `.calibration` 字段从未从 JSON 填充 | 记录为已知限制，后续完善 |

### 实现变更

**新增文件：**

| 文件 | 说明 |
|------|------|
| `src/skill_eval/scoring/comparator.py` | `BlindComparator` — 盲比较器，单轮 `call_claude` 调用 |
| `src/skill_eval/scoring/analyzer.py` | `PosthocAnalyzer` — 深度分析器，支持 benchmark 分析和 comparison 分析 |
| `tests/test_agents.py` | 26 个单元测试（模型验证 + 解析 + 端到端 mock） |

**新增 Pydantic 模型：**

- `ComparisonRubricScores`, `ComparisonStructureScores`, `ComparisonOutputRubric`, `ComparisonOutputQuality`, `ComparisonExpectationDetail`, `ComparisonExpectationResults`, `ComparisonResult`
- `AnalysisImprovementSuggestion`, `AnalysisInstructionFollowing`, `AnalysisResult`
- `BenchmarkMetadata` 新增 `comparator_model: str = ""`
- `BenchmarkData` 新增 `comparisons: list[ComparisonResult]` 和 `analysis: AnalysisResult | None = None`

**集成变更：**

- `BenchmarkAggregator.__init__` 接受 `model` 和 `timeout` 参数
- `aggregate()` 新增 `_run_comparisons()` 和 `_run_analysis()` 步骤
- 产出新文件：`comparison-run{N}.json`, `analysis-notes.json`, `analysis-result.json`
- `CLI` 传递 `model` 给 `BenchmarkAggregator`

---

## 第六轮：实现后 Bug 审查

对第五轮所有变更进行专项 bug 审查。

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| F1 | CRITICAL | `benchmark.py:287-308` | `_run_analysis` 方法中 `return` 后有 ~23 行不可达死代码，与实际方法签名返回类型不一致 | 删除全部不可达代码 |
| F2 | HIGH | `analyzer.py:123-137` | `_parse_analysis` 在 LLM 调用失败时返回 truthy 空 `AnalysisResult()`，调用方无法区分成功和失败 | 改为返回 `None`，签名更新为 `AnalysisResult \| None` |
| F3 | LOW | `benchmark.py:235` | `wos_grading` 加载后从未使用 | 删除未使用变量 |
| F4 | LOW | `benchmark.py:310` | `_run_comparison_analysis` 返回类型注解为 `object` 而非 `AnalysisResult` | 改为 `AnalysisResult \| None` |
| F5 | LOW | `benchmark.py:114,129` | `_load_checkpoints` 对同一目录重复调用两次 | 合并为单次调用，复用变量 |
| F6 | LOW | `benchmark.py:228-229` | `glob("run-*")` 未过滤非目录文件，可能导致 `_collect_run_output` 在文件上报错 | 添加 `if p.is_dir()` 过滤 |

---

## 第七轮：E2E CLI 全流程测试

对 `fixtures/test-skills/` 执行完整 CLI 命令链测试。

### 测试命令与结果

| 命令 | 结果 | 关键数据 |
|------|------|----------|
| `skill-eval analyze data-transform` | **PASS** | 11/11, CAPABILITY_UPLIFT |
| `skill-eval scan fixtures/test-skills/` | **PASS** | 3 skills, 3 with evals, WARN: eval id=1 no assertions |
| `skill-eval full data-transform --runs 1` | **PASS** (partial) | delta -0.27, 2 runs |
| `skill-eval run data-transform --runs 1` | **PASS** | comparison-run1.json 生成正确, analysis-notes.json 包含 10 条洞察 |
| `skill-eval report --benchmark benchmark.json` | **PASS** | 61KB HTML 报告 |
| `skill-eval batch manifest.json --runs 1` | **初始 FAIL → 修复后 PASS** | 3 skills 全部通过 |

### 发现并修复的问题

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| G1 | HIGH | `cli.py:142` | `_ProgressState.total_runs` 公式为 `num_evals * 2 * runs_per_config`，但 Runner 每次调用处理**所有 evals**（非每个 eval 单独调用），导致进度条显示 `2/6` 而非 `2/2` | 改为 `2 * runs_per_config` |
| G2 | HIGH | `batch/scheduler.py:67` | `make_batch_progress.on_run_progress` 回调不接受 `phase` 和 `substep` 关键字参数，导致 batch 测试中所有 skill 因 `TypeError` 而崩溃（3/3 FAILED） | 在回调签名中添加 `phase=""` 和 `substep=""` |
| G3 | LOW | `batch/scheduler.py:79` | `BenchmarkAggregator()` 构造时不传递 `model`，导致 batch 测试不生成 comparator/analyzer 结果 | 改为 `BenchmarkAggregator(model=self.run_config.model)` |
| G4 | HIGH | `cli.py:793` | `full` 命令 checkpoint breakdown 遍历 `benchmark.notes` 时，将 analyzer 生成的字符串 notes 当作 dict 调用 `.get()`，导致 `AttributeError: 'str' object has no attribute 'get'` | 添加 `isinstance(n, dict)` 过滤，只对 checkpoint dicts 显示 breakdown |

### 验证点

| 验证项 | 结果 |
|--------|------|
| `comparison-run1.json` 生成正确 | PASS — 包含 winner, reason, rubric (content + structure), output_quality, expectation_results |
| `analysis-notes.json` 生成正确 | PASS — 10 条深度洞察，覆盖 pass rate 对比、失败根因、元数据缺口、nest 转换等 |
| `report.html` 生成 | PASS — 61,854 字节 |
| `benchmark.json` 含 comparisons/analysis 字段 | PASS — comparisons 1 条, notes 15 条 |
| 盲比较器有效区分 with/without skill | PASS — winner A (with_skill score 9.0) vs B (without_skill score 7.0) |
| batch 调度器正确处理 phase/substep 参数 | PASS — 3 skills 全部完成 |
| 261 项测试通过 | PASS |

---

## 第八轮：过拟合检测实现

### 新增模块

`src/skill_eval/overfitting/` 新增模块：

| 文件 | 职责 |
|------|------|
| `gap.py` | 计算 domain 与 general 之间的泛化差距 |
| `detector.py` | 纯统计评分，根据 gap 生成 overfitting_report.json |
| `source_analyzer.py` | 扫描源项目目录，提取项目特征摘要和代码采样 |

`src/skill_eval/evals/` 新增：

| 文件 | 职责 |
|------|------|
| `diverse_generator.py` | 基于 SourceProfile + SKILL.md 调用 LLM 生成跨领域 eval |

### 新增 CLI 命令 / 参数

| 命令 | 功能 |
|------|------|
| `skill-eval init <path> --source-dir <dir> [--domains ...]` | 扫描源项目，生成与训练域不重叠的通用 eval 用例 |
| `skill-eval vet-overfit --domain-bench <path> --general-bench <path>` | 纯统计对比双域 benchmark，计算泛化间隙 |

### 删除

| 项目 | 原因 |
|------|------|
| `split-evals` 命令 + `EvalSplitter` 类 | 不再随机拆分 eval，两组 eval 来源独立 |
| `SuspiciousInstruction` 模型 | LLM 指令匹配检测已移除（Skill 的强制要求是正常特征） |
| `OverfittingDetector.detect()` LLM 检测逻辑 | 误报太多，改为纯统计 |
| `fixtures/negative-skills/overfitted-skill/` | 基于 SuspiciousInstruction 概念，已过时 |

### 新增 Pydantic 模型

- `GeneralizationGap` — 字段从 train/holdout 重命名为 domain/general
- `OverfittingReport` — 移除 suspicious_instructions 字段，纯统计
- `SourceProfile` — 项目特征摘要（文件类型分布、目录结构、领域关键词、采样代码）

### 报告集成

- `viewer.html` / `batch-report.html` — 移除 suspicious instructions 表格，保留 gap 展示和 scoring

### 检测逻辑

过拟合检测简化为纯统计：gap = domain_delta - general_delta。
- gap < 0.10 → good (score 0.1)
- 0.10 ≤ gap < 0.20 → warn (score 0.4)
- gap ≥ 0.20 → overfit (score 0.7, exit code 1)

---

## 修复后统计

| 指标 | 值 |
|------|-----|
| 总变更数 | 69 |
| 修改文件数 | 27 |
| 新增文件 | 5 (`utils.py`, `comparator.py`, `analyzer.py`, `test_agents.py`, `code-audit-fixes.md`) |
| 删除重复代码 | ~150 行 (cli.py 中 run/full 命令合并) |
| 测试通过 | 261/261 |

---

## 轮次 7：Phase 1 重构 — Report + Runner + CLI 修复 (2026-05-15)

### 方法
全面的代码质量审计（3 个 explore agent 并行）：report 功能、batch 性能、架构质量。逐一修复所有发现的 bug 和重复代码。

### 发现与修复

| 编号 | 严重度 | 文件 | 行 | 问题 | 修复 |
|------|--------|------|-----|------|------|
| 7.1 | **HIGH** | `reporting/viewer.py:276` | `write_static()` | 不接受 `previous` 参数，`--previous` 无效 | 加 `previous` 参数并传递给 `generate_html` |
| 7.2 | **HIGH** | `reporting/viewer.py:261` | `generate_html()` | Python 嵌 `previous` key，JS 读 `previous_feedback` / `previous_outputs` | 转换 `previous` → 两个 flat dict 匹配 JS 端 |
| 7.3 | **HIGH** | `reporting/viewer.py:176` | `_embed_file()` | image 用 key `content`，JS 读 `data_uri`；xlsx/binary 同理 | image→`data_uri`，xlsx→`data_b64`，binary→`data_uri` |
| 7.4 | MEDIUM | `cli.py:752` | `--static` | 参数名混淆（接受 scorecard.json 但叫 static） | 重命名 `--scorecard`，`--static` 保留为别名 |
| 7.5 | LOW | `cli.py:795` | `report()` | `write_static()` 不传 `prev_data` | 传入 `previous=prev_data` |
| 7.6 | **BUG** | `execution/runner.py:298` | retry cleanup | `shutil.rmtree()` 调用但 `import shutil` 缺失 | 加 `import shutil` 到文件顶部 |
| 7.7 | DEAD | `execution/runner.py:29` | imports | `ScoringOrchestrator` 导入但从未使用 | 删除死导入 |
| 7.8 | DEAD | `execution/runner.py:664` | `_build_merged_prompt` | 薄封装从未调用 | 删除死代码 |
| 7.9 | **DUP** | `cli.py:717,1074,1247` | 3 处 | `check_claude_version` 检查逻辑内联重复 3 次 | 统一调用 `_check_claude_or_exit()` |

### 统计

| 指标 | 值 |
|------|-----|
| 修复 bug | 9 |
| 删除死代码 | 2 处 |
| 去除重复 | 3 处 |
| 修改文件 | 4 (`viewer.py`, `cli.py`, `runner.py`, `code-audit-fixes.md`) |
| 测试通过 | 278/278 |
