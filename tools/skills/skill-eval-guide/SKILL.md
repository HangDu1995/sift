---
name: skill-eval-guide
description: >
  Use this skill whenever you need to evaluate, test, benchmark, or quality-check any skill (SKILL.md).
  Trigger on: "evaluate", "test", "benchmark", "跑评测", "评测", "跑一下", "测试", "全面测评",
  "跑所有测评", "验证skill", "检查skill质量", "安全审查", "vet", "静态分析", "analyze",
  "回归", "regression", "对照", "门禁", "gate", "遵循性", "对账", "audit", "transcript",
  "冻结基线", "freeze", "题集体检", "inspect", "batch", "跑batch", "多skill评测",
  "report", "报告", "init", "初始化evals", "add", "加eval".
  This skill provides EXACT commands to run for every skill-eval operation.
type: guide
domain: quality-assurance
---

# Skill Eval Guide — Agent 操作手册

用 `skill-eval` CLI 对任意 skill 执行评测。三层质量保障（评分 / 遵循性 / 校准）+ 盲比较 + post-hoc 门禁。

**12 个命令**（`skill-eval --help` 权威）：

```
vet  analyze  init  add  run  report  batch-report  batch
audit  inspect  freeze  gate
```

## 环境安装

### 如果 skill-eval 未安装

向用户索要 GitHub URL，然后 clone 并安装。**如果不能确定 URL，必须先问用户。**

```bash
# 用户提供 URL
git clone <用户提供的URL> /tmp/skill-eval
cd /tmp/skill-eval
bash install.sh        # 一键：装 CLI + 本 guide skill + 验证环境
# 或手动：pip install -e .
skill-eval --help      # 验证
```

### 环境检查

```bash
claude --version          # 动态评测（run/audit/init-LLM）依赖 claude -p；analyze/inspect/freeze/vet 不需要
skill-eval --version      # 评测工具
python3 --version || python --version  # 必须：>= 3.10
```

---

## 关键原则：缺少任何输入必须先问用户

**绝对不要假设或猜测。** 以下情况必须停下追问：

| 缺什么 | 问用户什么 |
|---|---|
| skill-eval 未安装 | "请提供 skill-eval 的 GitHub URL" |
| 目标 skill 路径不知道 | "请提供要评测的 skill 路径" |
| skill 目录下没有 evals.json | "没有 evals.json，要我运行 `skill-eval init` 生成吗？" |
| evals.json 质量差（>50% 模糊 expectation） | "当前 evals 质量较差（X/Y 条模糊），建议重新生成。要我运行 `skill-eval init` 吗？" |
| 做 v1/v2 回归对照但没给第二个 skill | "回归对照需要第二个 skill 路径（`--skill-b`），请提供" |
| 做门禁判决但没有 benchmark.json | "`gate` 需要先跑过 `run` 产出 benchmark.json" |
| 做遵循性对账但没有 transcript | "`audit` 需要真实会话 transcript（cc `.jsonl` 或 opencode session `.json`），请提供" |
| 做 batch 评测但没有 skills 目录或 manifest.json | "batch 评测需要一个包含多个 skill 的目录或 manifest.json，请提供" |

> 注意位置参数差异：`inspect` / `freeze` 吃 **evals.json 路径**，`audit` 吃 **skill 目录 + `--transcript`**，其余多数吃 **skill 目录**。给错会被拒。

---

## 命令意图映射

根据用户的话选择正确的命令：

