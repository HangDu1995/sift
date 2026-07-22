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
skill-eval vet path/to/skill/                # 表格输出(默认不落盘,-o 写文件)
skill-eval vet path/to/skill/ --format json  # JSON 输出
skill-eval vet path/to/skill/ --output report.json
skill-eval vet path/to/skill/ --skip-external # 跳过外部扫描器
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` / `-o` | 无 | 输出文件路径（不给则仅终端输出，不落盘） |
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

> 生成的 `evals.json` 格式（字段 / AssertionType 全表 / expectations vs assertions / tier 坑）详见 [evals-format.md](evals-format.md)。

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
| `--by-skill` | off | transcript 含多个 skill 时只挖本 skill 被调用期间段（按 Skill 调用切分，同 audit）；不传 + 检测到多 skill → 警告 |
| `--timeout` | 600 | LLM 调用超时秒数（mining 是单次大 prompt；撞超时 → degraded fallback） |

`--from-transcript` 解析 session transcript（模型、user turns、用到的 skill），重建 input_files / commands / produced_files，生成带结构化 assertions 的 eval。若 LLM 失败降级为 fallback eval，会带 `degraded:*` flag 并在终端告警（不再静默）。多 skill transcript 不传 `--by-skill` 时会把全部当本 skill 挖（可能串味）→ 终端告警，建议加 `--by-skill` 只挖本 skill 段。

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

11 项自动化体检，**零 API 成本，<1s 完成**。输出 scorecard（`score / max_score`）。

```bash
skill-eval analyze path/to/skill/                  # 表格（默认不落盘，-o 写文件）
skill-eval analyze path/to/skill/ --format json -o scorecard.json
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` / `-o` | 无 | 输出文件路径（不给则仅终端输出，不落盘） |

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
| `--refine-instructions` | 关闭 | 遵循性判定前 LLM 精筛：砍掉 extractor 误抽的示例/占位伪指令（提精度，见 `audit`） |
| `--no-calibration` | 关闭 | 跳过校准（结果×过程 coherence 判决） |
| `--security-gate` | 关闭 | 安全审查返回 BLOCKED / REVIEW_NEEDED 时判失败 |
| `--no-static` | 关闭 | 跳过末尾自动产静态评分卡（默认产，写进 output 目录根，供 report 评分卡面板） |
| `--no-vet` | 关闭 | 跳过末尾自动产安全审查（默认产，写进 output 目录根，供 report 安全面板） |
| `--no-report` | 关闭 | 跳过末尾自动产 HTML 报告（默认产 report.html；聚合 benchmark/评分卡/安全） |
| `--output` / `-o` | output/ | 输出目录 |
| `--eval-samples` | 无 | 运行 eval 的数量或比例（如 `10` 或 `0.25`） |
| `--format` | table | 输出格式：table / json |
| `--skill-b` | 无 | **v1/v2 模式**：第二个 skill 路径（两边各注入各自 skill）；不给则默认 with/without（同 skill 开/关） |
| `--label-a` | skill 目录名 | v1/v2 模式 side A 显示标签 |
| `--label-b` | skill 目录名 | v1/v2 模式 side B 显示标签 |

**两种模式**：

- **默认 A/B**（无 `--task`）：位置参数 skill = 候选（side A）。不给 `--skill-b` → with/without 对照（同 skill 开/关）；给 `--skill-b <dir>` → v1/v2 双 skill 对照（两边各注入各自的 skill，标签默认取目录名）。
- **路径 1 真任务模式**（给 `--task`）：忽略 evals.json，spawn cc 在 `--cwd`（拷贝隔离）跑真任务，产 `run-*/transcript.jsonl`，结尾打印 `audit` 命令提示。每 run 另在终端打印 operational 摘要（耗时 / turns / tokens / stop_reason / tool_calls + 结果头部，读自 `run-*/run_debug.json`）。此模式 `--evals`/`--scoring`/`--workers` 等大多不生效。

> **退出码**：`run` 是纯数据生产者——exit 0 = 产数据成功，2 = 输入错误，3 = 执行错误。**通过/失败判决不再由 `run` 的退出码决定**（旧版读混合 `delta.pass_rate<0` 退 1 的逻辑已退役），改由独立 [`gate`](#gate--通过失败判决) 命令出。`--security-gate` 触发时仍退 1。

**输出目录结构**（默认 with/without 模式；v1/v2 模式把 `with_skill`/`without_skill` 换成各自标签名）：

```
output/
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
  scorecard.json             ← run 末尾自动产(静态评分卡;也可 analyze -o 单独产)
  vet-report.json            ← run 末尾自动产(原生安全扫描;也可 vet -o 单独产)
  report.html                ← run 末尾自动产(也可 skill-eval report 重生成/带 --previous)
  gate.json                  ← 运行 gate 命令后产生(非 run 产出)
