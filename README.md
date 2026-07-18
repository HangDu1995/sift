# Skill-Eval

自动评测 [Claude Code Skill](https://github.com/anthropics/skill-creator) 的 CLI——三层质量保障（评分 / 遵循性 / 校准）+ 盲比较 + post-hoc 门禁，CI 就绪。

与 skill-creator 对比，skill-eval 的核心差异：

- **程序化断言** — 确定性验证（文件是否存在、内容是否匹配），零 LLM 成本，不可作弊
- **盲比较** — 同一 LLM 盲评两份产出（with/without 或 v1/v2），相对判断消阅卷噪声
- **遵循性检查** — 验证 Claude 是否真的按 SKILL.md 指令执行了，而不只是输出看起来对
- **校准** — 单次执行的结果×过程 coherence 判决：打分（结果）与遵循性（过程）协不协调，给出 5 态判决（可信/高分存疑/可修复/技能失效/过程未知）
- **双率** — 客观（programmatic）与主观（llm）分开报，干净信号不被 LLM 噪声污染
- **post-hoc 门禁** — `gate` 命令对 benchmark 出 PASS/FAIL 判决，exit code 直接驱动 CI
- **CI 就绪** — `--format json` + exit code，合入 CI pipeline

> 完整命令参数、字段定义、JSON schema 见 [参考手册](docs/skill-eval-reference.md)。

---

## 安装

### 一键安装

```bash
git clone https://github.com/fuxi-ailabs/skill-eval.git
cd skill-eval
bash install.sh     # macOS / Linux
# install.bat       # Windows
```

这会完成三件事：
1. 安装 skill-eval CLI(系统 Python 受 PEP 668 限制时——Debian/Ubuntu/WSL2——自动建隔离 `.venv`,并把 `skill-eval` 软链到 `~/.local/bin`)
2. 将 skill-eval-guide skill 安装到 Claude Code (`~/.claude/skills/`) 和 opencode (`~/.config/opencode/skills/`)
3. 验证环境

### 手动安装

```bash
# 安装 CLI
git clone https://github.com/fuxi-ailabs/skill-eval.git
cd skill-eval
pip install -e .
# Debian/Ubuntu/WSL2(PEP 668)报 externally-managed 时,改用 venv:
#   python3 -m venv .venv && .venv/bin/pip install -e . && ln -sf "$PWD/.venv/bin/skill-eval" ~/.local/bin/skill-eval

# 安装 skill-eval-guide（可选，让 Claude 自动选择评测命令）
# macOS / Linux:
cp tools/skills/skill-eval-guide/SKILL.md ~/.claude/skills/skill-eval-guide/SKILL.md
cp tools/skills/skill-eval-guide/SKILL.md ~/.config/opencode/skills/skill-eval-guide/SKILL.md

# Windows:
copy tools\skills\skill-eval-guide\SKILL.md %USERPROFILE%\.claude\skills\skill-eval-guide\SKILL.md
copy tools\skills\skill-eval-guide\SKILL.md %USERPROFILE%\.config\opencode\skills\skill-eval-guide\SKILL.md
```

前置条件：Python 3.10+，Claude Code CLI >= 2.1.0（仅动态评测需要）。

---

## 快速开始

```bash
# 1. 静态体检 + 安全审查（零 API 成本）
skill-eval analyze path/to/skill/
skill-eval vet    path/to/skill/

# 2. 生成 evals.json（LLM 自动出题 + 推导程序化断言）
skill-eval init path/to/skill/

# 3. A/B 评测（with/without 对照，或 v1/v2 双 skill）
skill-eval run path/to/skill/ --runs 3
skill-eval run v2/ --skill-b v1/ --evals baseline.json   # 回归对照：候选 v2 vs 基线 v1

# 4. 看报告
skill-eval report output/iteration-1/ --open

# 5. （CI）对结果出门禁判决
skill-eval gate output/iteration-1/benchmark.json --min-win-rate 0.5
echo $?    # 0=PASS / 1=FAIL / 2=N/A
```

---

## 命令

### `vet` — 安全审查

4 扫描器（2 内置 + 2 可选外部），检测密钥泄露、prompt 注入、危险命令。判定 SAFE / REVIEW_NEEDED / BLOCKED。

```bash
skill-eval vet path/to/skill/
skill-eval vet path/to/skill/ --format json --skip-external
```

### `analyze` — 静态分析

11 项检查：描述质量、命名规范、文件行数、示例覆盖、反捏造等。零 API 成本。

```bash
skill-eval analyze path/to/skill/
```

### `init` — 生成评测用例

LLM 自动生成 L1/L2 测试用例，自动推导程序化断言。也可从真实会话 transcript 挖 eval（`--from-transcript`）。

```bash
skill-eval init path/to/skill/
skill-eval init path/to/skill/ --from-transcript session.jsonl   # 从真实会话挖题
```

### `add` — 追加评测用例

LLM 辅助或手动添加 eval 到已有的 evals.json。

```bash
# LLM 模式：一句话想法 → 完整 eval
skill-eval add pdf/ --idea "test encrypted PDF decryption" --level L2

# 手动模式
skill-eval add pdf/ --prompt "Decrypt file.pdf..." --expectation "produces output.pdf" --level L2
```

### `inspect` — 题集体检

对已有 evals.json 做静态体检（完整性 / 稳定性 / 问题 flag），不执行、不下判决——帮人决定是否冻结成基线。

```bash
skill-eval inspect path/to/evals.json --json
```

### `freeze` — 冻结基线

把 evals.json 冻结成命名基线快照 + 溯源 sidecar，跨 run / CI 复用同一份题。

```bash
skill-eval freeze path/to/evals.json --name baseline
```

### `run` — A/B 评测

两种模式：默认 with_skill vs without_skill 对照（或 v1/v2 双 skill），或路径 1 真任务模式（`--task` 直接跑真实任务）。产出 benchmark.json（含双率 delta + 盲比较聚合）。

```bash
skill-eval run path/to/skill/
skill-eval run path/to/skill/ --runs 3 --model claude-sonnet-5
skill-eval run path/to/skill/ --scoring programmatic   # 仅程序化，零 LLM 评分成本
skill-eval run v2/ --skill-b v1/                        # v1/v2 双 skill 回归对照
skill-eval run path/to/skill/ --task "重构 X 模块" --cwd ./repo   # 路径 1 真任务模式
```

关键参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--runs` | 1 | 每次评测运行次数（推荐 3 抹平答题波动） |
| `--scoring` | hybrid | hybrid / programmatic / llm |
| `--skill-b` | — | v1/v2 模式：第二个 skill 路径（两边各注入各自 skill） |
| `--task` / `--cwd` | — | 路径 1 真任务模式：直接跑真实任务（不读 evals.json） |
| `--no-compliance` | auto | 跳过遵循性检查（`--runs<3` 时默认开） |
| `--security-gate` | off | 安全审查返回 BLOCKED/REVIEW_NEEDED 时判失败 |
| `--model` | — | claude -p 模型（也控制盲比较/赛后分析是否执行） |

> `run` 是纯数据生产者——exit 0 只代表产数据成功，**通过/失败判决由 `gate` 出**。

### `gate` — 通过/失败判决

对已有 benchmark.json 出 PASS/FAIL/N/A 判决。post-hoc 纯函数（毫秒级，改阈值不重跑 claude）。

```bash
skill-eval gate output/iteration-1/benchmark.json                     # 仅客观回归守卫
skill-eval gate output/iteration-1/benchmark.json --min-win-rate 0.5  # 加主观盲评阈值
```

两轴：客观侧 programmatic 回归守卫（默认常开）+ 主观侧盲比较胜率（`--min-win-rate` opt-in）。判决写 gate.json，exit `0=PASS / 1=FAIL / 2=N/A`。

### `audit` — 真实 transcript 对账

把真实会话 transcript 与 SKILL.md 对账，判指令遵循性。不 re-run，直接对账历史执行（支持多 transcript 聚合）。

```bash
skill-eval audit path/to/skill/ --transcript run-1/transcript.jsonl
```

### `report` — HTML 报告

```bash
skill-eval report output/iteration-1/ --open
```

读 `gate.json`（若存在）在标题旁渲染门禁徽章。

### `batch` / `batch-report` — 批量评测与报告

多 skill 按 DAG 拓扑顺序并行执行；`batch-report` 生成多 skill 对比 HTML。

```bash
skill-eval batch path/to/skills-dir/
skill-eval batch-report batch-workspace/ --open
```

---

## 评测体系

### 三层质量保障

| 引擎 | 问题 | 产出 |
|------|------|------|
| **Scoring** | 输出是否正确？ | grading.json |
| **Compliance** | 过程是否遵循 SKILL.md？ | compliance.json |
| **Calibration** | 结果×过程是否协调？ | calibration.json |

三个维度独立运行、交叉验证。输出对但过程未遵循 → 高分不可信；输出错但过程遵循 → 逻辑没问题，改指令即可。

### 双率与双主位

每条 expectation/assertion 带 `source`，评分与遵循性都按 source 拆**双率**：

- `*_programmatic`（客观，程序判定）——稳，强信
- `*_llm`（主观，LLM 判定）——抖，看方向

fixture-eval 下的双主位分工：**客观信 `delta.pass_rate_programmatic`**（候选 - 基线，噪声抵消到 0），**主观信盲比较胜率**。

### 盲比较（BlindComparator）

同一 LLM 同一次盲评两份产出（不知哪个是候选），判谁更好 + 打 rubric 分。位置随机化，相对判断消阅卷噪声，比 per-run 独立打分稳。聚合为 `comparison_summary`（胜/负/平/失败 + 胜率）。仅服务主观题，programmatic-only 下自动跳过。

### 断言类型

| 层级 | 判定方式 | 示例 |
|------|---------|------|
| **Tier 1** | 程序化（确定性） | FILE_EXISTS, FILE_CONTAINS, CONTAINS_PATTERN, NOT_CONTAINS, JSON_HAS_FIELD, LINE_COUNT, TOOL_CALLED, STEP_COUNT, CUSTOM_SCRIPT, CUSTOM_PLUGIN ... |
| **Tier 3** | LLM 语义判分 | LLM_GRADE, LLM_FACT, LLM_NOT_DELEGATE |

### 遵循性引擎

从 SKILL.md 自动提取 6 类指令，程序化 + LLM 双模检查（也拆双率：`compliance_rate_programmatic` / `compliance_rate_llm`）：

| 指令类型 | 检查方式 | 示例 |
|---------|---------|------|
| HEADING_STEP | LLM_JUDGE | "Step 1: Read input data" |
| TOOL_REQUIREMENT | TOOL_CALL_CHECK | "run scripts/validate.py" |
| CONSTRAINT | LLM_JUDGE | "NEVER fabricate data" |
| ANTI_FABRICATION | LLM_JUDGE | 防捏造规则 |
| OUTPUT_FORMAT | PATTERN_MATCH | 输出格式模板 |
| REFERENCE_USAGE | TOOL_CALL_CHECK | "see references/schema.md" |

### 校准引擎

单次 with_skill 执行的**结果×过程 coherence 判决**——打分（结果面）与遵循性（过程面）协不协调。结果轴：优先押**客观** `pass_rate_programmatic ≥ 0.8`（纯主观题无客观锚回退主观率）；过程轴：遵循性里有无 prohibition/output **硬类**违规（step/conditional 的 FAIL 只记软注、不翻判决，避免 LLM 方差污染）。两轴组合出 5 态：

| verdict | 结果 | 过程 | 含义 |
|---------|------|------|------|
| 可信 TRUSTED | 对 | 对 | 未发现过程问题 |
| 高分存疑 HIGH_SCORE_SUSPECT | 对 | 错 | 输出对了但过程违反硬性规则，建议核对（列 evidence） |
| 可修复 FIXABLE | 错 | 对 | 输出未达标，过程无明显违规 |
| 技能失效 SKILL_FAILED | 错 | 错 | 输出未达标且过程违反硬性规则 |
| 过程未知 PROCESS_UNKNOWN | — | 无可对账 | compliance 缺失或无可判定（如纯推理题） |

`calibration.json` 字段：`verdict` / `result_pass` / `process_status` / `hard_violations`（category+instruction+evidence）/ `soft_notes`（step/conditional FAIL 计数）/ `scoring_rate` / `scoring_rate_basis`（programmatic 押客观 | mixed 无客观锚回退主观）/ `scoring_rate_programmatic` / `scoring_rate_llm` / `compliance_rate`。gate 不读 calibration（纯显示层）；仅 with_skill 产（without 无 compliance → 不产）。

---

## 评测产物

```
output/iteration-1/
├── benchmark.json          # 聚合:双率 delta + comparisons + comparison_summary + analysis
├── report.html             # 自包含 HTML 报告(读 gate.json 显门禁徽章;run 末尾自动产)
├── gate.json               # gate 命令产出(判决 + 两轴明细;非 run 产出)
├── with_skill/             # v1/v2 模式下换成各自标签名
│   └── eval-<id>/run-<n>/
│       ├── outputs/        # Claude 产生的文件(skill references 已自动拷入)
│       ├── grading.json    # 评分(expectations + assertions pass/fail,双率)
│       ├── compliance.json # 遵循性检查(双率;仅 with_skill)
│       └── calibration.json # 结果×过程 coherence 判决(仅 with_skill)
└── without_skill/
    └── eval-<id>/run-<n>/  # 同上(无 compliance / 无校准)
```

---

## CI 集成

```bash
# 1. 静态体检 + 安全审查
skill-eval analyze path/to/skill/ --format json -o scorecard.json
skill-eval vet    path/to/skill/ --format json

# 2. 冻结基线题集(commit 它,保证跨 run 同卷)
skill-eval freeze path/to/evals.json --name baseline

# 3. A/B 跑(候选 v2 vs 基线 v1)
skill-eval run v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/

# 4. 出判决 → exit code 驱动 CI check
skill-eval gate out/iteration-1/benchmark.json --min-win-rate 0.5
```

Exit code（各命令语义不同）：

| 命令 | 0 | 1 | 2 | 3 |
|------|---|---|---|---|
| `gate` | PASS | FAIL | N/A 或 输入错误 | — |
| `run` | 产数据成功 | （`--security-gate` 触发） | 输入错误 | 执行错误 |
| `vet` | SAFE/REVIEW | BLOCKED | 输入错误 | — |
| `analyze` | score 满 | score 未满 | 输入错误 | — |

门禁阈值与 CI 落点（分支保护、check 名、N/A 是否阻塞）归消费方配置——skill-eval 只提供机制（三态 exit + gate.json）。

---

## 测试

```bash
pytest tests/ -v
```