| 用户说了什么 | 命令 | 说明 |
|---|---|---|
| "安全审查" / "vet" / "安全检查" / "密钥泄露" | `vet` | 4 扫描器安全审查 |
| "静态分析" / "analyze" / "评分卡" / "打分" / "体检" | `analyze` | 11 项结构化评分（零 API 成本） |
| "生成 evals" / "init evals" / "初始化" / "出题" | `init` | LLM 自动生成 evals.json |
| "从会话挖题" / "transcript 出 eval" | `init --from-transcript` | 从真实 session 挖 eval |
| "加 eval" / "写测试用例" / "add" | `add` | 追加单条 eval 到 evals.json |
| "题集体检" / "inspect" / "evals 质量" | `inspect` | 对 evals.json 静态体检（不执行） |
| "冻结基线" / "freeze" / "固定题集" | `freeze` | evals.json → 命名 baseline 快照 |
| "跑测试" / "A/B 测试" / "run" / "评测" | `run` | with/without 或 v1/v2 对照 |
| "真任务" / "跑真实任务" / "live" | `run --task --cwd` | 路径1 真任务模式（不读 evals.json） |
| "门禁" / "判决" / "通过没" / "gate" | `gate` | 对 benchmark.json 出 PASS/FAIL/N/A/UNCLEAR |
| "遵循性对账" / "audit" / "按指令做了没" | `audit` | transcript × SKILL.md 对账 |
| "生成报告" / "report" / "报告" | `report` | 单 skill HTML 报告 |
| "批量评测" / "batch" / "多 skill" | `batch` + `batch-report` | 多 skill（unit 层）并行 |
| "跑所有测评" / "全面测评" | `vet` → `analyze` → `run` → `gate` → `report` | 全流程串跑 |

---

## 评测模型（先理解再跑）

### 三层并列独立信号（不合成单一总分）

| 引擎 | 问题 | 产出 |
|------|------|------|
| **Scoring** | 输出是否正确？ | grading.json |
| **Compliance** | 过程是否遵循 SKILL.md？ | compliance.json |
| **Calibration** | 结果×过程是否协调？ | calibration.json |

输出对但过程未遵循 → 高分不可信；输出错但过程遵循 → 逻辑没问题，改指令即可。

### 双率 + 双主位

每条 expectation/assertion 带 `source`，评分按 source 拆**双率**（遵循性不分双率，见下 audit 段）：

- `*_programmatic`（客观，程序判定）——稳，强信
- `*_llm`（主观，LLM 判定）——抖，看方向

fixture-eval 下的**双主位**分工：**客观侧信 `delta.pass_rate_programmatic`**（候选 - 基线，A/B 噪声抵消到 0），**主观侧信盲比较胜率**。

### 盲比较（BlindComparator）

同一 LLM 同一次盲评两份产出（不知哪个是候选），判谁更好 + 打 rubric 分。位置随机化，相对判断消阅卷噪声。聚合为 `comparison_summary`（胜/负/平/失败 + 胜率）。仅服务主观题，`programmatic`-only 下自动跳过。

> `run` 是纯数据生产者——exit 0 只代表产数据成功，**通过/失败判决由 `gate` 出**。

---

## 命令速查

### `vet` — 安全审查

```bash
skill-eval vet /path/to/skill                   # 表格（默认不落盘，-o 写文件）
skill-eval vet /path/to/skill --format json     # JSON 输出
skill-eval vet /path/to/skill --skip-external   # 跳过外部扫描器
skill-eval vet /path/to/skill -o report.json    # 指定输出路径
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--format` | table | table / json |
| `--output` / `-o` | 无（仅终端） | 输出文件（不给则不落盘） |
| `--skip-external` | 关 | 跳过外部扫描器（aguara、skill-scanner） |

4 扫描器：`aguara`（可选，Prompt 注入）、`skill-scanner`（可选，恶意模式/CVE）、`secrets-scan`（内置，密钥/Token/Base64）、`structure-check`（内置，SKILL.md 结构 + 危险命令）。判定 **SAFE / REVIEW_NEEDED / BLOCKED**，BLOCKED 退 1。

### `analyze` — 静态分析

```bash
skill-eval analyze /path/to/skill                       # 表格（默认不落盘，-o 写文件）
skill-eval analyze /path/to/skill --format json -o scorecard.json
```

11 项检查（描述质量 / 命名规范 / 行数 / 示例 / 反捏造等），**零 API 成本，<1s**。输出 scorecard（`score / max_score`）。`score < max_score` → exit 1。