```

### `gate` — 通过/失败判决

对已有 benchmark.json 出通过/失败判决。**post-hoc 纯函数**——不重跑 claude、毫秒级、可反复改阈值重判（与 report/audit 一样，只读 run 的产物做后处理，不重新跑题）。

```bash
skill-eval gate output/benchmark.json                        # 仅客观回归守卫
skill-eval gate output/benchmark.json --min-win-rate 0.5     # 加主观 gate
skill-eval gate output/benchmark.json --no-programmatic-guard --min-win-rate 0.6
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
- **主观侧 comparison**（`--min-win-rate` opt-in）：tie-排除胜率 `a_wins/(a_wins+b_wins)` ≥ 阈值 → PASS；< 阈值且决定性比较数 ≥ `--min-valid` → FAIL；样本不足 → UNCLEAR（判不准）。

**判决**：AND——任一适用轴 FAIL → FAIL；无 FAIL 但有适用轴信号不足（判不准/样本不足）→ **UNCLEAR**；无适用轴（守卫关 / 未请求 / 无信号）→ **N/A（不适用）**；否则 PASS。写 `gate.json`，按 `PASS/FAIL/N/A/UNCLEAR` 退 `0/1/2/2`（N/A 与 UNCLEAR 都 exit 2，消费方定）。

> exit 2 多语义：**使用错误**（文件/解析错误，不写 gate.json）、**N/A 判决**（不适用，无适用轴）、**UNCLEAR 判决**（判不准，有轴但信号不足）都退 2——靠 gate.json 是否存在 + 其 `decision` 字段（N/A vs UNCLEAR）区分。
>
> **不适用（N/A）为何也 exit 2、不放行**：不适用=无适用轴，常意味着门禁根本没评上（如配置漏：纯主观题 + 没给 `--min-win-rate` + 关了 programmatic 守卫）。放行（exit 0）会让"没评"伪装成"通过"，有风险；故与判不准（UNCLEAR）一样退 exit 2 交消费方定（备选"不适用→exit 0 放行"暂缓，因放行风险）。两者都 exit 2，靠 gate.json 的 `decision` 字段区分 N/A vs UNCLEAR。

### `audit` — 真实 transcript 对账

把真实会话 transcript 与 SKILL.md 对账，判指令遵循性。**不 re-run**，直接对账历史执行（与 fixture compliance 互补）。支持多 transcript 指令级聚合。

> 想拿现成材料试手 + 对照预期结果：自带 3 个 audit 示例（合成违规 transcript，预期该抓的违规已逐条列明）+ 1 个 multi-skill-demo（演示 `--by-skill` 在一条多 skill transcript 里只对账一个 skill），见 [example-skills/README.md](../fixtures/example-skills/README.md)。

> 判定要逐批调 LLM（每批约 40–257s），跑期间**实时打印进度**：进每个 transcript 打头（`对账 transcript i/N: <name>`），每个 LLM 批前后各一行——`[条件性/禁令/步骤] 判定 N 条…` → `[条件性/禁令/步骤] 完成 ⏱ Xs`（类别名打头括号化；指令 >10 条分批时才显 `批 k/总`，单批不显）。**两种失败都会先自动重试（最多 2 次）再兜底**：① 调用失败（超时 / 退出码 / 限流）；② 调用成功但整批 0 条解析成功（LLM 输出畸形/截断，原是看着"完成"却全 N/A 的静默盲点）——都大概率瞬态、重试常能救回。重试打 `重试 n/2…`，耗尽才 `重试耗尽 → 全兜底 N/A`（audit 汇总"未判定"警告提示重跑）。看到 `[条件性/禁令/步骤] …` 滚动就是在跑，别误以为卡死。

> 三个 LLM 类别判定器（条件性/禁令/步骤）**并发跑**（`max_workers=3` = 类别数 = 实测安全并发），墙钟约 **2× 提速**（串行 13+51+37=101s → 并发 ≈51s）。副作用：三类的进度行会**交错**出现（每行 `[类别]` 打头，一眼分清是哪类在跑）；输出格式判定器是确定性子串匹配（无 LLM），不走并发。

