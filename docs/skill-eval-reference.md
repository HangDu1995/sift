# Skill-Eval 参考手册

本文档包含 Skill-Eval 的完整技术细节。

---

## 目录

- [CLI 命令详细参数](#cli-命令详细参数)
  - [vet — 安全审查](#vet--安全审查)
  - [init — 初始化评测定义](#init--初始化评测定义)
  - [add — 追加评测用例](#add--追加评测用例)
  - [analyze — 静态分析](#analyze--静态分析)
  - [inspect — 题集体检](#inspect--题集体检)
  - [freeze — 冻结基线](#freeze--冻结基线)
  - [run — A/B 评测](#run--ab-评测)
  - [gate — 通过/失败判决](#gate--通过失败判决)
  - [audit — 真实 transcript 对账](#audit--真实-transcript-对账)
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

全局选项：`--version` 显示版本。

### `vet` — 安全审查

```bash
skill-eval vet path/to/skill/                # 表格输出 + 自动写 vet-report.json
skill-eval vet path/to/skill/ --format json  # JSON 输出
skill-eval vet path/to/skill/ --output report.json
skill-eval vet path/to/skill/ --skip-external # 跳过外部扫描器
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` / `-o` | 无 | 输出文件路径 |
| `--skip-external` | 关闭 | 跳过外部扫描器（aguara、skill-scanner） |

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

根据 SKILL.md 内容自动生成 `evals.json`。三种生成路径，优先级 `--from-transcript` > `--generate-prompts`（LLM）> 模板。

```bash
skill-eval init path/to/skill/                                  # LLM 生成(默认)
skill-eval init path/to/skill/ --num-evals 8                    # 限制数量
skill-eval init path/to/skill/ --from-transcript session.jsonl  # 从真实会话挖 eval
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--output` / `-o` | 无 | evals.json 输出目录 |
| `--generate-prompts` / `--no-generate-prompts` | 开 | 是否用 LLM 生成 L1 单元 eval |
| `--model` | 默认 | LLM 模型 |
| `--num-evals` | 无 | 最多生成的 eval case 数量 |
| `--from-transcript` | 无 | 从真实 session transcript 挖 evals（加料 SKILL.md，非替代） |

`--from-transcript` 解析 session transcript（模型、user turns、用到的 skill），重建 input_files / commands / produced_files，生成带结构化 assertions 的 eval。若 LLM 失败降级为 fallback eval，会带 `degraded:*` flag 并在终端告警（不再静默）。

### `add` — 追加评测用例

LLM 辅助或手动添加单个 eval case 到已有 evals.json。

```bash
# LLM 辅助：输入想法 → LLM 生成完整 eval
skill-eval add pdf/ --idea "encrypted PDF decryption with wrong password should fail" --level L2

# 手动模式
skill-eval add pdf/ --prompt "Decrypt file.pdf..." --expectation "output exists" --level L2

# 预览
skill-eval add pdf/ --idea "..." --dry-run
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--idea` | 无 | 自然语言描述，LLM 生成完整 eval case |
| `--prompt` | 无 | 手动指定 eval prompt |
| `--expectation` | 无 | 手动指定 expectation（可重复） |
| `--level` | L1 | 测试层级：L1 / L2 |
| `--dry-run` | false | 仅预览，不写入 |
| `--model` | 默认 | LLM 模型（用于 --idea） |

`--idea` 模式下，自动分配 id、自动推导 assertions 和 fixtures。`--idea` 与 `--prompt` 必须给其一。

### `analyze` — 静态分析

11 项自动化体检，**零 API 成本，<1s 完成**。输出 scorecard（`pass_count / max_score`）。

```bash
skill-eval analyze path/to/skill/                  # 表格
skill-eval analyze path/to/skill/ --format json -o scorecard.json
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` / `-o` | 无 | 输出文件路径 |

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

输出还包含 Skill 分类：`CAPABILITY_UPLIFT` 或 `ENCODED_PREFERENCE`。`score < max_score` → exit 1。

### `inspect` — 题集体检

对已有 evals.json 做静态体检：完整性、稳定性（programmatic/LLM 占比）、问题 flag。**不执行、不调 LLM、不下判决**——只报指标 + 关注 flag，由人决定是否 freeze。

```bash
skill-eval inspect path/to/evals.json            # 表格
skill-eval inspect path/to/evals.json --json     # 机器可读
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--json` | 关闭 | 输出 JSON |

注意位置参数是 **evals.json 路径**（不是 skill 目录），与多数命令不同。

### `freeze` — 冻结基线

把 evals.json 冻结成命名基线快照 + 溯源 sidecar，供 CI / 跨 run 复用同一份题。loader 零改动（产出的仍是标准 evals 格式）。同名覆盖（幂等）。

```bash
skill-eval freeze path/to/evals.json                         # → baseline.json + baseline.meta.json
skill-eval freeze path/to/evals.json --name v1 --skill-version 1.2
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--name` | baseline | 基线名（→ `<name>.json` + `<name>.meta.json`） |
| `--out` | 无 | 输出目录（默认 evals.json 同目录） |
| `--skill-version` | 无 | 目标 skill 版本（记入 meta） |

> 设计原则：**机制不策略**。同卷 run 内架构保证一个 `--evals` 同进程；跨 run 的完整性靠 git（消费方决定是否 commit baseline.json）；门禁阈值 / CI 落点归消费方。

### `run` — A/B 评测

两种模式：默认 A/B 评测（同一 skill 开/关对照，或 v1/v2 双 skill），或路径 1 真任务模式（`--task` 直接跑真实任务）。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找 | evals.json 路径 |
| `--task` | 无 | **路径 1 真任务模式**：直接 spawn 跑此真实任务（不读 evals.json），产物 transcript 可喂 `audit` |
| `--cwd` | 无 | 真任务模式工作目录（任务材料真实文件）；拷贝隔离，cc 不碰原目录 |
| `--runs` | 1 | 每种配置的运行次数 |
| `--model` | 用户默认 | claude -p 模型 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--timeout` | 300 | 每次运行超时（秒） |
| `--max-turns` | 10 | claude -p 最大轮数 |
| `--max-retries` | 2 | 每 eval 在 UNEVAL/出错时的最大重试（每次重跑 claude+scoring+compliance，重 skill 可设 0-1 省 token） |
| `--scoring` | hybrid | 评分模式：hybrid / programmatic / llm |
| `--no-compliance` / `--compliance` | 自动 | 跳过/强制遵循性检查（默认自动：`--runs<3` 时开） |
| `--no-calibration` | 关闭 | 跳过评分校准 |
| `--security-gate` | 关闭 | 安全审查返回 BLOCKED / REVIEW_NEEDED 时判失败 |
| `--output` / `-o` | output/ | 输出目录 |
| `--eval-samples` | 无 | 运行 eval 的数量或比例（如 `10` 或 `0.25`） |
| `--format` | table | 输出格式：table / json |
| `--skill-b` | 无 | **v1/v2 模式**：第二个 skill 路径（两边各注入各自 skill）；不给则默认 with/without（同 skill 开/关） |
| `--label-a` | skill 目录名 | v1/v2 模式 side A 显示标签 |
| `--label-b` | skill 目录名 | v1/v2 模式 side B 显示标签 |

**两种模式**：

- **默认 A/B**（无 `--task`）：位置参数 skill = 候选（side A）。不给 `--skill-b` → with/without 对照（同 skill 开/关）；给 `--skill-b <dir>` → v1/v2 双 skill 对照（两边各注入各自的 skill，标签默认取目录名）。
- **路径 1 真任务模式**（给 `--task`）：忽略 evals.json，spawn cc 在 `--cwd`（拷贝隔离）跑真任务，产 `run-*/transcript.jsonl`，结尾打印 `audit` 命令提示。此模式 `--evals`/`--scoring`/`--workers` 等大多不生效。

> **退出码**：`run` 是纯数据生产者——exit 0 = 产数据成功，2 = 输入错误，3 = 执行错误。**通过/失败判决不再由 `run` 的退出码决定**（旧版读混合 `delta.pass_rate<0` 退 1 的逻辑已退役），改由独立 [`gate`](#gate--通过失败判决) 命令出。`--security-gate` 触发时仍退 1。

**输出目录结构**（默认 with/without 模式；v1/v2 模式把 `with_skill`/`without_skill` 换成各自标签名）：

```
output/iteration-1/
  with_skill/
    eval-<id>/
      run-1/                 ← 多次 run(--runs N);UNEVAL/出错会有 run-1-retry-*
        outputs/             ← Claude 产生的文件(skill references 已自动拷入)
        grading.json
        compliance.json
        calibration.json
        run_debug.json
        timing.json
  without_skill/
    eval-<id>/run-1/ ...
  benchmark.json             ← 聚合结果(含 comparison_summary 盲评聚合)
  scorecard.json
  vet-report.json
  report.html
  gate.json                  ← 运行 gate 命令后产生(非 run 产出)
```

### `gate` — 通过/失败判决

对已有 benchmark.json 出通过/失败判决。**post-hoc 纯函数**——不重跑 claude、毫秒级、可反复改阈值重判（与 report/audit 同为 artifact 消费家族）。

```bash
skill-eval gate output/iteration-1/benchmark.json                        # 仅客观回归守卫
skill-eval gate output/iteration-1/benchmark.json --min-win-rate 0.5     # 加主观 gate
skill-eval gate output/iteration-1/benchmark.json --no-programmatic-guard --min-win-rate 0.6
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--min-win-rate` | 无 | 主观 gate 阈值（候选 **tie-排除**胜率）；不给则只走客观回归守卫 |
| `--min-valid` | 3 | 主观 gate 的最少决定性比较数 |
| `--no-programmatic-guard` | 关闭 | 关掉客观回归守卫（只看主观质量时用） |
| `--candidate` | 自动 | 候选侧名（默认 `comparison_summary.label_a` / 首个 side key） |
| `--out` / `-o` | benchmark 同目录 | gate.json 输出路径 |

**两轴**（§45 双主位）：
- **客观侧 programmatic 回归守卫**（默认常开）：`delta.pass_rate_programmatic < 0` → FAIL；`>= 0` → PASS；纯主观题（无 programmatic expectation，值为 null）→ 此轴不适用。
- **主观侧 comparison**（`--min-win-rate` opt-in）：tie-排除胜率 `a_wins/(a_wins+b_wins)` ≥ 阈值 → PASS；< 阈值且决定性比较数 ≥ `--min-valid` → FAIL；样本不足 → N/A。

**判决**：AND——任一适用轴 FAIL → FAIL；无 FAIL 但判不准/样本不足 → N/A；否则 PASS。写 `gate.json`，按 `PASS/FAIL/N/A` 退 `0/1/2`。

> exit 2 双语义：**使用错误**（文件/解析错误，不写 gate.json）与 **N/A 判决**（信号不足，写 gate.json）都退 2——靠 gate.json 是否存在区分。

### `audit` — 真实 transcript 对账

把真实会话 transcript 与 SKILL.md 对账，判指令遵循性。**不 re-run**，直接对账历史执行（与 fixture compliance 互补）。支持多 transcript 指令级聚合。

```bash
skill-eval audit path/to/skill/ --transcript run-1/transcript.jsonl -o audit-out/
skill-eval audit path/to/skill/ --transcript a.jsonl --transcript b.jsonl --format json
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--transcript` | **必填** | 真实会话 transcript jsonl（**可重复**，多 transcript 聚合） |
| `--output` / `-o` | ./output | 输出目录 |
| `--model` | 默认 | 语义判定 LLM |
| `--format` | table | table / json |

判定分四类（必令 / 步骤 / 输出格式 / 条件性指令），条件性指令走三态（遵循/违反/N/A）。判不准报 N/A + 警告，不保守 FAIL。

### `report` — HTML 报告

```bash
skill-eval report output/iteration-1/        # 生成静态 report.html
skill-eval report output/iteration-1/ --open # 生成并打开
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--benchmark` | 自动 | benchmark.json 路径 |
| `--previous` | 无 | 上一轮 benchmark.json（对比） |
| `--scorecard` / `--static` | 自动 | scorecard.json 路径（双别名） |
| `--open` | 关闭 | 生成后浏览器打开 |

自动发现 `benchmark.json`、`scorecard.json`、`vet-report.json`、`gate.json`（存在则标题旁渲染门禁徽章）。两个标签页：
- **输出** — 浏览 eval 的 prompt、输出文件、评分详情
- **基准测试** — 门禁徽章 + 安全面板 + 评分卡 + 异常检测 + 通过率对比（双率）+ 盲评对比 + 深度分析 + 检查点明细

### `batch` — 批量评测

跨多 skill 跑批量评测（仅 unit 层）。位置参数给 manifest JSON 或 skill 目录（目录则自动 discover）。

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

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--runs` | 3 | 每种配置的运行次数（注意：与 `run` 的默认 1 不同） |
| `--model` | 默认 | claude -p 模型 |
| `--max-turns` | 10 | claude -p 最大轮数 |
| `--max-retries` | 2 | 每 eval 最大重试 |
| `--fail-fast` | 关闭 | 首个失败即停 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--output` / `-o` | 无 | 输出目录 |

### `batch-report` — 批量 HTML 报告

```bash
skill-eval batch-report batch-workspace/ --open
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--manifest` | 自动 | manifest.json 路径 |
| `--output` / `-o` | workspace 下 | 输出 HTML 路径 |
| `--open` | 关闭 | 生成后浏览器打开 |

展示多 skill 对比、通过率对比、盲评胜率、各 Skill 详情。是独立顶层命令（非 `batch` 子命令）。

---

## 评测体系

### 三层质量保障

三层各自独立产出，**不合成单一总分**：

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

> 三层是**并列独立信号**，不存在复合 Score 公式（历史文档里 `Score = StaticCompliance + DeltaPassRate + ...` 的公式在代码中并未实现）。消费方按需组合判断。

### 双率：客观侧 vs 主观侧

每条 expectation/assertion 带 `source`（`programmatic` / `llm`）。评分与遵循性都按 source 拆**双率**，让干净信号不被 LLM 阅卷噪声污染：

| 率 | 来源 | 稳定性 | 信任度 |
|---|---|---|---|
| `*_programmatic` | 确定性断言（程序判定） | 稳（A=0 噪声） | 强信 |
| `*_llm` | LLM 语义判定 | 抖（阅卷噪声） | 看方向 |
| 总率（`pass_rate` / `compliance_rate`） | 全部混算 | 历史兼容 | 粗略 |

**双主位分工**（fixture-eval 下）：
- **客观侧主位** = `delta.pass_rate_programmatic`（候选 - 基线）——A/B 噪声抵消到 0，最稳。
- **主观侧主位** = 盲比较胜率（见下）——同一 LLM 同时看两边产出判谁更好，相对判断消阅卷噪声。

`gate` 命令正是按这两轴判决：客观侧读 `delta_programmatic`，主观侧读盲比较 tie-排除胜率。

### 盲比较（BlindComparator）

同一 LLM 同一次看 side A / side B 两份产出（不知哪个是候选），判谁更好 + 打 rubric 分。**相对判断、噪声抵消**，比 per-run 独立打分稳。位置随机化（coin-flip 决定 A/B 摆位），winner 自描述为语义标签（`label_a`/`label_b`/tie）。

聚合为 `comparison_summary`：`a_wins` / `b_wins` / `ties`（genuine tie，计入分母）/ `failed`（调用失败，**排除出分母**）/ `valid` / `win_rate`（= `a_wins/valid`，tie-**计入**）。

> 注意 `comparison_summary.win_rate` 是 tie-**计入**版；`gate --min-win-rate` 用的是 tie-**排除**版 `a_wins/(a_wins+b_wins)`，两者在阈值边界会判决翻转——故 gate 自行计算，不复用该字段。

盲比较只服务主观题（客观题 programmatic 已把噪声消到 0），且在 `programmatic`-only 评分下自动跳过。

### 评分模式

| 模式 | 说明 | LLM 调用 | 适用场景 |
|------|------|----------|----------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分 | 是 | 通用场景 |
| `programmatic` | 仅确定性断言 | 否 | 输出结构化、可机械验证 |
| `llm` | 仅 LLM 语义评分 | 是 | 输出需要语义判断 |

### 程序化断言类型

**Tier 1 — 确定性**（程序判定，零噪声）：

| 类型 | 说明 | 参数 |
|------|------|------|
| `FILE_EXISTS` | 文件存在 | `filename` |
| `FILE_CONTAINS` | 文件含子串 | `filename`, `pattern` |
| `CONTAINS_PATTERN` | 正则匹配 | `pattern`, `path`(可选) |
| `JSON_HAS_FIELD` | JSON 字段 | `filename`, `field`（支持 `a.b.c`） |
| `LINE_COUNT` | 行数范围 | `min`/`max`(至少一个), `path`(可选) |
| `NOT_CONTAINS` | 反向匹配 | `pattern`, `path`(可选) |
| `CUSTOM_SCRIPT` | 自定义脚本 | `script`, `args`(可选) |
| `CROSS_FILE_CONSISTENCY` | 检查相对 import | `include`(可选, glob) |
| `CODE_COMPILES` | 基础语法检查 | `include`(可选, glob) |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | `plugin_name` |
| `TOOL_CALLED` | 检查工具调用（看 tool_trace） | `tool_name`, `min_count`(默认 1) |
| `STEP_COUNT` | agentic 步数范围（看 num_turns） | `min`/`max`(至少一个) |

**Tier 3 — 语义**（LLM 判定，归 LLM grader 处理）：

| 类型 | 说明 |
|------|------|
| `LLM_GRADE` | LLM 语义评分 |
| `LLM_FACT` | LLM 事实核查 |
| `LLM_NOT_DELEGATE` | 检查未把任务不当委托 |

### 测试层级

| 层级 | 测试什么 | 需要什么 |
|------|----------|----------|
| **L1**（单元级） | 单个 Skill 的规则理解是否正确 | evals.json + expectations |
| **L2**（Mock 场景） | Skill 在有 fixture 的场景下能否正确产出文件 | evals.json + assertions + fixtures |

### 校准可信度与 eval 设计

`calibration.json` 的核心信号 `agreement_rate`（评分可信度）依赖 **程序化断言和 LLM 期望评同一项**（双判）：只有同一件事同时被 `assertions`（程序化）和 `expectations`（LLM 语义）覆盖，才能比较两者是否一致。

| eval 设计 | agreement_rate | calibration 可信度 |
|---|---|---|
| **双判**（同一项既有 assertion 又有 expectation） | 有数据（一致率） | 可作为强参考 |
| **无双判**（assertions 和 expectations 各评各的，无交集） | `null`（显示"—"，非"0%"） | 退化为基准值 + 弱信号，不宜作强参考 |

**建议**：对真正想验证的关键产出，在 `evals.json` 里**同时**写一条 `assertion`（可机械验证）和一条 `expectation`（语义描述）、两者指向同一件事，让校准有双判数据。例：验证"生成了排序脚本" → 既写 `{"type": "FILE_EXISTS", "params": {"filename": "sort.py"}}`，又写 expectation `"实现了排序算法"`。

> 注：不双判时 calibration 显式标 `agreement_rate: null` 并在 recommendations 提示“无双判数据”，不会假报“0% 一致”。

除 `grade`/`agreement_rate` 外，calibration.json 还携带三层诊断，报告直接渲染：`confidence_signals`（影响置信度的信号，如“6/6 项为单一来源评测 impact -0.1”，常驻有值，解释 `overall_confidence` 怎么来的）、`disagreements`（双判项里程序化与 LLM 打架的 expectation + 裁决策略 + 证据分析，仅双判时非空）、双判计数（`both_evaluated`/`programmatic_only_count`/`llm_only_count`，解释 `agreement_rate` 为何为 null）。

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
      ],
      "timeout": 600,
      "flags": []
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
| `expectations` | 否 | 语义预期语句列表（LLM 判） |
| `assertions` | 否 | 程序化断言列表（程序判） |
| `timeout` | 否 | per-eval 超时覆盖（秒）；不设则用 `--timeout` |
| `source_dir` | 否 | 从 skill 目录拷进工作区的源目录 |
| `flags` | 否 | 后处理标记（如 `degraded:no-llm`）；默认空 |

### grading.json

```json
{
  "expectations": [
    {"text": "The output includes X", "passed": true, "evidence": "Found in transcript Step 3",
     "source": "llm", "verdict": "PASS"}
  ],
  "summary": {
    "passed": 3, "failed": 1, "total": 4, "pass_rate": 0.75,
    "passed_programmatic": 2, "total_programmatic": 2, "pass_rate_programmatic": 1.0,
    "passed_llm": 1, "total_llm": 2, "pass_rate_llm": 0.5
  },
  "execution_metrics": {"tool_calls": {"Read": 3, "Write": 2}, "total_tool_calls": 5},
  "timing": {"total_tokens": 84852, "duration_ms": 23332, "total_duration_seconds": 23.3},
  "error": ""
}
```

每条 expectation 带 `verdict`（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`）与 `source`（`programmatic`/`llm`）。报告里 UNEVAL/TIMEOUT/ERROR 与 FAIL **区分显示**（?/⏱/!），不会被混成红 ✗——"没评出来/超时/出错"不是"失败"。

`summary` 的双率字段（`*_programmatic` / `*_llm`）按 expectation 的 `source` 分桶；某类无 expectation 时为 `null`（不报假 0）。

### benchmark.json

```json
{
  "metadata": {"skill_name": "my-skill", "timestamp": "2026-07-12T10:30:00Z", "runs_per_configuration": 3},
  "runs": [...],
  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.05},
      "pass_rate_programmatic": {"mean": 0.90, "stddev": 0.04},
      "pass_rate_llm": {"mean": 0.80, "stddev": 0.08}
    },
    "without_skill": { "...": "..." },
    "delta": {
      "pass_rate": "+0.50",
      "pass_rate_programmatic": "+0.27",
      "pass_rate_llm": "+0.12",
      "time_seconds": "+13.0",
      "tokens": "+1700"
    }
  },
  "comparisons": [
    {"winner": "with", "reasoning": "...", "rubric": {...}, "output_quality": {...}, "failed": false}
  ],
  "comparison_summary": {
    "total": 10, "a_wins": 6, "b_wins": 2, "ties": 1, "failed": 1,
    "valid": 9, "win_rate": 0.67, "label_a": "with", "label_b": "without"
  },
  "analysis": {...}
}
```

- 每个 config 的 `run_summary` 含 `pass_rate` / `pass_rate_programmatic` / `pass_rate_llm`（后两者可能为 `null`）+ `time_seconds` / `tokens`。
- `delta` 全为有符号字符串（`+0.27`），= `configs[0] - configs[1]`（候选 - 基线）；双率项可能为 `null`。
- `comparison_summary.win_rate` = `a_wins/valid`（tie-计入）；`gate` 用 tie-排除版自行算。

### gate.json（`gate` 命令产出）

```json
{
  "decision": "PASS",
  "exit_code": 0,
  "reason": "客观侧无回归(+0.27); 主观胜率 0.75 >= 0.50 (8 决定性)",
  "artifact": "output/iteration-1/benchmark.json",
  "candidate": "with_skill",
  "baseline": "without_skill",
  "thresholds": {"min_win_rate": 0.5, "min_valid": 3, "programmatic_guard": true},
  "axes": {
    "programmatic": {"verdict": "PASS", "delta": 0.27, "reason": "..."},
    "comparison": {"verdict": "PASS", "win_rate": 0.75, "decisive": 8, "a_wins": 6, "b_wins": 2, "ties": 1, "reason": "..."}
  },
  "warnings": []
}
```

### 输出文件说明

`run` 命令完成后，`output/iteration-1/` 目录：

| 文件 | 内容 |
|------|------|
| `benchmark.json` | 聚合结果（双率 delta + 盲比较 comparison_summary） |
| `report.html` | 自包含 HTML 报告（读 gate.json 显门禁徽章） |
| `gate.json` | `gate` 命令产出（非 run 产出）；判决 + 两轴明细 |
| `with_skill/eval-<id>/run-1/grading.json` | expectations + assertions 的 pass/fail（双率） |
| `with_skill/eval-<id>/run-1/compliance.json` | SKILL.md 指令遵循性（双率） |
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
│   ├── cli/                       # typer CLI 入口(12 个命令)
│   ├── models/                    # Pydantic 数据模型
│   ├── parser.py                  # SKILL.md 解析器
│   ├── config/                    # 配置驱动架构
│   ├── security/                  # 安全审查(4 扫描器)
│   ├── static_analysis/           # 静态分析(11 项检查)
│   ├── evals/                     # evals.json 加载/生成 + transcript + baseline
│   ├── execution/                 # Claude 执行 + A/B 测试 + run_paths(retry 索引)
│   ├── scoring/                   # 评分引擎(程序化 + LLM + 盲比较 + 分析)
│   ├── compliance/                # 遵循性引擎
│   ├── calibration/               # 评分校准
│   ├── audit/                     # 真实 transcript 对账(TranscriptAuditor)
│   ├── gating/                    # gate 判决纯函数(客观+主观双轴)
│   ├── reporting/                 # HTML 报告
│   └── batch/                     # 批量评测(DAG + 调度)
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
- `audit/` — 真实 transcript 对账
- `gating/` — post-hoc 通过/失败判决
- `batch/` — 批量评测（DAG）
- `config/` — 配置系统

---

## Exit Code

各命令语义不同，无全局统一含义：

### `run`

| Code | 含义 |
|------|------|
| 0 | 产数据成功（**不代表评测通过**——通过/失败由 `gate` 判） |
| 2 | 输入错误（SKILL.md 缺失、evals.json 格式错误） |
| 3 | 执行错误（claude CLI 未找到、超时） |

`--security-gate` 触发（BLOCKED/REVIEW_NEEDED）时退 1。

### `gate`

| Code | 含义 |
|------|------|
| 0 | PASS |
| 1 | FAIL（客观回归 或 主观不过阈值） |
| 2 | N/A（信号不足）或 使用错误（文件/解析错误）——靠 gate.json 是否存在区分 |

### 其他

| 命令 | 退 1 条件 |
|------|----------|
| `vet` | Verdict = BLOCKED |
| `analyze` | score < max_score |
| `batch` | 有 eval 失败 |
| `init`/`inspect`/`freeze`/`gate`/`report` | 2 = 输入错误 |

---

## 常见问题

### 安全审查需要安装额外的扫描器吗？

不需要。内置 2 个原生扫描器（secrets-scan、structure-check），开箱即用。aguara / skill-scanner 是可选增强。

### `run` 退 1 了，是评测失败吗？

不一定。`run` 现在是**纯数据生产者**——退 1 只在 `--security-gate` 触发时；负 delta 不再导致退 1（旧逻辑已退役）。评测通过/失败改由独立 `gate` 命令判：

```bash
skill-eval run v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/
skill-eval gate out/iteration-1/benchmark.json --min-win-rate 0.5
echo $?   # 0=PASS / 1=FAIL / 2=N/A
```

### 静态分析 / inspect 需要 Claude CLI 吗？

不需要。`analyze`、`inspect` 都是零 API 成本、<1s 完成。

### evals.json 放在哪里？

推荐 `<skill_path>/evals/evals.json`。`run` 会自动查找，也可用 `--evals` 手动指定。跨 run 想固定同一份题，用 `freeze` 冻结成 baseline.json 并 commit。

### 如何在 CI 中使用？

```bash
# 1. 静态体检 + 安全审查
skill-eval analyze path/to/skill/ --format json -o scorecard.json
skill-eval vet    path/to/skill/ --format json

# 2. 冻结基线题集(commit 它,保证同卷)
skill-eval freeze path/to/evals.json --name baseline

# 3. A/B 跑(候选 v2 vs 基线 v1)
skill-eval run v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/

# 4. 出判决 → exit code 驱动 CI check
skill-eval gate out/iteration-1/benchmark.json --min-win-rate 0.5
# exit: 0=PASS(放行) / 1=FAIL(卡合并) / 2=N/A(信号不足,消费方定)
```

门禁阈值与 CI 落点（分支保护、check 名、N/A 是否阻塞）归消费方配置——skill-eval 只提供机制（三态 exit + gate.json）。