### `init` — 生成 evals.json

```bash
skill-eval init /path/to/skill                                  # LLM 生成（默认）
skill-eval init /path/to/skill --num-evals 8                    # 限制数量
skill-eval init /path/to/skill --from-transcript session.jsonl  # 从真实会话挖 eval
skill-eval init /path/to/skill --no-generate-prompts            # 不调 LLM，走模板
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--output` / `-o` | skill 目录 | evals.json 输出目录 |
| `--generate-prompts` / `--no-generate-prompts` | 开 | 是否用 LLM 生成 L1 eval |
| `--num-evals` | 无 | 最多生成的 eval 数 |
| `--from-transcript` | 无 | 从真实 session 挖 eval（加料 SKILL.md，非替代） |
| `--kind` | (无) | `skill`|`agent`：只挖本 skill/agent 被调用/被 dispatch 期间段（同 audit --kind）；不传 + 检测到多 skill → 警告 |
| `--model` | 默认 | LLM 模型 |
| `--timeout` | 600 | LLM 调用超时秒数（mining 是单次大 prompt；撞超时 → degraded fallback） |

`--from-transcript` 解析 session 重建 input_files/commands/produced_files，生成带结构化 assertions 的 eval。LLM 失败降级为 fallback eval 会带 `degraded:*` flag 并在终端告警（不再静默）——拿到 degraded eval 要重跑。

### `add` — 追加评测用例

```bash
# LLM 模式：一句话 → 完整 eval
skill-eval add /path/to/skill --idea "加密PDF用错密码应失败" --level L2

# 手动模式
skill-eval add /path/to/skill --prompt "Decrypt file.pdf..." --expectation "produces output.pdf" --level L2

# 预览不保存
skill-eval add /path/to/skill --idea "..." --dry-run
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--idea` | 无 | 自然语言描述，LLM 生成完整 eval case |
| `--prompt` | 无 | 手动指定 eval prompt |
| `--expectation` | 无 | 手动指定 expectation（可重复） |
| `--level` | L1 | L1 / L2 |
| `--dry-run` | false | 仅预览 |

`--idea` 与 `--prompt` 必须给其一。`--idea` 模式自动分配 id、推导 assertions 和 fixtures。

### `inspect` — 题集体检

```bash
skill-eval inspect /path/to/evals.json            # 表格
skill-eval inspect /path/to/evals.json --json     # 机器可读
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--json` | 关 | 输出 JSON |

**位置参数是 evals.json 路径**（不是 skill 目录）。对 evals.json 做静态体检：完整性 / 稳定性（programmatic vs LLM 占比）/ 问题 flag。**不执行、不调 LLM、不下判决**——帮人决定是否 freeze。

### `freeze` — 冻结基线

```bash
skill-eval freeze /path/to/evals.json                              # → baseline.json + baseline.meta.json
skill-eval freeze /path/to/evals.json --name v1 --skill-version 1.2
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--name` | baseline | 基线名（→ `<name>.json` + `<name>.meta.json`） |
| `--out` | evals.json 同目录 | 输出目录 |
| `--skill-version` | 无 | 目标 skill 版本（记入 meta） |

把 evals.json 冻结成命名快照 + 溯源 sidecar，跨 run / CI 复用同一份题。同名覆盖（幂等）。**位置参数是 evals.json 路径**。

### `run` — A/B 评测

两种模式：默认 A/B（同一 skill 开/关对照，或 v1/v2 双 skill），或路径1 真任务模式（`--task`）。

```bash
skill-eval run /path/to/skill --runs 3                            # with/without 对照
skill-eval run v2/ --skill-b v1/ --evals baseline.json --runs 3   # v1/v2 回归对照
skill-eval run /path/to/skill --scoring programmatic              # 仅程序化，零 LLM 评分成本
skill-eval run /path/to/skill --task "重构 X 模块" --cwd ./repo   # 路径1 真任务模式
```