```bash
skill-eval audit path/to/skill/ --transcript run-1/transcript.jsonl -o audit-out/
skill-eval audit path/to/skill/ --transcript a.jsonl --transcript b.jsonl --format json
# transcript 里用了多个 skill,只对账本 skill 被调用的段(按 Skill 工具调用切分):
skill-eval audit path/to/skill/ --transcript big-multi-skill.jsonl --by-skill
# opencode 跑出的 session(`opencode export <sessionID>` 导出,顶层 info/messages、parts 嵌在 message 里):
skill-eval audit path/to/skill/ --transcript opencode-session.json
```

> **`--transcript` 吃两种格式(按后缀自动分派)**：cc 会话 `.jsonl`(逐行 JSON 记录,含 `Skill`/`Read`/`Write`/`Bash` tool_use);opencode session `.json`(`opencode export <sessionID>` 的产出,顶层 `info`/`messages`,parts 嵌在 message 里、role/agent 在 `message.info`)。两者重建成同一 `SessionDigest` 再对账;`--by-skill` cc 按 `Skill` tool_use 切分、opencode 按 per-message `agent` 切分(扁平,无 cc 父子作用域)。run 产的 `transcript.jsonl` 永远是 cc 格式(run 只 spawn cc)。生产导出 opencode 会话:`opencode session list` 找 id → `opencode export <id> > session.json`(`--sanitize` 可脱敏分享)→ `skill-eval audit --transcript session.json`。