关键参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--evals` | 自动查找 | evals.json 路径 |
| `--runs` | 1 | 每配置运行次数（推荐 3 抹平波动） |
| `--scoring` | hybrid | hybrid / programmatic / llm |
| `--skill-b` | — | v1/v2 模式：第二个 skill 路径（两边各注入各自 skill） |
| `--label-a` / `--label-b` | 目录名 | v1/v2 模式两侧显示标签 |
| `--task` / `--cwd` | — | **路径1 真任务模式**：直接跑真实任务（不读 evals.json），`--cwd` 拷贝隔离 |
| `--no-compliance` / `--compliance` | 自动 | 跳过/强制遵循性（`--runs<3` 默认跳） |
| `--security-gate` | 关 | 安全审查返回 BLOCKED/REVIEW_NEEDED 时判失败 |
| `--no-static` / `--no-vet` / `--no-report` | 都产 | 跳过末尾自动产的静态评分卡 / 安全审查 / HTML 报告（默认都产，写进 iteration-1/；report.html 聚合 benchmark/评分卡/安全） |
| `--model` | — | claude -p 模型（也控制盲比较/赛后分析是否执行） |
| `--timeout` / `--max-turns` / `--workers` / `--max-retries` | 300/10/4/2 | 执行控制 |

- **默认 A/B**：位置参数 skill = 候选（side A）。不给 `--skill-b` → with/without 对照；给 `--skill-b <dir>` → v1/v2 双 skill。
- **末尾自动产评分卡 + 安全审查 + HTML 报告**：A/B 模式跑完后，自动把 `scorecard.json`（静态 11 项）+ `vet-report.json`（原生安全扫描）+ `report.html`（聚合 benchmark/评分卡/安全的自包含 HTML）写进 `iteration-1/`，跑完即得报告（不用再单独 `skill-eval report`；该命令仍可用于 `--previous` 基线对照 / `--open` / 重生成）。三者秒级零 API 成本、各自失败不阻断 run；`--no-static` / `--no-vet` / `--no-report` 跳过。**仅 A/B 模式**——真任务模式（`--task`）无 iteration-1/，不产。
- **路径1 真任务**（给 `--task`）：忽略 evals.json，在 `--cwd`（拷贝隔离）跑真任务，产 transcript 喂 `audit`，每 run 打印 operational 摘要。**执行器**：默认 `cc`（产 `run-*/transcript.jsonl`）；`--executor opencode` 改用 opencode（产 `run-*/transcript.json`，需 opencode CLI 在 PATH，模型在 `opencode.json` 配，省略 `--model`）；`--executor cc,opencode` 两执行器各跑 `--runs` 次。
- **退出码**：`run` 是纯数据生产者——0 = 产数据成功（**不代表通过**），2 = 输入错误，3 = 执行错误。**通过/失败由 `gate` 判**。`--security-gate` 触发时退 1。

### `gate` — 通过/失败判决

对已有 benchmark.json 出判决。**post-hoc 纯函数**——不重跑 claude、毫秒级、可反复改阈值重判。

```bash
skill-eval gate out/iteration-1/benchmark.json                        # 仅客观回归守卫
skill-eval gate out/iteration-1/benchmark.json --min-win-rate 0.5     # 加主观盲评阈值
skill-eval gate out/iteration-1/benchmark.json --no-programmatic-guard --min-win-rate 0.6
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--min-win-rate` | 无 | 主观 gate 阈值（候选 tie-**排除**胜率）；不给则只走客观守卫 |
| `--min-valid` | 3 | 主观 gate 最少决定性比较数 |
| `--no-programmatic-guard` | 关 | 关掉客观回归守卫 |
| `--candidate` | 自动 | 候选侧名 |
| `--out` / `-o` | benchmark 同目录 | gate.json 输出路径 |

**两轴**（双主位）：客观侧 `delta.pass_rate_programmatic < 0` → FAIL；主观侧 tie-排除胜率 `< 阈值` 且样本足 → FAIL。AND——任一适用轴 FAIL → FAIL；有适用轴但信号不足（判不准/样本不足）→ UNCLEAR；无适用轴 → N/A（不适用）；否则 PASS。写 `gate.json`，退 `0=PASS / 1=FAIL / 2=N/A 或 UNCLEAR`（N/A 与 UNCLEAR 都 exit 2，消费方定）。

### `audit` — 真实 transcript 对账

把真实会话 transcript 与 SKILL.md 对账，判指令遵循性。**不 re-run**，直接对账历史执行。每条 FAIL 自动标**疑似工具噪声 / skill 问题 / 待确认** + 理由（`noise_label` 徽章；判别法见 handbook §7.8）——fixture 下 FAIL 常混工具噪声（extractor 误提资源描述当步骤、evidence 自相矛盾），别据噪声误改 skill。

> 判定要逐批调 LLM（每批约 40–257s），跑期间**实时打印进度**：进每个 transcript 打头（`对账 transcript i/N: <name>`），每个 LLM 批前后各一行——`[条件性/禁令/步骤] 判定 N 条…` → `[条件性/禁令/步骤] 完成 ⏱ Xs`（类别名打头；>10 条分批才显 `批 k/总`）。两种失败（调用失败 / 0 条解析成功）都先自动重试最多 2 次再兜底 UNRESOLVED（没判成），重试打 `重试 n/2…`、耗尽打 `重试耗尽`。三个 LLM 类别判定器并发跑（max_workers=3），约 2× 提速，进度行交错但每行 `[类别]` 打头、一眼分清。看到 `[条件性/禁令/步骤] …` 滚动就是在跑。

```bash
skill-eval audit /path/to/skill/ --transcript run-1/transcript.jsonl -o audit-out/
skill-eval audit /path/to/skill/ --transcript a.jsonl --transcript b.jsonl --format json --open
# transcript 里调了多个 skill,只对账本 skill 被调用的段:
skill-eval audit /path/to/skill/ --transcript big-multi-skill.jsonl --kind skill
# opencode 跑出的 session(`opencode export <sessionID>` 导出,顶层 info/messages、parts 嵌在 message 里):
skill-eval audit /path/to/skill/ --transcript opencode-session.json
```

> **`--transcript` 吃两种格式(按后缀自动分派)**：cc 会话 `.jsonl`（逐行 JSON 记录，含 `Skill`/`Read`/`Write`/`Bash` tool_use）；opencode session `.json`（`opencode export <sessionID>` 的产出，顶层 `info`/`messages`，parts 嵌在 message 里、role/agent 在 `message.info`）。两者重建成同一 `SessionDigest` 再对账(经同一条 canonical 重建路径:归一到 cc-record 形 → 共享重建器,三源一致,见 handbook §7.7)。生产导出：`opencode session list` 找 id → `opencode export <id> > session.json`（`--sanitize` 可脱敏分享）。**拿到一个 opencode `.db` 文件**(别处拷来的库):直接 `audit --db handed.db` —— 内部自动 list/export/audit(单 session 自动选,多个列出让你挑;底层 = XDG 隔离 + `opencode export`,完整见 reference audit 段)。run 产的 transcript 默认 cc 格式（`.jsonl`）；给 `--executor opencode` 时产 opencode 格式（`.json`）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--transcript` | **必填** | 真实会话 transcript（cc `.jsonl` 或 opencode session `.json`，按格式自动分派；**可重复**，多 transcript 聚合） |
| `--kind` | (无) | `skill`|`agent`：切本 skill/agent 被调用/被 dispatch 期间段（`skill` 按 `Skill` 调用切；`agent` 按 `agent` 归属切；opencode 单锚点按 per-message `agent` 切） |
| `--output` / `-o` | ./output | 输出目录 |
| `--model` | 默认 | 语义判定 LLM |
| `--format` | table | table / json |
| `--open` | 关 | 生成后浏览器打开 audit-report.html |

> **`--kind skill` 归段 scope-aware（cc）**：每个 `Skill` 调用起一段、归该 skill 到下一次调用；子 agent（Agent 工具）是独立作用域（记录带 `parent_tool_use_id`），子 agent 里用的 skill 切到 Agent 返回、不污染主 agent。target 调多次 → 多段 → 多 transcript 聚合。**两限制**：① skill 无"返回"信号，"调 A→B→不重调 A 接着干 A"那段归 B（不可测）；② 委派子 skill 折进主 skill（inclusive）需声明、暂不支持。该 skill 没在 transcript 里被调过 → exit 2。
>
> **opencode 单锚点（`--kind` 对它无意义）**：opencode 无 `Skill` tool_use，skill = per-message `agent` 字段；按**连续同 agent 的 message** 切段（每次 agent 切换断段）。比 cc 简单但**扁平**——无 cc 的父子作用域（opencode 子 agent / `task` 工具就是另一段 agent run，不像 cc 能让主 span 跨子 agent 不断）。同一段切出后落盘成子 `.json`、再走 rebuild 对账。

判定四类（条件性 / 禁令 / 步骤 / 输出格式），每条出**五态 verdict** + **method** 标（`PROGRAMMATIC` 程序对账 / `LLM` 语义判定）：`PASS` / `FAIL` / `N/A`（不适用，场景不触发）/ `UNRESOLVED`（没判成，LLM 失败/解析失败/未提及，可重跑）/ `INDETERMINATE`（判不准，证据真模糊，重跑无益）；输出格式是确定性子串匹配（`PROGRAMMATIC`，不调 LLM，只 PASS/FAIL）。**禁令判的角度反着来**——主动找违规、不判“遵守没”（如 `NEVER ...` / `不得编造`）。没判成报 UNRESOLVED + “建议重跑”警告、判不准报 INDETERMINATE + 占比告警，都不保守 FAIL（各类判法详表见 reference 的 audit 段）。产出 `audit-report.json` + `audit-report.html`（HTML always 写）。