> **拿到一个 opencode `.db` 文件怎么审**（别人/别处拷来的库,不是你本机默认库）：opencode 的 `export` 只认它自己配置的 db（无 `--db` 参数），但 opencode 认 `XDG_DATA_HOME` 环境变量——设它就能让 opencode 去指向那个 .db、**不碰你本机真库**（`XDG_DATA_HOME` 是 Linux「应用数据目录」规范,opencode 把 db 放在 `$XDG_DATA_HOME/opencode/opencode.db`）。三步:
> ```bash
> # 1. 把 .db 摆成 opencode 认的布局(<envdir>/opencode/opencode.db;库大可 ln -s 软链代替 cp)
> mkdir -p /tmp/ocenv/opencode && cp /path/to/handed.db /tmp/ocenv/opencode/opencode.db
> # 2. 用 XDG_DATA_HOME 指向它,列 session 找 id
> XDG_DATA_HOME=/tmp/ocenv opencode session list
> # 3. 导出目标 session(--sanitize 脱敏分享)→ audit
> XDG_DATA_HOME=/tmp/ocenv opencode export ses_xxxx --sanitize > session.json
> skill-eval audit path/to/skill/ --transcript session.json
> ```
> 若 .db **就是你本机 opencode 跑的**（默认位置）,跳过 XDG,直接 `opencode session list` → `opencode export <id>` → audit。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--transcript` | **必填** | 真实会话 transcript（cc `.jsonl` 或 opencode session `.json`，按格式自动分派；**可重复**，多 transcript 聚合） |
| `--by-skill` | 关闭 | transcript 里调了多个 skill/agent 时，只对账本 skill 被调用的段（cc 按 `Skill` 工具调用切分；opencode 按 per-message `agent` 字段切分，见下） |
| `--refine-instructions` | 关闭 | 判定前 LLM 精筛：砍掉 extractor 误抽的示例/占位伪指令（精度>召回，见下） |
| `--output` / `-o` | ./output | 输出目录 |
| `--model` | 默认 | 语义判定 LLM |
| `--format` | table | table / json |
| `--open` | 关闭 | 生成后浏览器打开 `audit-report.html` |

> **`--by-skill`：多 skill transcript 只对账一个 skill**。transcript 里调了多个 skill 时，默认会把整段当一个袋子判（其他 skill 的行为会混进报告）。加 `--by-skill` 后，按 transcript 里的 `Skill` 工具调用切成若干单 skill 子 transcript，只对账 `<skill_path>` 这个 skill 被调用期间的段。归段 **scope-aware**：每个 `Skill` 调用起一段、归该 skill 到下一次调用；子 agent（Agent 工具）是独立作用域，其记录带 `parent_tool_use_id`，子 agent 里用的 skill 切到 Agent 返回、**不污染主 agent 归属**；target 调多次 → 多段 → 多 transcript 聚合。**两条限制**：① skill 没有"返回"信号，所以"调 A → 调 B → 不重调 A 接着干 A"那段会归给 B（不可检测，解法是 agent 显式重调 A）；② 把委派子 skill 的段折进主 skill 审计（inclusive）需用户声明，暂不支持。该 skill 没在 transcript 里被调过 → 报错退出（exit 2），不静默跑空。

> **`--refine-instructions`：判定前精筛伪指令**。extractor 是纯正则（静态全抽 SKILL.md），会把教程型 skill 里的**示例文字 / 模板占位 / 描述性句子**误抽成"指令"——这些伪指令喂给判官只能出 N/A（无法对账）或偶发假 FAIL。加 `--refine-instructions` 后，audit 在 extract 后、判定前用 LLM 逐条分类"真实可检查指令 vs 示例/占位"，**只砍明确判为伪指令的**（精度 > 召回：判不准 / 没判成 / 调用失败一律保留，宁可留伪指令判 N/A，也不砍真指令）。per-skill 只精筛一次（多 transcript 共享），砍掉的条数和来源写进 report 顶部 warnings。OUTPUT_FORMAT（纯程序子串匹配，精筛无收益）不精筛。`run` 也支持（经 RunConfig 传给内部 audit）。opt-in，验证稳定后考虑默认开。

判定分四类——每条指令按 SKILL.md 声明来源路由到一类，各按不同判法出 verdict。每条 finding 有两个**正交**维度：**verdict（结果，五态）** + **method（怎么查的，程序 `PROGRAMMATIC` / LLM `LLM`）**。

- **PASS / FAIL**：已判明通过 / 违规。
- **N/A（不适用）**：规则存在但本次 transcript 场景不触发——已解决的有效结论。
- **UNRESOLVED（没判成）**：LLM 调用 / 解析失败 / 未提及 / 畸形值——操作性失败，**可重跑救回**。
- **INDETERMINATE（判不准）**：LLM 看了证据但真模糊（证据相互矛盾 / 关键缺失），给不出 pass/fail/na——**重跑无益**（只能人工复核），强制带 `indeterminate_reason`（evidence 字段）。

| 类别 | 来自哪些指令 | 怎么判 | 结论 |
|------|------------|--------|------|
| 条件性 CONDITIONAL | TOOL_REQUIREMENT / REFERENCE_USAGE（该用 X 工具、读 X 参考） | 命中 tool_trace → PASS（**程序判定，不调 LLM**）；未命中 → LLM 判该场景该不该触发 | 遵循 / 违反 / N/A / 没判成 / 判不准 |
| 禁令 PROHIBITION | CONSTRAINT / ANTI_FABRICATION（不许做 X） | LLM **主动检测这次有没有违规**（不是判“遵守没”） | 未违反→PASS / 违反→FAIL / 没判成 / 判不准 |
| 步骤 STEP | HEADING_STEP（应走第 N 步） | LLM 判走过 / 该走没走 / 不涉及 | pass / fail / na / 没判成 / 判不准 |
| 输出格式 OUTPUT | OUTPUT_FORMAT（产物应含某格式） | **确定性子串匹配，程序判定，不调 LLM** | 命中→PASS / 未命中→FAIL（无 N/A / 没判成 / 判不准） |

> **禁令判的角度反着来**：其它三类问“该不该做 / 做没做”（正面遵循），禁令问“这次执行里有没有发生**违反**这条禁令的行为”——主动找违规。例：`NEVER delete production data`、`不得编造不存在的错误码`（模型 hallucinate 一个码 → `violated=true` → FAIL）。

> **没判成不乱报**：三类 LLM 判定（条件性 / 禁令 / 步骤）在 LLM 失败 / 解析失败 / 未提及时一律报 **UNRESOLVED（没判成）**，**不保守 FAIL**——FAIL 只留给 LLM 明确判“违反 / 该做没做”。
>
> **N/A、没判成、判不准 是三码事，别混**：① **UNRESOLVED（没判成）** = LLM 没出判定（调用 / 解析失败 / 未提及 / 畸形值）→ 操作性失败、**可重跑**、汇总进“建议重跑”警告；② **INDETERMINATE（判不准）** = LLM 出了判定、判定本身是“证据真模糊判不了”→ semantic、**重跑无益**，须带具体 reason（写明矛盾/缺失的证据，空 reason 降级回 UNRESOLVED）；③ **N/A（不适用）** = 规则存在但本次场景不触发——条件性类 LLM 明确判“不涉及”（`applicable=false`）、步骤类 LLM 返回“na”→ 已解决的有效结论、不进警告、不用重跑。N/A 为什么会有：提取是**对 SKILL 静态全抽**（skill 写了“读 references/foo.md”就抽一条），但“该读 foo.md”有触发条件——audit 一个根本不涉及该条件的 transcript 时，agent 没读是对的，不能 FAIL 也不能 PASS（规则没被触发），故 N/A。
>
> **判不准三道滥用闸**（INDETERMINATE 是 LLM 逃逸口，需防滥用）：① **强制 reason**——parser 见 indeterminate 必须带具体矛盾/缺失证据，空则降级 UNRESOLVED（不许空口“判不准”）；② **占比告警**——audit 统计 indeterminate 占比，超 20% 且总条数 ≥5 → 报告顶部告警提示人工复核是否 LLM 滥用；③ **prompt 纪律**——三个判官 prompt 都写明“优先明确结论，indeterminate 须写具体矛盾证据，不许偷懒”。
>
> **何时提示“建议重跑”**：`○ 没判成`（UNRESOLVED）条数 > 0 时，audit 在报告顶部出警告（`X 条指令没判成(UNRESOLVED:LLM 调用/解析失败/未提及)，本次结果可能不完整，建议重跑`）。**重跑 = 重跑同一条 `audit` 命令**（纯对账、无副作用，不 re-run）；这些失败多为瞬态（LLM 后端失败 / 限流 / 输出截断，retry 没救回才落到这），重跑常能救回。INDETERMINATE（判不准）**不**提示重跑（重跑无益）。不重跑则那些条没判成、可能漏掉真违规，故 **“0 FAIL ≠ 全过”**——看警告里 UNRESOLVED 是不是 0。

产出 `<out>/audit-report.json`（机器读）+ `<out>/audit-report.html`（人读，**always 写**，与 JSON 并列，对齐 scoring report 视觉）。HTML 自上而下：① 头部（skill / summary 计数徽章：✓通过 / ✗失败 / ⊘不适用 / ○没判成（>0 时）/ ?判不准（>0 时）/ 反推指令数）② 警告框（没判成建议重跑、判不准占比偏高告警）③ 系统性聚合表（**仅多 transcript**，跨会话 fail% + fail in transcripts）④ findings 全集（**含 PASS**，每条带 verdict 徽章 + method 标签（程序/LLM）+ evidence + category + source；多 transcript 按指令分组可折叠，FAIL/UNRESOLVED/INDETERMINATE/N/A 默认展开）。

> **「X 条由 transcript 反推」是什么**：findings 里 `derived_from_transcript: true` 的不是 SKILL.md 抽出来的，而是从 transcript 实际行为**补**出来的检查（只增不减）。v1 只补一类：transcript 里 agent 真实 Read 过的输入/参考文件（`digest.input_files`）→ 各补一条「读取 \<path\>」（REFERENCE_USAGE）。例：SKILL.md 只写"读用户指定的日志文件"不点名，transcript 实际读了 `app.log` → 反推一条「读取 app.log」并确认命中。这类由 Read 行为反推的 read 检查构造上即 PASS（不膨胀 fail），作用是把 agent 真碰过的输入文件记进报告。`required_tools`（Bash 命令分不清业务/通用）、`required_outputs`（历史 trace 产物未必还在盘）不反推。

> **怎么看结果（别只数 fail）**：每条 finding 带 **#N 序号**。① **同文重复已去重**——同句被抽成 `CONSTRAINT` + `ANTI_FABRICATION` 的合并成一条、来源徽章显 `CONSTRAINT + ANTI_FABRICATION`（不再虚增 fail）。② **同行为跨类已关联**——同一行为（如没读某 reference）同时违反 条件性 + 步骤 两条**不同**规则的，仍是两条（确是两条规则），但互标 `↔ 同源违规 #N`（可点跳）；故 fail 条数仍可能 > 真实问题数，**逐条看 evidence 对照"哪个行为"**，别数条数。③ **没判成（UNRESOLVED）多 ≠ 全过**——LLM 没判成的一律 UNRESOLVED（见上），汇总成警告提示重跑；N/A（不适用）是已解决结论、不进警告。④ **对照预期**：自带 3 个 audit 示例的预期违规已逐条列明（见 [example-skills/README.md](../fixtures/example-skills/README.md)），跑完比对该抓的违规是否都出现在 FAIL、evidence 是否点中。⑤ **verdict 徽章 + method 标签**（每条）：左侧 verdict 五态图标（✓通过 / ✗失败 / ⊘不适用（场景不触发）/ ○没判成（可重跑）/ ?判不准（重跑无益）），右侧 method 标签「程序」（tool_trace 命中 / 子串匹配，不调 LLM）或「LLM」。

### `report` — HTML 报告

```bash
skill-eval report output/        # 生成静态 report.html
skill-eval report output/ --open # 生成并打开
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--benchmark` | 自动 | benchmark.json 路径 |
| `--previous` | 无 | 上一轮 benchmark.json（对比） |
| `--scorecard` / `--static` | 自动 | scorecard.json 路径（双别名） |
| `--open` | 关闭 | 生成后浏览器打开 |

自动发现 `benchmark.json`、`scorecard.json`、`vet-report.json`、`gate.json`（存在则标题旁渲染门禁徽章）。两个标签页：
- **单次执行** — 浏览该 run 的 prompt、输出文件、评估详情（打分 + 遵循性 + 校准三层）。顶部下拉按 eval 分组，可直接跳转到指定 run（不必逐个翻「上一个/下一个」）；方向键仍可逐条翻
- **整体汇总** — 门禁徽章 + 安全面板 + 评分卡 + 异常检测 + 通过率对比（双率）+ 盲评对比 + 深度分析 + 检查点总览（per-eval 程序/LLM/混合 3-dim 一览，点行跳明细）+ 逐项评测明细（per-run 3-dim + 检查项 ✓/✗ 矩阵）+ 遵循性聚合（整个考试）

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
│  评分引擎    │  遵循性引擎      │  校准                       │
│ (Scoring)   │  (Compliance)   │  (Calibration)              │
│             │                 │                             │
│ 输出正确？   │  过程遵循？      │  结果×过程协调？             │
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