### `report` — HTML 报告

```bash
skill-eval report out/iteration-1/        # 生成静态 report.html
skill-eval report out/iteration-1/ --open # 生成并打开
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--benchmark` / `--previous` / `--scorecard` | 自动 | 各 JSON 路径（`--scorecard` 别名 `--static`） |
| `--open` | 关 | 生成后浏览器打开 |

自动发现 `benchmark.json` / `scorecard.json` / `vet-report.json` / `gate.json`（存在则标题旁渲染门禁徽章）。

### `batch` — 批量评测

```bash
# 用 skills 目录（自动 discover）
skill-eval batch /path/to/skills-directory/ --runs 3 -o batch-workspace/

# 用 manifest.json
skill-eval batch manifest.json --runs 3
```

Manifest 格式：
```json
{"name": "project", "skills": [{"path": "skills/s1", "depends_on": []}, {"path": "skills/s2", "depends_on": ["skills/s1"]}]}
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--runs` | **3** | 每配置运行次数（注意：与 `run` 默认 1 不同） |
| `--workers` / `--max-turns` / `--max-retries` | 4/10/2 | 执行控制 |
| `--fail-fast` | 关 | 首个失败即停 |
| `--output` / `-o` | batch-workspace/ | 输出目录 |

仅 unit 层，按 DAG 拓扑并行。有 eval 失败 → exit 1。

### `batch-report` — 批量 HTML 报告

```bash
skill-eval batch-report batch-workspace/ --open
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--manifest` | 自动 | manifest.json 路径 |
| `--output` / `-o` | workspace 下 | 输出 HTML 路径 |
| `--open` | 关 | 生成后浏览器打开 |

---

## 帮用户设计 eval 用例

1. 读 SKILL.md，了解 skill 功能和边界
2. 问用户想测什么场景，或主动建议：
   - 常规流程（最常用操作）
   - 边界/错误处理（输入不对、文件缺失）
   - 工具选择（有多个工具时选哪个）
   - 输出格式（产出什么文件、什么结构）