每条 expectation/assertion 带 `source`（`programmatic` / `llm`）。**评分**按 source 拆**双率**，让干净信号不被 LLM 阅卷噪声污染：

| 率 | 来源 | 稳定性 | 信任度 |
|---|---|---|---|
| `pass_rate_programmatic` | 确定性断言（程序判定） | 稳（A=0 噪声） | 强信 |
| `pass_rate_llm` | LLM 语义判定 | 抖（阅卷噪声） | 看方向 |
| `pass_rate`（总率） | 全部混算 | 历史兼容 | 粗略 |

> **遵循性不分 source 双率**：`compliance.json` 现是 **AuditReport**（遵循性引擎已统一到 audit），按语义 category 四类（condition/prohibition/step/output）判，产 `summary{total,pass,fail,na,unresolved,indeterminate}`（没判成/判不准）。遵循度 = `pass/(pass+fail)`，排除 **N/A**（不适用）+ **UNRESOLVED**（没判成）+ **INDETERMINATE**（判不准）——三者都不进分母，与 audit 命令的 `audit-report.json` 同 schema。

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

### 校准判决（结果×过程 coherence）

`calibration.json` 是单次 with_skill 执行的**结果×过程 coherence 判决**——打分（结果面）与遵循性（过程面）协不协调，产出一个 5 态 `verdict`，不是凑出来的置信度数。

- **结果轴**：优先押**客观** `pass_rate_programmatic ≥ 0.8` → 结果对；纯主观题无客观锚时回退主观率（`scoring_rate_basis=mixed`，显示标注「基于主观判分」）。
- **过程轴**：遵循性（audit）里有无 prohibition/output **硬类**违规。有 → 过程错；无 → 过程对；compliance 缺失/无可判定 → 过程未知。step/conditional 的 FAIL 只记 `soft_notes`、不翻判决（避免 LLM 方差污染 verdict）。

| verdict | 结果 | 过程 | 含义 |
|---|---|---|---|
| TRUSTED 可信 | 对 | 对 | 未发现过程问题 |
| HIGH_SCORE_SUSPECT 高分存疑 | 对 | 错 | 输出对了但过程违反硬性规则（列 hard_violations evidence） |
| FIXABLE 可修复 | 错 | 对 | 输出未达标，过程无明显违规 |
| SKILL_FAILED 技能失效 | 错 | 错 | 输出未达标且过程违反硬性规则 |
| PROCESS_UNKNOWN 过程未知 | — | 无可对账 | compliance 缺失或无可判定项（如纯推理题） |

`calibration.json` 字段：`verdict` / `result_pass` / `process_status`（ok/violated/unknown）/ `hard_violations`（每条 category+instruction_text+evidence）/ `soft_notes`（step/conditional FAIL 计数）/ `scoring_rate` / `scoring_rate_basis`（programmatic|mixed）/ `scoring_rate_programmatic` / `scoring_rate_llm` / `compliance_rate`。