3. 对每个场景用 `skill-eval add <path> --idea "描述"` 写入
4. **L1**（知识问答题）测规则理解，**L2**（执行题带 fixture）测实际产出
5. 跑 `skill-eval run` 验证用例质量，根据结果迭代

---

## 断言设计原则

### 两层断言体系

| 层级 | 类型 | 判定 | 适用 |
|------|------|------|------|
| **Tier 1** 确定性 | `FILE_EXISTS` `FILE_CONTAINS` `CONTAINS_PATTERN` `NOT_CONTAINS` `JSON_HAS_FIELD` `LINE_COUNT` `TOOL_CALLED` `STEP_COUNT` `CUSTOM_SCRIPT` `CUSTOM_PLUGIN` `CROSS_FILE_CONSISTENCY` `CODE_COMPILES` | 正则/文件/工具痕迹检查 | 可机械验证 |
| **Tier 3** 语义性 | `LLM_GRADE` `LLM_FACT` `LLM_NOT_DELEGATE` | LLM 打分 + 举证 | 语义判断 |

### 关键规则

1. **同一维度只用一层**：不要同时放 `NOT_CONTAINS "x"` 和 `LLM_NOT_DELEGATE`
2. **校准无需特殊 eval 设计**：校准是结果（优先押客观 pass_rate≥0.8，无客观锚回退主观）× 过程（遵循性有无 prohibition/output 硬类违规）的 coherence 判决，自动跑、给 5 态 verdict（可信/高分存疑/可修复/技能失效/过程未知），不需要为它刻意"同一项同时写 assertion + expectation"
3. **不为每条 expectation 自动套 CONTAINS_PATTERN**：expectation 是 LLM 语义判断，CONTAINS_PATTERN 是机器正则，量的是不同东西

---

## 标准操作流程

### 首次评测一个 skill（无 evals.json）

```bash
SKILL="/path/to/skill"
OUT="output"

# Step 0: 没有 evals.json 就生成
[ -f "$SKILL/evals.json" ] || skill-eval init "$SKILL"

# Step 1: 静态体检 + 安全审查（零 API 成本）
skill-eval vet "$SKILL"
skill-eval analyze "$SKILL"

# Step 2: A/B 评测（推荐 --runs 3）
skill-eval run "$SKILL" --runs 3 -o "$OUT"

# Step 3: 出门禁判决（exit code 驱动 CI）
skill-eval gate "$OUT/iteration-1/benchmark.json" --min-win-rate 0.5
echo $?    # 0=PASS / 1=FAIL / 2=N/A(不适用) 或 UNCLEAR(判不准)

# Step 4: 看报告
skill-eval report "$OUT/iteration-1/" --open
```

### 回归对照（候选 v2 vs 基线 v1）

```bash
# 冻结同一份题，保证跨 run 同卷
skill-eval freeze path/to/evals.json --name baseline

# A/B 跑（两边各注入各自 skill）
skill-eval run v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/

# 出判决
skill-eval gate out/iteration-1/benchmark.json --min-win-rate 0.5
```

### 真任务遵循性（路径1：跑真实任务 → 对账指令）

```bash
# 跑真实任务（--cwd 拷贝隔离，cc 不碰原目录）
skill-eval run /path/to/skill/ --task "<真实任务>" --cwd ./repo --runs 2 -o /tmp/live

# 对账 transcript × SKILL.md
skill-eval audit /path/to/skill/ --transcript /tmp/live/run-1/transcript.jsonl -o /tmp/live-audit --open
```

### "快速验证"

```bash
skill-eval run /path/to/skill --eval-samples 3 --runs 1 -o out/
```

### "批量评测多个 skill"

```bash
skill-eval batch /path/to/skills-dir/ --runs 3 -o batch-workspace/
skill-eval batch-report batch-workspace/ --open
```

---

## 评测产物

```
output/iteration-1/
├── benchmark.json          # 聚合：双率 delta + comparisons + comparison_summary + analysis
├── report.html             # 自包含 HTML 报告（读 gate.json 显门禁徽章；run 末尾自动产）
├── gate.json               # gate 命令产出（判决 + 两轴明细；非 run 产出）
├── scorecard.json          # run 末尾自动产（静态评分卡；也可 analyze -o 单独产）
├── vet-report.json         # run 末尾自动产（原生安全扫描；也可 vet -o 单独产）
└── with_skill/             # v1/v2 模式换成各自标签名
    └── eval-<id>/run-<n>/
        ├── outputs/        # Claude 产生的文件（skill references 已自动拷入）
        ├── grading.json    # 评分（expectations + assertions pass/fail，双率）
        ├── compliance.json # 遵循性检查（五态 verdict + method；仅 with_skill）
        ├── calibration.json
        ├── run_debug.json  # 完整回复 + tool_calls + timing
        └── timing.json
```

报告完整性检查：

```bash
python3 << 'PYEOF'
from pathlib import Path; import json
it = Path("output/iteration-1")
ok = True
for f in ['benchmark.json','report.html']:
    if not (it/f).exists(): print(f'MISSING: {f}'); ok = False
if (it/'gate.json').exists():
    g=json.load(open(it/'gate.json')); print(f'gate: {g["decision"]}')
if (it/'scorecard.json').exists():
    s=json.load(open(it/'scorecard.json')); print(f'scorecard: {s.get("score")}/{s.get("max_score")}')
if (it/'vet-report.json').exists():
    v=json.load(open(it/'vet-report.json')); print(f'vet: {v["verdict"]}')
if (it/'benchmark.json').exists():
    b=json.load(open(it/'benchmark.json'))
    delta=b.get('run_summary',{}).get('delta',{})
    cs=b.get('comparison_summary',{})
    print(f"delta.pass_rate_programmatic: {delta.get('pass_rate_programmatic')}")
    if cs: print(f"blind win_rate: {cs.get('win_rate')} ({cs.get('a_wins')}W/{cs.get('b_wins')}L/{cs.get('ties')}T/{cs.get('failed')}F)")
print('ALL OK' if ok else 'ISSUES')
PYEOF
```

---

## 常见问题

| 问题 | 解决 |
|---|---|
| skill-eval 未安装 | 向用户索要 GitHub URL，clone + `bash install.sh` |
| `run` 退 1 了 | 只在 `--security-gate` 触发时；负 delta 不再退 1。通过/失败看 `gate` |
| pass_rate 全 0% | expectation 太模糊，重新 `init`；或用 `--scoring llm` |
| 超时 | `--timeout 600`，`--eval-samples 5` |
| without_skill 无 grading | 正常（无 skill 时 Claude 可能不产出可评判内容） |
| calibration 过程未知 | compliance 缺失或无可判定项（如纯推理题）；正常现象，非缺陷 |
| calibration 高分存疑 | 输出对了但过程违反 prohibition/output 硬性规则；看 evidence 核对 |
| benchmark.json 缺失 | aggregation 失败，单独 `skill-eval report iteration-1/` |
| report 评分卡/安全面板空 | run 默认已自动产；若空说明跑了 `--no-static`/`--no-vet` 或 analyze/vet 失败（run 会打 Warning，不阻断）|
| batch 显示 0 skills | 输入不是 skills 目录或 manifest.json 格式不对 |
| LLM 断言返回 "Not evaluated" | 自动重试（默认最多 2 次）；仍 UNEVAL 则标记 INDETERMINATE（报告里区分显示，不混成 FAIL） |
| 单次运行方差大 | `--runs 1` 仅快速验证；推荐 `--runs 3` 启用异常检测和统计有效性 |
| 遵循性率低 | SKILL.md 指令有矛盾或太难遵循，重写指令 |
| `init --from-transcript` 出 degraded | LLM 失败走了 fallback，终端会告警；重跑或检查 claude CLI 可用性 |