> 设计取舍：verdict 只押在实测高精度的 prohibition/output 硬类违规上（"高分存疑"= 值得一看的分诊，非定罪）。仅 with_skill 产（without 无 compliance）。旧版的 confidence/grade/agreement/dual-judgment 机制已移除——eval 设计**不再需要**为校准刻意"同一项同时写 assertion + expectation"。
>
> **聚合 + gate 消费**：per-run `calibration.json` 在 benchmark 聚合时聚合成顶层 `calibration_summary`（per-side：`sides: {<side>: {total, verdict_counts, high_score_suspects:[{eval,run,category,instruction_text,evidence,compliance_rate}]} | null}`；with/without 模式 without 侧 null，v1/v2 两侧都有）。gate 的 `_eval_calibration` 读它——把 HIGH_SCORE_SUSPECT 作 **soft warning**：分诊两侧、填 `GateResult.calibration` + `warnings`，**不进 axes、不改 decision/exit_code**（HIGH_SCORE_SUSPECT 是 gate 两轴的盲区：结果对→客观轴过、输出好→盲评过，但 prohibition 被违反）。SKILL_FAILED/FIXABLE 结果错归客观轴，不重复标。viewer `renderCalibrationSummary` 同源显示。

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
| `expected_output` | 否 | 预期输出的文字描述(grader 评分参考,**不喂给被测 cc**;拼进 prompt = 答案泄露致 A/B 失真) |
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
  "error": "",
  "cache_key": "d5077d0f9e83586a"
}
```

每条 expectation 带 `verdict`（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`）与 `source`（`programmatic`/`llm`）。报告里 UNEVAL/TIMEOUT/ERROR 与 FAIL **区分显示**（?/⏱/!），不会被混成红 ✗——"没评出来/超时/出错"不是"失败"。

`summary` 的双率字段（`*_programmatic` / `*_llm`）按 expectation 的 `source` 分桶；某类无 expectation 时为 `null`（不报假 0）。

`cache_key` 是**内容感知续跑键**：`= sha256(SKILL 正文 + eval 全身 + 模型/配置 + skill-eval 代码版本)[:16]`。重跑同一 skill 时，`run` 只对 `cache_key` 与本次输入**完全一致**且 `summary.total > 0` 的槽跳过重跑（省 token）；改了 SKILL.md / evals / model / scoring 配置 / **升级了 skill-eval 自身** → key 变 → 自动失效重跑，不再盲复用旧结果。代码版本 = `src/skill_eval/**/*.py` 内容聚合 hash（进程内只算一次），故 `pip install -e .` 升级或本地改了 runner/grader 后默认目录重跑也会用新代码。BASE 侧（`without_skill`）不含 skill 正文，故改 SKILL.md 只重跑 with 侧。**已知缺口**：skill 目录里 eval 引用的 `source_dir` / `references/` 文件内容改动不检测——改这些请用新 `-o` 强制重跑。老 `grading.json`（无此字段）首次重跑时全部失效一次（向后兼容）。

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

每条 `comparisons` 还携带 `rubric`（两侧 overall/content/structure 分）、`output_quality`（score + strengths/weaknesses 数组）、`expectation_results`（两侧 passed/total + 逐 expectation `{text, passed}` 明细）——报告逐条比较渲染这些；`analysis.transcript_insights`（dict）、`metadata` 的 `executor_model`/`analyzer_model`/`comparator_model` 也一并展示。

- 每个 config 的 `run_summary` 含 `pass_rate` / `pass_rate_programmatic` / `pass_rate_llm`（后两者可能为 `null`）+ `time_seconds` / `tokens`。
- `delta` 全为有符号字符串（`+0.27`），= `configs[0] - configs[1]`（候选 - 基线）；双率项可能为 `null`。
- `comparison_summary.win_rate` = `a_wins/valid`（tie-计入）；`gate` 用 tie-排除版自行算。

### gate.json（`gate` 命令产出）

```json
{
  "decision": "PASS",
  "exit_code": 0,
  "reason": "客观侧无回归(+0.27); 主观胜率 0.75 >= 0.50 (8 决定性)",
  "artifact": "output/benchmark.json",
  "candidate": "with_skill",
  "baseline": "without_skill",
  "thresholds": {"min_win_rate": 0.5, "min_valid": 3, "programmatic_guard": true},
  "axes": {
    "programmatic": {"verdict": "PASS", "delta": 0.27, "reason": "..."},
    "comparison": {"verdict": "PASS", "win_rate": 0.75, "decisive": 8, "a_wins": 6, "b_wins": 2, "ties": 1, "reason": "..."}
  },
  "warnings": [],
  "calibration": {
    "sides": {
      "with_skill": {"triggered": true, "count": 3, "total": 21,
                     "evidence": [{"eval": 1, "run": 2, "category": "prohibition",
                                   "instruction_text": "引用日志原文行", "evidence": "未引用",
                                   "compliance_rate": 0.875}]}
    }
  }
}
```

### 输出文件说明

`run` 命令完成后，`output/` 目录：

| 文件 | 内容 |
|------|------|
| `benchmark.json` | 聚合结果（双率 delta + 盲比较 comparison_summary + calibration_summary per-side 高分低遵循） |
| `report.html` | 自包含 HTML 报告（读 gate.json 显门禁徽章 + previous/benchmark.json 显「vs 上次」纵向 delta 卡）；run 末尾自动产（也可 `report` 命令重生成 / 带 `--previous` 基线对照） |
| `previous/` | 重跑归档：fresh 重跑时把上一次完整 run 树整体 MOVE 到此（单槽覆盖）。含旧 `benchmark.json`（聚合纵向 delta 数据源）+ 旧 outputs/（report「上次输出」文件级面板）。仅 fresh 产生；accumulate/resume 不产。见下「重跑归档」 |
| `gate.json` | `gate` 命令产出（非 run 产出）；判决 + 两轴明细 + calibration soft warning（高分低遵循 HIGH_SCORE_SUSPECT，不进 decision） |
| `scorecard.json` | run 末尾自动产（静态评分卡；也可 `analyze -o` 单独产） |
| `vet-report.json` | run 末尾自动产（原生安全扫描；也可 `vet -o` 单独产） |
| `with_skill/eval-<id>/run-1/grading.json` | expectations + assertions 的 pass/fail（双率） |
| `with_skill/eval-<id>/run-1/compliance.json` | SKILL.md 指令遵循性（AuditReport：summary 计数 + findings 五态 PASS/FAIL/N-A/UNRESOLVED/INDETERMINATE；遵循度 = pass/(pass+fail)，排除 N/A + UNRESOLVED + INDETERMINATE） |
| `with_skill/eval-<id>/run-1/calibration.json` | 结果×过程 coherence 判决（5 态 verdict；仅 with_skill 产） |
| `with_skill/eval-<id>/run-1/run_debug.json` | 完整回复 + tool_calls + timing |
| `with_skill/eval-<id>/run-1/timing.json` | 执行耗时和 token 统计 |

#### 重跑归档（resume / accumulate / fresh）

`run` 开始时按 `output/benchmark.json` 是否存在 + 现有 slot 的 cache_key 是否仍匹配 + `--runs` 是否调大，判三种行为：

| 情况 | `--runs` vs 已有 | 行为 | previous/ |
|---|---|---|---|
| 上次没跑完（`benchmark.json` 不在） | 任意 | **resume**：cache 续跑已完成的 slot，补跑剩下的 | 不产 |
| 上次跑完 + inputs 没改 | 调大 | **accumulate**：旧 slot cache 复用 + 补新 run 号，聚合算全部 | 不产 |
| 上次跑完 + inputs 改了（skill/代码/eval/model） | 任意 | **fresh**：旧树 MOVE 到 `previous/` + 全部 fresh 重跑 | ✅ 产生 |
| 上次跑完 + inputs 没改 | 同/变小 | **fresh**：MOVE 旧树 + fresh 重跑（看复现性 / 主观侧方差） | ✅ 产生 |

fresh 用 **MOVE**（非 copy）：清空原位 → 全 slot cache miss → 必然 fresh 重跑；同时旧树进 `previous/` 供对比。重跑要看的就是方差（cc 非确定性，主观侧有真方差），copy 反而会让 cache skip 出"和上次一模一样"的无意义归档。accumulate 的"复用旧"靠 cache_key 匹配自然达成（不 MOVE → grading.json 在位 → skip）。

report 读 `previous/benchmark.json` 算「vs 上次」聚合 delta（双率 + 盲评 + 样本数）；样本数不同（如 6 vs 上次 3 runs）时显式标出提醒可比性。文件级「上次输出」面板只对 previous 里也存在的 run 显示，本次新增的 run 自然无对照（隐藏）。

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
- `compliance/` — `InstructionExtractor`(SKILL.md → 指令;audit/run 共用的出题地基)。判定引擎在 `audit/`(transcript 对账,run 的遵循性也走它)
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
| 2 | N/A（不适用，无适用轴）或 UNCLEAR（判不准，信号不足）或 使用错误（文件/解析错误）——靠 gate.json 是否存在 + decision 字段区分 |

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
skill-eval gate out/benchmark.json --min-win-rate 0.5
echo $?   # 0=PASS / 1=FAIL / 2=N/A(不适用) 或 UNCLEAR(判不准)
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
skill-eval gate out/benchmark.json --min-win-rate 0.5
# exit: 0=PASS(放行) / 1=FAIL(卡合并) / 2=N/A(不适用) 或 UNCLEAR(判不准),消费方定
```

门禁阈值与 CI 落点（分支保护、check 名、N/A / UNCLEAR 是否阻塞）归消费方配置——skill-eval 只提供机制（PASS/FAIL/exit-2 兼盖 N/A 与 UNCLEAR + gate.json）。
