# Skill-Eval 使用手册

> 本手册面向第一次接触 skill-eval 的读者，从"为什么需要它"讲起，逐层展开每个功能的设计思路与使用方法。读完能够：理解 skill-eval 的整体架构、独立跑通任何一条评测路径、看懂每一份产物、在 CI 里用它做门禁。
>
> 命令的完整参数表、JSON schema、字段级定义请查 [参考手册](skill-eval-reference.md)；评测方法论的理论依据见 [方法论溯源](skill-eval-principles.md)。本手册负责把"为什么这么设计 + 怎么用 + 怎么看结果"讲透，需要查具体参数时再回参考手册。

---

## 目录

- [第一章 建立直觉：skill-eval 在做什么](#第一章-建立直觉skill-eval-在做什么)
- [第二章 静态体检：不跑 Claude 先看 skill](#第二章-静态体检不跑-claude-先看-skill)
- [第三章 考卷：出题、体检、冻结](#第三章-考卷出题体检冻结)
- [第四章 动态评测主流程：run](#第四章-动态评测主流程run)
- [第五章 评分引擎](#第五章-评分引擎)
- [第六章 盲比较](#第六章-盲比较)
- [第七章 遵循性与 audit](#第七章-遵循性与-audit)
- [第八章 校准](#第八章-校准)
- [第九章 run 过程与产物解析](#第九章-run-过程与产物解析)
- [第十章 重跑归档](#第十章-重跑归档)
- [第十一章 门禁 gate](#第十一章-门禁-gate)
- [第十二章 report HTML 布局](#第十二章-report-html-布局)
- [第十三章 路径 1：真任务模式（live）](#第十三章-路径-1真任务模式live)
- [第十四章 batch / batch-report](#第十四章-batch--batch-report)
- [第十五章 触发准确度（trigger-accuracy，第 4 维）](#第十五章-触发准确度trigger-accuracy第-4-维)
- [附录](#附录)

---

## 第一章 建立直觉：skill-eval 在做什么

### 1.1 先说清楚：一个 skill 是什么

一个 Claude Code Skill 不是一个会被"加载"的插件，而是**一份给模型看的操作手册**：一个 `SKILL.md`（写明什么时候该用这个 skill、按什么步骤做、有什么约束），加上可选的 `references/`（参考文档、脚本、schema）和 `scripts/`（可执行工具）。

当模型决定使用某个 skill 时，它的正文被拼进发给 Claude 的 prompt 里，references 里的文件被拷进工作区供模型按需读取。所以 skill 的"好坏"本质上就是：**这份手册能不能让模型在真实任务里做得更好、且按规矩做**。

### 1.2 "skill 好不好"其实是两个独立的问题

很多人会把它混成一回事，但它们是正交的：

| 问题 | 含义 | 谁来回答 |
|------|------|----------|
| **能力**：加了 skill，输出变好了吗？ | 同一个任务，开/关 skill，结果差多少 | 评分引擎（scoring）+ 盲比较 |
| **过程**：模型有没有真的按 SKILL.md 做？ | 输出即使对了，过程是否遵循指令 | 遵循性引擎（compliance / audit） |

为什么非要拆开？因为存在两种危险情况：

- **输出对了，但过程没遵循**——模型自己蒙对了答案，并不能归功于你的 skill。这种"高分"不可信。
- **输出错了，但过程是对的**——skill 的逻辑没问题，只是执行失败，改指令即可，不用推翻 skill。

skill-eval 的三层架构正是为了把这两件事分别测清楚，再由"校准"层把它们的矛盾**暴露**出来。

### 1.3 三层架构 + 盲比较 + 门禁：一张全景图

```
┌─────────────────────────────────────────────────────────────┐
│                    Skill 质量评估                            │
├─────────────────────┬────────────────┬──────────────────────┤
│  评分 Scoring        │ 遵循性          │ 校准 Calibration      │
│                     │ Compliance     │                      │
│  输出对不对？         │ 过程遵不遵循？  │ 结果×过程协不协调？    │
│                     │                │                      │
│  • 客观 programmatic │ compliance.json│ calibration.json     │
│    → grading.json   │                │  读「评分 × 遵循性」   │
│  • 主观 盲比较        │                │                      │
│    → comparison_    │                │                      │
│      summary        │                │                      │
└──────────┬──────────┴────────────────┴──────────────────────┘
           │ 客观 delta + 主观胜率（盲比较），都来自评分层
           ▼
      gate 门禁（post-hoc 判决，CI 用）
      读评分两轴定 decision；校准 HIGH_SCORE_SUSPECT 作 soft warning（不进 decision）
```

> 盲比较是评分层的主观侧主信号（对比两侧 outputs），不是独立第四层；校准读「评分 × 遵循性」交叉；gate 读评分两轴（客观 delta + 主观胜率）定 decision + 校准 HIGH_SCORE_SUSPECT 作 soft warning（不进 decision）。

三个关键设计原则贯穿全书，先记住：

1. **三层是并列独立信号，不合成单一总分**。不存在一个"综合 Score"公式。消费方按需组合判断——这避免了用一个数字掩盖问题。
2. **客观与主观分开报（双率）**。能程序化验证的（文件在不在、内容匹不匹配）归"客观"，零 LLM 成本、不可作弊、噪声为零；需要语义判断的归"主观"，会抖。把它们分开，干净信号才不会被阅卷噪声污染。
3. **判决 post-hoc 出，不和产数据耦合**。`run` 只负责产数据，通过/失败由独立的 `gate` 命令判——改阈值不用重跑 Claude。

### 1.4 三条评测路径：怎么跑 × 信号源

skill-eval 不是一条路，而是三条，按"怎么产生 Claude 的执行"和"信号从哪来"两个维度组合（编号 3/2/1 沿用内部历史命名，**不是重要度排序**——主路反而是路径 3）：

| 路径 | 怎么跑 | 信号源 | 对应命令 |
|------|--------|--------|----------|
| **路径 3（主路）** | 用 fixture 剧本 spawn Claude | 重新跑出来的新 trace | `run`（默认 A/B） |
| **路径 2** | 不跑，对账已有会话 | 历史 transcript | `audit` |
| **路径 1（live）** | 在真环境里跑真任务 | 新产的真实 trace | `run --task --cwd` |

- 路径 3 是日常主路：你出一份考卷（evals.json），skill-eval 让 Claude 在两种配置下各答一遍——同一 skill 开/关对照（with/without），或两个版本对照（v1/v2），评分 + 查遵循性 + 校准，聚合出 benchmark。
- 路径 2（audit）不重跑，直接拿真实会话的 transcript（**可一份或多份**，多份做指令级聚合）跟 SKILL.md 对账。它回答两类问题：① 这一段执行有没有按 skill 做（审一次会话）；② 多段真实会话聚合后，这个 skill 在实际使用中遵循得怎么样——反过来反映 skill 的指令设计清不清晰、容不容易被遵循。
- 路径 1（live）spawn 执行器（默认 `cc`，可选 `opencode`，见第十三章）在真实代码库/数据目录里跑一个真任务，产出的 transcript 既能直接看，也能喂给 audit。它和路径 2 闭环：路径 1 产 transcript，路径 2 审 transcript。

这三条路的**遵循性判定引擎是同一套**（`TranscriptAuditor`），只是喂给它的 transcript 来源不同。

> **全套命令按工作流阶段归类**（平铺速查见附录 A）：
>
> | 阶段 | 命令 | 干什么 | 详 |
> |---|---|---|---|
> | 静态体检（不跑 cc） | `analyze` / `vet` | skill 本身 11 项体检 + 4 扫描器安全审查 | 第 2 章 |
> | 出题 | `init` / `add` / `inspect` / `freeze` | 生成 / 追加 / 体检（不下判决）/ 冻结基线 | 第 3 章 |
> | 跑测 | `run` / `run --task --cwd` | A/B 对照（路径 3）/ 真任务（路径 1） | 第 4、13 章 |
> | 判决 | `gate` | post-hoc 门禁（CI 用，毫秒级） | 第 11 章 |
> | 对账 | `audit` | 历史 transcript 遵循性（路径 2） | 第 7 章 |
> | 看结果 | `report` | HTML 报告（读 benchmark/gate/previous） | 第 12 章 |
> | 批量 | `batch` / `batch-report` | 多 skill DAG 并行 / 批量对比 | 第 14 章 |
>
> 典型顺序：`analyze`/`vet`（skill 健不健康）→ `init`/`inspect`/`freeze`（出一份好考卷）→ `run`（跑测产 benchmark）→ `gate`（出判决）+ `audit`（查过程）→ `report`（看全貌）。前三步治出卷噪声 C，`run` 内治答题噪声 B，评分时治阅卷噪声 A（见 1.6）。

### 1.5 双率与双主位

这是理解后续所有数字的钥匙。

**双率**：评分时每条被判项都按**判定方式**带一个 `source`——`programmatic`（程序判定）或 `llm`（LLM 语义判定）。大致对应：`expectations`（自然语言预期语句）一律 LLM 判；`assertions`（结构化断言）主要由程序判（Tier 1 类型，如 `FILE_EXISTS`），少数语义类型（Tier 3，如 `LLM_GRADE`）仍由 LLM 判（第 5 章细讲）。汇总时按 source 分桶，得到三个率：

| 率 | 来源 | 稳定性 | 信任度 |
|---|---|---|---|
| `pass_rate_programmatic` | 确定性断言 | 稳（噪声为 0） | 强信 |
| `pass_rate_llm` | LLM 语义判定 | 抖（阅卷噪声） | 看方向 |
| `pass_rate`（总率） | 全部混算 | 历史兼容 | 粗略 |

**双主位**（fixture 评测下，两个侧面各信哪个）：

- **客观侧主位** = `delta.pass_rate_programmatic`（候选 − 基线；**候选 = 开 skill 那侧 / v2，基线 = 关 skill 那侧 / v1**）。A/B 两边跑同样的题，程序化判定的噪声相互抵消到 0，所以这个 delta 最稳——它回答"skill 到底让客观正确率涨了多少"。
- **主观侧主位** = 盲比较胜率。同一个 LLM 同时看两份产出判谁更好，相对判断消掉了独立打分时的阅卷偏差。

`gate` 门禁正是按这两轴判决：客观侧读 `delta.pass_rate_programmatic`，主观侧读盲比较胜率。后面会展开。

> 遵循性**不分** source 双率——它按指令的语义类别（条件性/禁令/步骤/输出格式）判，产的是**遵循性五态**计数（`PASS`/`FAIL`/`N/A`/`UNRESOLVED`/`INDETERMINATE`，见第七章），不存在 programmatic/llm 分桶。双率是评分侧的概念。注意：这里的"遵循性五态"和评分侧的"评分五态"（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`，见 5.4）是**两套不同的状态集**，别混。

### 1.6 三层噪声，各治各的

到这里你可能会问：为什么 skill-eval 要分 programmatic、盲比较、`--runs`、inspect 这么多东西？因为从出题到打分，噪声不是一个地方抖，而是**三层**，分布在三个环节，各自由不同机制治。先记住这个分工，后面每一章都在治其中一层。

| 噪声 | 在哪个环节 | 长什么样 | 谁治 |
|------|-----------|---------|------|
| **A 阅卷噪声** | 打分（LLM 判分时） | 同一个判官今天判 PASS、明天判 FAIL；或整批偏严/偏松 | 客观题交给代码（`programmatic`，噪声归零）+ 主观题盲比较（相对判断，系统偏差相减抵消） |
| **B 答题噪声** | Claude 答题时 | 同一个 skill + 同一道题，这次写了文件、下次只口头总结；这次写 192KB、下次没写 | `--runs N` 多跑几次取聚合 |
| **C 出卷噪声** | init 出题时 | evals.json 本身是 LLM 生成的，会抖：这套题断言全太松 → 两边假高分；出题器漏抽 skill 该用的工具 → 考卷没覆盖关键点 | `inspect`（题集体检）+ `freeze`（冻结同一份基线，跨 run/CI 复用）+ `--from-transcript`（从一份真实好会话挖题，源头降噪） |

三个要分清的点：

- **A 和 B 是两回事，别混**。`--scoring programmatic` 消的是阅卷噪声 A（LLM 改卷波动），`--runs 3` 消的是答题噪声 B（Claude 每次产出不一样）。所以 `programmatic` + `runs 3` 才是稳的组合，缺一不可——光选 programmatic 不多跑，Claude 这次答对、下次答错，客观率照样跟着波动。
- **C 藏在 benchmark 之外**。A、B 都在 `run` 内部，能测、能消；C 是出题环节的偏差，`run` 里看不出来——两边跑同一份"坏卷子"，delta 照样漂，你却以为稳了。所以 C 只能在出题时防。它有两面，三个手段各治一面：这套题**每次 init 出得不一样**（逐套抖动）→ `freeze` 锁定一份基线跨 run 复用；这套题**本身可能不真实、断言太松**（单套偏差）→ `inspect` 体检发现问题，或 `--from-transcript` 干脆从一份真实好会话挖题（场景真、断言从真实产物里抽，不让 LLM 凭空捏），从源头避开。
- **每一章都在治其中一层**：第 5 章 programmatic（治 A）、第 6 章盲比较（治 A 里独立打分抹不掉的系统偏差）、`--runs`（治 B）、第 3 章 inspect/freeze（治 C）。

---

## 第二章 静态体检：不跑 Claude 先看 skill

静态分析零 API 成本、不到 1 秒完成，是评测的第一道关——在花 token 跑 Claude 之前，先看 skill 文件本身写得怎么样、有没有安全隐患。

### 2.1 analyze：11 项静态体检

> 11 项检查源自 skill-benchmark 的验证规则，转成了可执行检查函数（出处见 [方法论溯源](skill-eval-principles.md)）。

```bash
skill-eval analyze path/to/skill/                    # 表格输出
skill-eval analyze path/to/skill/ --format json -o scorecard.json
```

11 项检查分四组（前 6 项查 SKILL.md 顶部 **frontmatter**——即文件开头 `---` 包围的 YAML 段，里面的 `name` / `description` 字段；后 5 项查正文 / `references/` 目录）：`description`（3 项）、`name`（3 项）、SKILL.md 正文（4 项）、`references/`（1 项），每项 pass/fail，汇总成 `score / max_score`（满分 11）。逐条：

| # | 检查项 | 判定逻辑 | 为什么重要 / 不过怎么办 |
|---|--------|----------|--------------------------|
| 1 | `description_length` | frontmatter 的 description ≥ 20 字符 | 太短模型抓不到"何时该用这个 skill"；补具体动作描述 |
| 2 | `description_has_trigger` | description 含动作触发词（中英：create/生成/分析/重构/配置/部署…） | 没动作词=没说清做什么；动词开头 |
| 3 | `description_third_person` | description 不含第一人称（I / 我会 / 我的 / 我们…；用词组匹配，"自我"这种不会误伤） | 描述应客观陈述技能而非自述；去掉"我" |
| 4 | `name_kebab_case` | name 是小写字母+数字+连字符、首尾为字母或数字 | 命名规范才便于引用；改成 kebab-case |
| 5 | `name_max_64` | name ≤ 64 字符 | 太长在 UI / 调用时被截断；缩短 |
| 6 | `name_no_reserved` | name 不在保留字集（help / init / run / eval / test / skill / create / list / config） | 保留字会和系统命令冲突；换名 |
| 7 | `skill_md_max_500_lines` | SKILL.md ≤ 500 行 | 太长模型读不完、抓不住重点；详情拆进 references/ |
| 8 | `has_examples` | 正文含 example / 示例 / 例如 / 代码块 等 | 没示例模型难对齐预期；加一段用法示例 |
| 9 | `reference_depth` | references/ 扁平（无子目录；没有该目录算过） | 子目录模型可能漏读；保持一层 |
| 10 | `anti_fabrication` | 正文含反捏造词（never fabricate / 不得编造 / 不臆造…，中英词表与遵循性提取器共用） | 显式约束防模型幻觉；写明反捏造指引 |
| 11 | `source_documented` | 正文含来源词（reference / 参考 / 依据 / 出处…） | 标注信息来源可追溯；写明依据 |

> 第 10 项的反捏造词表和遵循性引擎（第七章）的指令抽取**共用同一份词表**（`skill_eval.i18n`）——所以一个 skill 过了静态反捏造检查，它的反捏造指令也大概率会被遵循性引擎抽出来查。两层的判定口径一致。

每项产出 pass/fail，汇总成 `score / max_score`，还会给一个分类标签：

- `CAPABILITY_UPLIFT`——技能注入新能力（教模型做原本不会的事）
- `ENCODED_PREFERENCE`——编码某种偏好/规范（让模型按特定方式做本来就会的事）

`score < max_score` 时 exit 1，方便接 CI。

### 2.2 vet：安全审查

> 安全审查源自 skill-vetter 的扫描逻辑（secrets-scan + structure-check），以原生 Python 重实现（出处见 [方法论溯源](skill-eval-principles.md)）。

```bash
skill-eval vet path/to/skill/                # 默认表格，-o 写文件
skill-eval vet path/to/skill/ --skip-external # 只跑内置 2 个扫描器
```

四个扫描器，两个内置开箱即用、两个可选增强：

| 扫描器 | 类型 | 查什么 |
|---|---|---|
| secrets-scan | 内置 | 硬编码 API Key / Token / 凭证、Base64 |
| structure-check | 内置 | 缺 SKILL.md / 缺 frontmatter / 危险 shell 命令 |
| aguara | 可选（Go） | Prompt 注入、混淆 |
| skill-scanner | 可选（pip） | 已知恶意模式、CVE |

判定三态：`SAFE`（全过，exit 0）/ `REVIEW_NEEDED`（有中等风险，exit 0）/ `BLOCKED`（有严重风险，exit 1）。

> `run` 命令默认会在末尾自动产出 `scorecard.json` 和 `vet-report.json`（可用 `--no-static` / `--no-vet` 跳过），所以你不用单独跑这两步也能在 report 里看到静态体检结果。

---

## 第三章 考卷：出题、体检、冻结

动态评测的前提是有一份"考卷"——`evals.json`。这一章讲怎么造卷、怎么体检、怎么冻结成基线。

### 3.1 evals.json 长什么样

一份考卷就是一个 JSON，里面是若干道 eval（题）。每道题的核心字段：

```json
{
  "id": 1,
  "prompt": "把 data.csv 按日期排序后输出 sorted.csv",
  "rationale": "对应 ## 步骤(按日期升序排序后产出 sorted.csv)",
  "confidence": 0.85,
  "expected_output": "生成 sorted.csv，内容按日期升序",
  "level": "L2",
  "fixtures": ["data.csv"],
  "expectations": ["输出文件存在", "内容按日期升序"],
  "assertions": [
    {"name": "文件存在", "type": "FILE_EXISTS", "params": {"filename": "sorted.csv"}},
    {"name": "含排序逻辑", "type": "CONTAINS_PATTERN", "params": {"pattern": "sort", "path": "sorted.csv"}}
  ],
  "source_dir": "src",
  "flags": []
}
```

几个关键概念：

- **L1 vs L2 是题型，不是"有没有 expectations"**。L1（单元级）测模型对 skill 规则的理解对不对，通常纯推理、无 fixture；L2（Mock 场景）测在有 fixture 的场景里能不能正确产出文件。两者都能同时有 expectations 和 assertions。
- **expectations vs assertions 是两个判分层**。`expectations` 是自然语言预期语句，由 LLM 判；`assertions` 是结构化程序化断言（类型 + 参数），由程序判。一道题可以只有其一，也可以都有。
- **expected_output 是给阅卷参考的，绝不喂给被测 Claude**。把它拼进 prompt 等于把答案泄露给考生，A/B 对照会失真。skill-eval 只在评分侧用它。
- **fixtures** 是前置文件（如输入数据），会被拷进工作区。
- **source_dir** 是 eval 自带的额外目录（如 skill 的 `scripts/`、被处理的样例代码），会和 fixtures 一起拷进工作区——skill 依赖、但不在 `references/` 里的东西靠它带进考场（见 4.3 的依赖拷贝坑）。
- **rationale / confidence**（`init` 生成时自带）：每道题带 `rationale`（对应 SKILL.md 哪段/哪条,审阅溯源抓手）和 `confidence`（0-1,生成器自报把握）。`init` 终端逐条打印这俩,方便审阅"这题到底在考什么、生成器多有把握"。两者都**不入评分 gate**（confidence ≠ audit-finding 的 confidence,别混淆）;`rationale` 缺失打 `review:no-rationale` flag 提示人工补。
- 完整字段（`timeout` / `flags` 等）见参考手册和 [evals-format.md](evals-format.md)。

### 3.2 init：自动出题

```bash
skill-eval init path/to/skill/                                  # LLM 生成（默认）
skill-eval init path/to/skill/ --num-evals 8                    # 限制数量
skill-eval init path/to/skill/ --from-transcript session.jsonl  # 从真实会话挖题
```

`init` 有三种生成模式，按优先级自动选择：

1. **`--from-transcript`（从真实会话挖）**：读一段真实 session transcript，重建当时的输入文件/命令/产物，挖出真实场景的 eval。其中有一条是**逐字节真挖**——直接还原会话里真实执行的任务，fixture 就是 agent 当时 Read 的真实文件内容；此外 LLM 还会基于这段会话再生成若干变体题。为防 LLM 臆造文件名，生成结果过一道**反幻觉闭集约束**：允许引用的文件被限定在一个闭集里（transcript 实际出现的输入/产物文件 + SKILL 声明的产物），引用闭集外文件的断言和 fixture 会被确定性后处理丢弃并打 `review:fixture-not-in-manifest` / `review:dropped-assertions` flag——这样生成的题不会冒出会话里根本不存在的文件。如果 transcript 里调了多个 skill，加 `--kind skill` 只挖本 skill 被调用期间的段（否则会串味，终端会警告）。
2. **`--generate-prompts`（LLM 生成，默认开）**：LLM 读 SKILL.md 自动出 L1/L2 题，并尝试推导程序化断言。
3. **`--no-generate-prompts`（纯模板，不调 LLM）**：不调 LLM，纯规则从 SKILL.md 结构推导——把正文按 `##` 二级标题切成若干段，每段出一道 eval：prompt 由标题 + 描述里检测到的动作词拼成（如 `Convert a task related to <段> — …`），`expectations` 从段里的列表项（`-` / `*` / `1.`）逐条转成预期语句，`expected_output` 形如 `Successfully complete the <段> workflow (N steps)`。**"模板"不是磁盘文件**，而是 `evals/generator.py` 里的一套确定性推导规则（`_derive_prompt` / `_derive_expectations` / `_derive_assertions`）；唯一可配的"模板"是配置文件里按 domain 注入的领域断言（`domain_templates`）。**产出的不全是程序断言**——主体是 `expectations`（LLM 判的文本预期），程序断言（`FILE_EXISTS` / `CONTAINS_PATTERN` 等）只是从代码块 / import / 领域模板**抽得到才补**，纯规则类 skill 可能零断言。

挖题/生成失败时（LLM 超时 / 限流 / 退出码，或返回了但解析不出结构化 eval）不会静默产空，而是产一个**兜底 eval**：prompt 退到最朴素形态（transcript 挖题用真实 user turns、LLM 生成退到模板），`assertions` 往往为空，并打 `degraded:*` flag（transcript 挖题是 `degraded:no-llm`）。`init` 检测到后黄色告警——`⚠️ N 个 eval 为 degraded fallback(LLM 失败,未挖到结构化信号,assertions 可能为空):`，逐条列出哪些 eval + flag，并提示`建议重跑,或检查 claude CLI 可用性`。这样既不因一次失败就产不出题，也不假装成功。

**字段名拼错也会告警**：LLM 生成时如果把字段名写错（如 `expected_output`→`expected`、`assertions`→`assertion`），pydantic 默认会**悄悄丢掉**该字段——考卷看着生成成功、实则缺那块，`run` 时才暴雷。`init` 会校验 LLM 输出的字段名，发现不认识的就告警 `⚠️ N 条 eval 含未知字段(已忽略;可能拼错)`，逐条指出哪条 eval 的哪些字段可疑，免得拿着残缺考卷还以为没问题。同样 warn 不拒（拒绝会掉进纯模板兜底，反而更差）。

### 3.3 add：追加单道题

```bash
skill-eval add pdf/ --idea "加密 PDF 用错密码应失败" --level L2   # 一句话 → LLM 生成完整 eval
skill-eval add pdf/ --prompt "..." --expectation "..." --level L2  # 手动模式
skill-eval add pdf/ --idea "..." --dry-run                          # 预览不写入
```

`--idea` 模式下自动分配 id、自动推导 assertions 和 fixtures。

### 3.4 定向补薄弱面：`--focus` + `--append`

`init` 一次性生成整套时，LLM 有个系统性盲区——**倾向正面题 / happy path**，容易漏反面场景（skill 不该被触发的请求）、边界 / 错误处理（空输入、超大、损坏、异常）。`init --focus` + `--append` 帮你定向补这些盲区，且不丢之前生成 / 手改的题：

```bash
skill-eval init path/to/skill/ --focus negative-cases --append  # 补反面场景,追加到已有考卷
skill-eval init path/to/skill/ --focus edge-cases --append      # 补边界/错误处理
skill-eval init path/to/skill/ --focus "加密/损坏 PDF" --append # 自由文本:skill 特定方向
```

- **`--focus <类>`** 决定这次生成什么：预设 `negative-cases`（skill 不该被触发的反面场景）/ `edge-cases`（边界 / 错误处理）覆盖最常见的通用盲区；也接受**自由文本**（任意描述一个 skill 特定方向，如"加密 PDF""超大日志"），补通用类够不到的薄弱处。它往 prompt 注入一段定向指令，强制 LLM 进入反面 / 边界思维。（`anti-triggers` 是旧名，仍作静默别名兼容。）
- **`--append`** 决定怎么写：追加到已有 evals.json（新 eval 的 id 接续已有 `max+1`、不覆盖旧题）。不带 `--append` 则默认覆盖（首次出题或想重练一套时用）。

**为什么用这个而不是 `add`**：`add`（3.3）是手动一道道加，得你自己想反面场景；`--focus` 让 LLM 在定向模式下批量生成一类，省掉手想的力气——尤其补的是你**没意识到的**盲区（通用预设类主动提示）。两者互补：`--focus` 批量铺面，`add` 精修单道。

> `--focus` 只对 LLM 生成路径（默认 `init`）生效；`--from-transcript` 是从真实会话挖题，不适用 `--focus`。补完别忘了 `inspect`（3.5）体检、`freeze`（3.6）冻成基线再 `run`。

### 3.5 inspect：题集体检（不下判决）

```bash
skill-eval inspect path/to/evals.json        # 表格
skill-eval inspect path/to/evals.json --json  # 机器可读
```

注意位置参数是 **evals.json 路径**（不是 skill 目录）。`inspect` 对已有考卷做静态体检：完整性、稳定性（programmatic/LLM 占比）、潜在问题 flag。它**不执行、不调 LLM、不下判决**——只报指标和关注点，帮你决定"这份卷子能不能冻结成基线"。

### 3.6 freeze：冻结基线

```bash
skill-eval freeze path/to/evals.json                         # → baseline.json + baseline.meta.json
skill-eval freeze path/to/evals.json --name v1 --skill-version 1.2
```

`freeze` 把考卷冻成命名快照 + 溯源 sidecar，供 CI / 跨 run 复用同一份题。产出的仍是标准 evals 格式，loader 零改动；同名覆盖（幂等）。

这里有一个贯穿全工具的设计原则值得记住——**机制不策略**：skill-eval 只提供"冻一份题、跨 run 复用"的机制；要不要把 baseline.json commit 进 git、门禁阈值定多少、CI 里 N/A 阻不阻塞，都归消费方决定。工具不替你做这些策略选择。

---

## 第四章 动态评测主流程：run

`run` 是最核心、也最重的命令。它把 Claude 真的跑起来、评分、查遵循性、校准、聚合，是纯数据生产者。

### 4.1 A/B 对照：回答"skill 到底有没有用"

`run` 的默认模式是 A/B 对照：同一份考卷，让 Claude 在**开 skill**（with_skill）和**关 skill**（without_skill）两种配置下各跑一遍，比较差异。

为什么要对照？因为模型本身就有能力。如果只跑开 skill 看通过率是 85%，你不知道这 85% 是 skill 带来的、还是模型本来就会。只有和"关 skill"的基线对比，delta 才是 skill 的真实贡献（处理效应）。这本质上是随机对照实验（RCT）。

两种对照方式：

- **默认 with/without**：同一个 skill，开/关。位置参数就是 skill 目录。
- **v1/v2 双 skill**：给 `--skill-b <另一个 skill 目录>`，两边各注入各自的 skill，标签默认取目录名。适合回归对照（候选 v2 vs 基线 v1）。

```bash
skill-eval run path/to/skill/ --runs 3                       # with/without，跑 3 轮
skill-eval run v2/ --skill-b v1/ --evals baseline.json        # v1/v2 回归对照（baseline.json 由 freeze 产生，见 3.6）
skill-eval run path/to/skill/ --scoring programmatic          # 仅程序化评分，零 LLM 成本
```

> 下文把两个配置（with_skill / without_skill，或 v1/v2）统称为 **side**。
>
> run 默认会在末尾自动跑静态体检 + 安全审查（产出 `scorecard.json` / `vet-report.json`，可 `--no-static` / `--no-vet` 跳过）。还有一个安全相关开关 **`--security-gate`**：开启后，若 vet 判 `BLOCKED` / `REVIEW_NEEDED`，run 直接退 1（否则 run 的退出码只表示产数据成败，通过/失败由 `gate` 判，见附录 exit code 表）。

### 4.2 选题：全量 / 抽样 / 指定某几道

run 默认跑 evals.json 的**全部题**。题多时全量耗时耗 token（每道 × runs × sides × Claude 调用），开发迭代常不需每次全量。四种选题策略：

- **全量（默认）**：跑全部题——完整评估，正式评测用。耗时最长。
- **`--eval-samples N` 或 `pct`**：**随机抽样** N 道或 pct% 道（如 `--eval-samples 10` 或 `0.25`）——快速估算整体表现，但**不可控选哪几道**（随机）。
- **`--eval-ids 3,5,7`**：**精确指定**某几道（按 eval.id，逗号分隔）——调试某道 fail、回归某道、重现某道行为。和 `--eval-samples`/`--changed-from` **三选一互斥**。
- **`--changed-from <baseline 目录>`**：**自动判受影响**——改 skill 后给基线目录，自动 diff 两边 SKILL.md 指令 + LLM 判哪些 eval 受这次改动影响、只跑那些（**回归探针**）。和 `--eval-samples`/`--eval-ids` **互斥**。

```bash
skill-eval run path/to/skill/ --eval-samples 10              # 随机抽 10 道估整体
skill-eval run path/to/skill/ --eval-ids 3,5                 # 只跑 id=3、5（调试/回归某几道）
skill-eval run path/to/skill/ --changed-from v1/             # 改 skill 后只跑受影响的题（给基线目录）
```

**为什么 ids 和 samples 互斥（不是叠加）**：指定了 id 又随机抽样会矛盾——"我选了第 3、5 道却又被随机抽掉"。语义清晰起见，同给报错；要减量就二选一（随机估整体用 samples / 聚焦某几道用 ids）。

**id 不存在报错（不静默）**：`--eval-ids 99`（evals.json 没这题）直接报错并列出可用 id，不静默漏跑——id 是精确指定，笔误该立刻发现（不像 `--eval-samples` 格式错静默当全量：那个随机抽样，笔误当全量可接受）。保序去重（`--eval-ids 3,3,5` 跑 3、5 两道，按给定顺序）。

**何时用哪个**：正式评测 → 全量；快速看 skill 大致水平 → `--eval-samples`；调试/回归/重现某道 → `--eval-ids`；**改 skill 后回归受影响的几道 → `--changed-from`**（省全量，但子集 delta 非整体测量，周期仍需全量兜底）。

> `--changed-from` 是**回归探针，不是代表性测量**：它只跑 LLM 判定受改动影响的子集，聚合 delta 在偏置子集上算、不代表整体质量；判定靠 LLM（偏过召：拿不准就带），不对劲可用 `--eval-ids` 覆盖。判定失败（调用/解析/返回不存在 id）报错退出；判定空集（零受影响）合法、跑空退出。周期性仍跑一次全量拿真信号。

> **想要更可控的判定？** `--changed-from` 现在靠 LLM 每次自动判受影响 eval（够用，但每次重判、不保证确定）。若需要更精确、可复现的判定，一个思路是**手工给每道 eval 标注它考的是 skill 的哪条指令**（存一张 eval↔指令对应表），改 skill 后直接查表得出受影响 eval——确定性、可审计，但这张表只有人工逐条校对维护时才比自动判定强；若也让 LLM 自动建、没人管，准度和自动判定（同一个 LLM 判断）一样，却白搭维护复杂度——权衡后当前未采用。自动判定不敷用时再考虑。

> 路径1 真任务模式（`--task`）不读 evals.json、没有 id 概念，`--eval-ids`、`--eval-samples`、`--changed-from` 对它都无效（见第十三章）。

### 4.3 一次 run 的完整流程

一道 eval 在**注入了 skill 的 side** 上跑时，背后走 7 步——with/without 模式只有 with_skill 侧满足，v1/v2 模式两边都满足：

1. **准备工作区**：拷贝 fixtures、所注入 skill 的 references、`source_dir` 进去。
2. **拼 prompt**：把 skill 正文拼进发给 Claude 的 prompt（见 4.3）。
3. **spawn Claude**：`claude -p` 跑这道题，产出 `outputs/`（Claude 生成的文件）+ `transcript.jsonl`（完整会话）。
4. **评分（grading）**：跑程序化断言 + LLM 语义判分，产 `grading.json`（双率）。
5. **遵循性（compliance）**：从 SKILL.md 提取指令，对照 transcript/tool_trace 查，产 `compliance.json`。**只要这个 side 注入了 skill 就跑**——所以 with/without 模式只有 with_skill 侧产，v1/v2 模式两边都注入了各自的 skill、**两边都产**（各自对账自己那份 SKILL.md）。
6. **校准（calibration）**：交叉评分与遵循性，产 `calibration.json`。门控同上（有 compliance 才产）。
7. **聚合**：所有 run 跑完后，聚合成 `benchmark.json` + 盲比较 + 深度分析 + report.html。

> with/without 模式下，without_skill 侧没注入 skill、谈不上遵循，只跑前 4 步 + 聚合（无 compliance / 无 calibration）；v1/v2 模式两边都有 skill，7 步全跑。

评分里若出现 **UNEVAL（评不出来）**——某条 expectation/assertion LLM 判不了（transcript 截断 / 解析失败 / 信息不足），区别于 FAIL（确定错）——会触发自动重试（`--max-retries`，默认 2），重试目录形如 `run-1-retry-1`。**重试有上限、不是无限重试到评出来为止**：耗尽次数仍 UNEVAL 就原样落盘，所以最终 grading 不保证全 PASS/FAIL，可能仍含 UNEVAL（以及 TIMEOUT/ERROR）。

### 4.4 skill 怎么"注入"：拼进 prompt，不是加载

skill 不是一个运行时加载的模块。skill-eval 做的是：

- 把 `SKILL.md` 正文**拼进**发给 `claude -p` 的 prompt；
- 把 skill 的 `references/`、`scripts/` 等拷进工作区，让模型可以按 SKILL.md 里写的"读 references/foo.md"真的读到。

所以"模型用了 skill"≈"模型读到了这份手册并照着做"。这也是为什么 SKILL.md 的写法直接决定效果——它就是模型看到的全部指令。

> 一个常见坑：**references/ 在 skill 注入时会自动拷进工作区**（无需配置）；但 skill 依赖的 `scripts/` 等其它目录**不会**自动拷——若 SKILL.md 让模型运行 `scripts/foo.py` 或读取 references 以外的文件，得在 eval 里用 `source_dir` 把它拷进来，否则模型反复找不存在的文件直到 timeout，效果从"有增益"掉回"和不开一样"。看到 with/without delta 接近 0 时，先排查依赖目录进没进工作区。（客观 Δ 趋零时 viewer/batch 会主动标 ⚠ 提醒——但 ⚠ 只是「无增益」信号，首选按这里排查依赖这一常见假阳性；排除依赖后，才考虑 skill 是否已被模型原生能力覆盖、该收窄 scope。）

### 4.5 产物目录树

```
output/
├── benchmark.json          # 聚合：双率 delta + 盲比较 + 深度分析
├── report.html             # 自包含 HTML 报告
├── gate.json               # gate 命令产出（非 run 产出）
├── scorecard.json          # 静态评分卡（run 末尾自动产）
├── vet-report.json         # 安全扫描（run 末尾自动产）
├── previous/               # 重跑归档：fresh 重跑时上次整棵树移入（单槽）
├── with_skill/             # v1/v2 模式换成各自标签名（两边都注入 skill → 两边都有 compliance + calibration）
│   └── eval-<id>/run-<n>/
│       ├── outputs/        # Claude 产生的文件
│       ├── grading.json    # 评分（双率）
│       ├── compliance.json # 遵循性（五态 + method）
│       ├── calibration.json# 结果×过程校准
│       ├── run_debug.json  # 完整回复 + tool_calls + timing
│       └── timing.json     # 耗时和 token
└── without_skill/
    └── eval-<id>/run-<n>/  # 同上，但无 compliance / 无 calibration
```

各文件的字段级含义见[第九章](#第九章-run-过程与产物解析)和参考手册。

> `run` 是纯数据生产者——exit 0 只代表产数据成功，**通过/失败判决由 `gate` 出**，不再由 run 的退出码决定（旧版读混合 delta 退 1 的逻辑已退役）。

---

## 第五章 评分引擎

评分回答"输出对不对"。它分两个判分层，结果汇成双率。

> **Tier 是断言的能力分级，只有两档**：Tier 1（确定性，程序判定）和 Tier 3（语义，LLM 判定），**没有 Tier 2**。编号沿用 skill-creator（Anthropic 官方的 skill 脚手架工具，skill-eval 沿用了它的一些命名）的分级传统——原体系里 Tier 2 是同属"程序/客观"侧的另一档（skill-eval 的 `grading_models.py` 注释还留着"programmatic(Tier1/2 客观)"字样），skill-eval 把它并进了 Tier 1（`TOOL_CALLED`/`STEP_COUNT` 这种查 tool_trace / 运行时行为的，现在就归在 Tier 1 下）。所以"跳过 2"是历史命名残留，能力光谱就是"程序判 → LLM 判"两档。

### 5.1 程序化断言（Tier 1，确定性）

这是评分里最值得信赖的部分——程序判定、零 LLM 成本、噪声为零、不可作弊。完整类型表见参考手册，常用的有：

| 类型 | 判什么 | 关键参数 |
|------|--------|----------|
| `FILE_EXISTS` | 文件存在 | `filename` |
| `FILE_CONTAINS` | 文件含子串 | `filename`, `pattern` |
| `CONTAINS_PATTERN` | 正则匹配 | `pattern`, `path`(可选) |
| `NOT_CONTAINS` | 反向匹配 | `pattern`, `path`(可选) |
| `JSON_HAS_FIELD` | JSON 字段 | `filename`, `field`（支持 `a.b.c`） |
| `LINE_COUNT` | 行数范围 | `min`/`max`(至少一个) |
| `TOOL_CALLED` | 检查工具调用（看 tool_trace） | `tool_name`, `min_count` |
| `STEP_COUNT` | agentic 步数范围（Claude 跑了几轮 = `num_turns`，可客观计数，故归 Tier 1） | `min`/`max` |
| `CUSTOM_SCRIPT` | 自定义脚本 | `script`, `args` |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | `plugin_name` |

设计哲学是"**能程序化查的就程序化查**"——凡是能客观验证的（文件在不在、内容匹不匹配、工具调没调），一律用断言，不浪费 LLM、也不引入阅卷噪声。

### 5.2 LLM 语义判分（Tier 3）

有些东西没法客观验证（"分析是否合理""解释是否清晰"），交给 LLM 判。类型有 `LLM_GRADE`（语义评分）、`LLM_FACT`（事实核查）、`LLM_NOT_DELEGATE`（检查模型有没有把不该委托的子任务甩出去）。LLM 判官用结构化输出 + 强制 evidence 引用，每条判定必须附证据。

`expectations`（自然语言预期语句）也走 LLM 判，归到 LLM 侧。

### 5.3 三种评分模式

`run --scoring <mode>`：

| 模式 | 说明 | 调 LLM | 适用 |
|------|------|--------|------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分 | 是 | 通用 |
| `programmatic` | 仅确定性断言 | 否 | 输出结构化、可机械验证（省钱） |
| `llm` | 仅 LLM 语义评分 | 是 | 输出需要语义判断 |

`programmatic` 模式零 LLM 评分成本，盲比较也会自动跳过（盲比较只服务主观题）。

### 5.4 双率怎么算出来

每条 expectation/assertion 判完带一个 `verdict`（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`）和一个 `source`（`programmatic`/`llm`）。`grading.json` 的 `summary` 按 source 分桶：

```json
"summary": {
  "passed": 3, "failed": 1, "total": 4, "pass_rate": 0.75,
  "passed_programmatic": 2, "total_programmatic": 2, "pass_rate_programmatic": 1.0,
  "passed_llm": 1, "total_llm": 2, "pass_rate_llm": 0.5
}
```

某类没有 expectation 时，对应字段为 `null`（不报假 0）。评分是**评分五态** verdict（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`），不是只有通过/失败：UNEVAL（评不出来，如 transcript 截断）/TIMEOUT/ERROR 在报告里与 FAIL **区分显示**（?/⏱/!，不混成红 ✗），让你分辨"没评出来"和"真错了"。**但注意 pass_rate 数值上它们仍算"未通过"**——`total` 含全部条数（含 UNEVAL/超时/出错），`passed` 只数 PASS，`failed = total − passed`，所以这些条会和 FAIL 一样拉低通过率；区分只在显示层，不在计数层。（这是**评分五态**；遵循性侧另有一套**遵循性五态** `PASS`/`FAIL`/`N/A`/`UNRESOLVED`/`INDETERMINATE`，见第七章，两套别混。）

### 5.5 cache_key：内容感知续跑

重跑同一 skill 时，skill-eval 不会盲目重跑所有题。它给每个 (eval, run) 槽算一个 `cache_key`：

```
cache_key = sha256(SKILL 正文 + eval 全身 + 模型/配置 + skill-eval 代码版本)[:16]
```

其中"skill-eval 代码版本"= skill-eval 自身源码（`src/skill_eval/**/*.py`）的内容 hash（进程内只算一次）；`[:16]` 取 sha256 十六进制串的前 16 个字符。

重跑时，只有 `cache_key` 与本次输入**完全一致**且 `summary.total > 0` 的槽才跳过重跑（省 token）。这意味着：

- 改了 SKILL.md / evals / model / scoring 配置 → key 变 → 自动失效重跑；
- 升级了 skill-eval 自身（代码版本 hash 变）→ key 变 → 重跑；
- without 侧不含 skill 正文，所以改 SKILL.md 只重跑 with 侧。

> 已知缺口：eval 引用的 `source_dir` / `references/` 文件**内容**改动不检测。改这些请用新 `-o` 强制重跑。

---

## 第六章 盲比较

盲比较（BlindComparator）是主观侧的主位信号，回答"两份产出，抛开标签，哪个更好"。

### 6.1 为什么需要盲比较

盲比较治的是 1.6 三层噪声里的**阅卷噪声 A**，而且专治其中独立打分抹不掉的那一面。

阅卷噪声 A 有两面：一面是**随机抖**（同一个判官今天判 PASS、明天判 FAIL）——这个 `--runs` 多跑几次、或客观题交给代码就能平均掉；另一面是**系统性偏差**（判官这一整批偏严或偏松），它才是麻烦：with 和 without 各自独立打分时，这个"偏严"不会在两次独立打分之间抵消，而是夹带进 delta，让你以为 skill 有贡献、其实只是判官今天手紧。

盲比较正是来补这个缺口：**同一个 LLM、同一次调用**同时看两份产出（with 和 without，或 v1 和 v2），判谁更好。同一把尺子量两份，系统性偏差在"谁更好"这个问题上自动抵消（尺子偏严也对两份一视同仁），随机抖也不再是两组独立叠加。所以盲比较比 per-run 独立打分稳——它不是因为 per-run delta 不稳才需要（`--runs 3` 下其实够用），而是结构上更稳，且结论更直观（"with 胜率 73%"比"+18%"更直接回答 skill 帮不帮）。

### 6.2 位置随机化 + 语义 winner

为了彻底消除位置偏差（LLM 可能偏好首位 / 末位），盲比较每次会**抛硬币式（50/50 随机）决定摆位**——哪边放在 A 位、哪边放在 B 位是随机的，判官只看到 position A/B，不知道背后是哪一侧。

报告怎么知道"A 赢了"是哪一侧？靠存**语义标签**而非裸 A/B：两侧一开始就各有名字 `label_a` / `label_b`（默认 `label_a` = with = 候选、`label_b` = without = 基线；v1/v2 模式 `label_a` = 位置参数那个 skill = 候选、`label_b` = `--skill-b` 那个 = 基线，标签默认取目录名）。判官返回裸的 A / B / tie 后，盲比较按**当次**的随机摆位把它翻回语义侧——这次 with 恰好摆在 A 位、判官判 A 赢 → winner 存成 `"with"`。所以 `ComparisonResult.winner` 存的就是**赢的那一侧的名字**，报告直接读它，不用自己反推 A/B 映射。

这套机制的核心是一句分工：**评判者（LLM）全程盲，编排者（代码）全程知道**。代码随机决定摆位、记住映射、最后把判官返回的裸 A/B 翻回语义侧；LLM 只看到两份抹掉身份的产出，只管判哪份好。一边用"盲"消掉 LLM 的偏见（不知道谁是谁），一边用"不盲"保住可读的结果（报告里直接是"with 赢了"）——盲而不乱。

每条 `ComparisonResult` 除了 `winner`，还携带这些字段（多数按侧拆，两侧各一份）：

| 字段 | 装什么 |
|------|--------|
| `reasoning` | 判官的判决理由（文字） |
| `rubric` | 两侧各自的打分：`content`（correctness / completeness / accuracy）+ `structure`（organization / formatting / usability）+ 汇总 `content_score` / `structure_score` / `overall_score` |
| `output_quality` | 两侧各自的 `score` + `strengths[]`（优点）+ `weaknesses[]`（缺点） |
| `expectation_results` | 两侧各自的 `passed` / `total` / `pass_rate` + 逐条 `{text, passed}` 明细 |
| `pos_a_label` / `pos_b_label` | 揭盲映射：position A / B 各自对应哪一侧（报告据此在 reasoning 旁标注，否则 reasoning 里的 A/B 和去匿名化的 winner 对不上） |

报告逐条渲染这些——既看到"谁赢"，也看到两侧各自的 rubric 分、优缺点和逐 expectation 通过情况。

### 6.3 comparison_summary 与胜率两版

盲比较聚合成 `comparison_summary`：

```json
"comparison_summary": {
  "total": 10, "a_wins": 6, "b_wins": 2, "ties": 1, "failed": 1,
  "valid": 9, "win_rate": 0.67, "label_a": "with", "label_b": "without"
}
```

这里有个容易踩的细节——**胜率有两个版本**：

| 版本 | 公式 | 用在哪 |
|------|------|--------|
| tie-**计入** | `a_wins / valid`（valid = a_wins + b_wins + ties） | `comparison_summary.win_rate` |
| tie-**排除** | `a_wins / (a_wins + b_wins)` | `gate --min-win-rate` |

两者在阈值边界会让门禁判决翻转，所以 gate 自行计算 tie-排除版，不复用 summary 字段。另外 `failed`（调用失败）排除出分母，`ties`（真平局）计入分母。

### 6.4 什么时候跳过

盲比较只服务主观题——客观题 programmatic 已经把噪声消到 0，没必要再盲评。所以在 `programmatic`-only 评分模式下，盲比较自动跳过。

### 6.5 比较对象可换：v1/v2 回归门禁是杀手级应用

盲比较的关键不是"又比了一次 with/without"——现有评分早就在比 with vs without 的 delta。它的价值是**一个稳定的比较器，而且比较对象可以换**。换比较对象，回答的问题就变：

| 比较对象 | 回答什么 |
|---------|---------|
| `with vs without` | 这个 skill 到底帮不帮 Claude 把事做好（skill 存在的意义） |
| `v1 vs v2`（同一 skill 两个版本） | 这次更新有没有正收益 |

v1/v2 是盲比较的**杀手级应用——CI 回归门禁**：skill 提 PR（v1 = main 旧版、v2 = PR 新版），CI 拉同一份冻结的 eval 基线，分别 spawn with v1 / with v2 各跑 runs N，每条 eval 盲比较两版产出，汇总 v2 胜率——v2 胜率明显高 → 正收益（CI 绿）；v1 胜 → 回归（CI 红）；大量平手 → 改了寂寞（更新没触及实际产出）。

这正是第十一章 gate 把盲比较胜率做成可选门禁轴（`--min-win-rate`）的用意：它天然适合"PR 不许回归"的 CI 用法。换 `--skill-b` 指向另一个 skill 路径就切到 v1/v2 模式（见 4.1）。

---

## 第七章 遵循性与 audit

遵循性回答"过程有没有按 SKILL.md 做"。它是 skill-eval 的原创层，也是和 skill-creator 这类工具最大的差异点。

### 7.1 为什么需要遵循性

输出对了 ≠ 过程对了。模型可能"抄近路"蒙对答案——比如 skill 要求"运行 validate.py 校验"，模型跳过校验直接给了个恰好正确的输出。单看评分，这是满分；但这次满分不能归功于 skill，因为 skill 的关键步骤被跳过了。遵循性就是把"有没有真按指令做"单独测出来。

### 7.2 6 类指令 → 4 个语义类别

遵循性判定的**出题依据是 SKILL.md**（不是 eval）。`InstructionExtractor` 从 SKILL.md 静态抽出 6 类可检查指令：

- `HEADING_STEP`（应走某步骤）
- `TOOL_REQUIREMENT`（该用某工具/运行某脚本）
- `REFERENCE_USAGE`（该读某参考）
- `CONSTRAINT`（约束/禁令）
- `ANTI_FABRICATION`（反捏造规则）
- `OUTPUT_FORMAT`（输出格式要求）

然后按**语义**归到 4 个类别，每类配方向正确的判法：

| 类别 | 来自哪些指令 | 怎么判 |
|------|------------|--------|
| 条件性 CONDITIONAL | TOOL_REQUIREMENT / REFERENCE_USAGE | 命中 tool_trace → PASS（**程序判定**）；未命中 → LLM 判该不该触发 |
| 禁令 PROHIBITION | CONSTRAINT / ANTI_FABRICATION | LLM **主动检测这次有没有违规**（不是判"遵守没"） |
| 步骤 STEP | HEADING_STEP | LLM 判走过 / 该走没走 / 不涉及 |
| 输出格式 OUTPUT | OUTPUT_FORMAT | **确定性子串匹配**（程序判定，不调 LLM） |

> **输出格式（OUTPUT_FORMAT）怎么知道匹配什么**：extractor 从 SKILL.md 抽出声明的输出格式要求（比如规定产物要含某个标题 / 标记 / 模板），audit 拿它去和实际输出做**子串匹配**——命中 → PASS，未命中 → FAIL（这类没有 N/A / 没判成 / 判不准，因为是纯字符串判定）。所以 SKILL.md 里把输出格式写得越具体（点名某个 marker），越容易被抽到、查得越准。

> 禁令的判法是**反着来**的：其它三类问"该不该做/做没做"（正面遵循），禁令问"这次执行里有没有发生**违反**这条禁令的行为"——主动找违规。例如 `NEVER fabricate error codes`，模型 hallucinate 一个错误码 → `violated=true` → FAIL。

> **判官看什么 + 两处结构性噪声的防呆**：三个 LLM 判官各看不同的执行上下文——条件性看「场景」（`digest.user_turns`，任务上下文，判指令该不该触发）、禁令 / 步骤看「执行记录」（`transcript_text`，assistant 文字 + 工具调用渲染）。两处防呆治实测的两条主噪声：① **条件性场景回退**——`--kind skill/agent` 切出来的段只有执行 turn、没有 user turn（任务在父作用域），条件性判官原拿空场景只能一律弃判 INDETERMINATE（实测占 22%–55%）；现空场景时回退用 `transcript_text`（执行本身揭示任务上下文）当场景，判官方能判 applicability 而非弃判。② **头 + 尾窗口**——执行记录远超 prompt 预算时（实测单段 60–150KB），旧裸 `[:8000]` 只留头、尾部（最终结果 / 总结 / 收尾）判官全盲 → 禁令类「未见收口」假 PASS 或弃判；现取头 + 尾各半 + 中段截断标记，token 成本不变、覆盖从单端变双端。步骤类另有完整 `tool_trace`（不截断）兜底，所以「有没有调某工具」不受截断影响。

> **extractor 的地基瓶颈**：SKILL.md 是给人读的自由文本——讲解、示例、真步骤混在一起，没有结构标记区分"这行是指令"还是"这行是示例"。extractor 抽指令只能靠正则死规则（标题下、大写开头 ≈ 步骤），于是教程型 skill 里的示例文字（如 `3 agents dispatched in parallel`、`6 failures across 3 files`）会被误抽成"指令"喂给判官 → 卷子大半是废题 → 判官再准也只能大量判 N/A（伪指令判不准）。这就是为什么遵循性常出很多 N/A、为什么教程型 skill 特别难审（路径 2/3 共用的地基问题）。根治两路：① **结构化声明**——skill 作者在 frontmatter 声明 `required_tools`/`required_reads`/`required_outputs`，extractor 直接查声明、不挖散文（最稳，但要 skill 作者配合改格式，待做）；② **refine**——正则粗召回后用 LLM 精筛伪指令（工具侧改进，不改 skill 格式），**默认开**，详见 §7.3。

### 7.3 refine：extractor 粗召回后的 LLM 精筛

§7.2 末尾提到 extractor 的地基瓶颈——它靠正则死规则从自由文本里抽指令，保召回但不分"真指令"还是"示例/占位"，于是教程型 skill 里的示例文字会被误抽成指令喂给判官，判官再准也只能大量判 N/A。refine 就是治这个的工具侧方案（不改 skill 格式）。

**两段式**：extractor 正则**粗召回**（宁可多抽、不漏真指令）→ refine 在判定前用 LLM 逐条**精筛**"这是真实可检查的指令，还是示例/占位文字"，只砍明确判为伪指令的。

**精度 > 召回**（关键哲学）：refine 只砍 LLM **明确**判 `is_real=False` 的；没判成 / 判不准 / 调用失败一律**保留**——宁可留伪指令让下游判 N/A，也绝不砍真指令。误砍一条真指令 = 漏审一条规则（不可逆），误留一条伪指令 = 多一条 N/A（可容忍）。实测误砍率 0（4 个结构差异 skill × 139 条指令，砍的是表头/占位/资源描述，留的是步骤/禁令/约束）。

**默认开**：`--refine-instructions` 默认开（`--no-refine-instructions` 关），`audit` / `run` 都支持。砍掉的条数和来源写进 report 顶部 warnings（如 `refine: 滤除 9 条疑似示例/占位伪指令(HEADING_STEP 7, …)`）。`OUTPUT_FORMAT` 不精筛（纯程序子串匹配，精筛无收益）。

**per-SKILL hash 缓存**：refine 只看 SKILL 指令、不看 transcript，所以同一份指令只算一次。缓存键 = 指令内容（每条的来源/标题/文本）+ model + prompt 版本的 hash，落盘 `~/.cache/skill-eval/refine/`，跨 audit() 调用、跨进程复用。这解决了 runner 每 eval×config×run 调一次 audit、同 SKILL.md 反复 refine 实测 10+ 分钟的问题——首次算完落盘，后续命中秒回。指令内容 / model / prompt 版本任一变 → 键变 → 自动失效重算；删 `~/.cache/skill-eval/refine/` 可强制清。

**边界**：extractor 本身冻结在固定版本、纯正则不调 LLM（保证可复现、不把 LLM 方差引入抽取层）；refine 是 audit 层加的独立步骤，不破坏 extractor 的 layering。另一条根治路——结构化声明（frontmatter 声明 `required_tools`/`required_reads`/`required_outputs`，extractor 直接查声明）——更稳但仍待做（要 skill 作者配合改格式）；在那之前，refine 是默认兜底。

### 7.4 五态 verdict + method 双维度

每条 finding 有两个**正交**维度：

**verdict（结果，五态）**：

| 态 | 含义 | 重跑有用吗 |
|----|------|-----------|
| `PASS` | 已判明通过 | — |
| `FAIL` | 违规 | — |
| `N/A`（不适用） | 规则存在但本次场景不触发 | 不用 |
| `UNRESOLVED`（没判成） | LLM 调用/解析失败/未提及/畸形值 | **可重跑救回** |
| `INDETERMINATE`（判不准） | 证据真模糊（矛盾/缺失） | 重跑无益 |

**method（怎么查的）**：`PROGRAMMATIC`（tool_trace 命中 / 子串匹配，不调 LLM）或 `LLM`（语义判定）。

遵循度 = `pass / (pass + fail)`，排除 N/A + UNRESOLVED + INDETERMINATE（三者都不进分母）。

这三态必须分清，是看遵循性结果最容易混的地方：

- **UNRESOLVED（没判成）** = LLM 没出判定（操作性失败）→ 可重跑、汇总进"建议重跑"警告；
- **INDETERMINATE（判不准）** = LLM 出了判定但本身是"证据模糊判不了"（语义失败）→ 重跑无益，必须带具体 reason；
- **N/A（不适用）** = 规则存在但本次场景不触发（如 skill 写"读 references/foo.md"，但本次任务根本不涉及该条件）→ 已解决的有效结论，不进警告。

### 7.5 "宁可少报不乱报"

遵循性是诊断工具，**精度 > 召回**——假 FAIL 一多，skill 作者就不信这工具了。所以：

- **FAIL 只在 LLM 明确判"违规/该做没做"时报**；
- LLM 没判成（超时/异常/解析失败/未提及/畸形值）→ 报 UNRESOLVED，**不保守 FAIL**；
- 真模糊 → 报 INDETERMINATE（必须带具体矛盾/缺失证据，空 reason 降级回 UNRESOLVED）；
- audit 汇总 UNRESOLVED 条数 > 0 时在报告顶部出"建议重跑"警告。

记住一句话：**"0 FAIL ≠ 全过"**——要看警告里 UNRESOLVED 是不是 0。没判成的条可能藏着真违规。

### 7.6 run 内的遵循性 = audit 命令

重要：**run 里的遵循性判定和独立的 `audit` 命令是同一套引擎**（`TranscriptAuditor`）。run 直接调它喂单条 transcript.jsonl，产出的 `compliance.json` 就是 AuditReport，schema 和 audit 命令的 `audit-report.json` 一致。所以本章的判法对两者都成立。

### 7.7 audit 命令：对账历史 transcript

audit 是**对账**，不是单看 transcript——背后有个根本前提：**transcript 只是执行事实，不是规范**。它只记"做了什么"，给不出禁令（如"禁止编造数据"）、也说不出遗漏（"该做没做"）。若把 transcript 单方面当标准答案，会出两类错：把违规固化成期望（做过就当对）、把违规洗成 N/A（没判违规就算过）。所以规范权威永远是 SKILL.md，transcript 只提供场景和事实，**两者交叉**才有可靠判断——冲突点由 SKILL.md 仲裁，trace 始终只当证据。

```bash
skill-eval audit path/to/skill/ --transcript run-1/transcript.jsonl -o audit-out/
skill-eval audit path/to/skill/ --transcript a.jsonl --transcript b.jsonl   # 多 transcript 聚合
skill-eval audit path/to/skill/ --transcript big-multi-skill.jsonl --kind skill
skill-eval audit path/to/skill/ --transcript opencode-session.json          # opencode session(.json)
```

audit **不 re-run Claude**，直接拿已有会话的 transcript 跟 SKILL.md 对账。`--transcript` 吃两种格式(按后缀自动分派):cc 会话 `.jsonl`(`run --task` 两种执行器都支持,默认 `cc` 产 `.jsonl`,`--executor opencode` 产 `.json`)或 opencode session `.json`(`opencode export <sessionID>` 导出,顶层 `info`/`messages`、parts 嵌在 message 里;生产:`opencode session list` 找 id → `opencode export <id> > s.json`)。**拿到一个 .db 文件**(别处拷来的库)直接 `audit --db handed.db`(内部自动 list/export/audit;单 session 自动选,多个列出挑;底层三步见 reference audit 段)。可重复,多条 transcript 做指令级聚合(跨会话看 fail%)。

判定要逐批调 LLM（每批约 40–257s），跑期间实时打印进度（`[条件性/禁令/步骤] 判定 N 条…`），三个 LLM 类别判定器（条件性 / 禁令 / 步骤；输出格式走程序判定不调 LLM）并发跑（约 2× 提速）。看到 `[类别] …` 滚动就是在跑，别误以为卡死。

**对账作用域**：audit 默认审 **skill 作用域**——按 transcript 里的 `Skill` 工具调用切（scope-aware，last-invoked-owns：每个 `Skill` 调用起一段、归该 skill 到下一次调用；子 agent 是独立作用域，其 `parent_tool_use_id` 标记的 record 不污染主 skill 归属），只对账本 skill 被调用期间的段。两条固有限制：① skill 无"返回"信号，"调 A→调 B→不重调 A 接着干 A"那段会归给 B（不可测）；② 把委派子 skill 的段折进来审（inclusive）需声明、暂不支持。opencode 单锚点（无 `Skill` tool_use），按 per-message `agent` 字段切。

另两种作用域各成一节：**被 dispatch 的子 agent → §7.8**；**顶层主 agent / 编排者 → §7.9**。`--kind` 三选一，选锚点、不并集。

读结果（别只数 fail、来源徽章 / 同源关联）见 **§7.10**；transcript 反推出的额外检查见 **§7.11**；违规 turn 定位见 **§7.13**。

### 7.8 审 agent：对账被 dispatch 的 subagent

audit 不只能审 skill，也能审**被 dispatch 的 subagent**——拿 subagent 的定义 .md 去验它在会话里有没有按定义执行。skill 对账审不了 subagent：skill 按 `Skill` 工具调用切，而 subagent 不是被 `Skill` 调用的、是被 `Agent`/`Task` 工具 dispatch 的，落不进 skill 的切段。agent 对账填的就是这个空白。

**怎么用**：第一个参数给 agent 的定义目录（含 `SKILL.md`），加 `--kind agent`：

```bash
skill-eval audit path/to/test-runner/ --transcript session.json --kind agent   # 审被 dispatch 的 subagent（给 agent 定义目录）
```

切片器按 `record.agent == <name>` 切出该 agent 被 dispatch 期间的连续段，再对账它的 `SKILL.md` 指令。**name 契约**：agent .md frontmatter 的 `name` 要对上 transcript 里的 `agent` 字段（如 `name: test-runner` ↔ `record.agent == "test-runner"`）——对不上就切不到段、exit 2。

**和 skill 对账的三处区别**：

- **锚点不同**：skill 按 `Skill` 工具调用切（scope-aware last-invoked-owns）；agent 按 `agent` 归属字段切（dispatch 锚点，无 `Skill` 调用）。
- **扁平切、无父子作用域**：agent 切片只看 `record.agent == target`、**不读 `parent_tool_use_id`**。target 的 run 里若嵌套了别的 agent 的 turn（A dispatch B 后 resume A，record 序 A…B…A），A 的 run 被 B 断成两段。这是上游数据模型的被迫选择（`buildStructuredRecords` 不合成 `Agent` tool_use、无「agent 名 → dispatch id」映射，无法像 skill 路那样按作用域归段）。**断段无害**：每段独立喂 auditor，findings 全量 pool 后按指令聚合，verdict 与合成 1 段等价。
- **dual-nature 同名**：同一个名字既是 skill（被 `Skill` 调用）又是 agent（被 dispatch）时（如某个同名既作 skill 调用、又作 agent dispatch 的名字），`--kind skill` 审 skill 那面、`--kind agent` 审 agent 那面，各切各的、不串味。**kind 选锚点、不并集**——并集会把 agent 执行段塞进 skill 正文对账、对象错位。

现成示例：`fixtures/example-agents/`（test-runner / lint-reviewer，合成违规 transcript + 期望该抓的违规逐条列明），见 [example-agents/README.md](../fixtures/example-agents/README.md)。

> opencode transcript 单锚点（无 `Skill` tool_use），`--kind skill/agent` 的区分对它无意义、照常按 per-message `agent` 字段切——cc 的 skill/agent 双锚点区分只对 cc / structured transcript 有意义（`--kind root` 在 opencode 取 `session.info.agent`，见 §7.9）。

### 7.9 审 root（`--kind root`）：对账顶层主 agent / 编排者

与 agent（被 dispatch 的子 agent）对称，root 切的是**顶层主 agent**的作用域——审主 agent 的**编排动作**（dispatch 了谁、何时、怎么接返回），而非子 agent 内部。它是整 session / workflow 审计的地基：主 agent 的声明常是一份 workflow 级 SKILL.md（管主 agent 怎么编排，而不是某个子 skill / 子 agent 的行为），root 就是拿这份声明去对账主 agent 作用域的执行。

**双平台定义**：

- **cc / 结构化 records** = `parent_tool_use_id` 为空的全部 record（主作用域）。主 agent 自己的 turn + 它发起的 Agent dispatch tool_use / tool_result 都在主作用域（这些是主 agent 的编排动作，**包含**）；子 agent 内部 record 的 `parent_tool_use_id` 指向某 Agent 调用（**排除**——那些归各自 `kind="agent"` 单元审）。
- **opencode** 无父子作用域，root 取 `session.info.agent`（declared 主 agent），复用 `slice_opencode_by_agent` 按连续同 agent message 切段。

**断段差异**（同源于 §7.8 agent 路的"扁平切"）：cc 主作用域过滤后是连贯的主执行流，作为**单 span** 喂 auditor（即使主 record 在原序列里被子 agent record 物理夹断，过滤后仍连贯一段，区别于 agent 路的同名 run 断段）；opencode 按连续 message 切，主 agent 的 message 若被子 agent 的 message 夹断（主…子…主…子…主），会被切成**多段**——这无害，每段独立喂 auditor，findings 全量 pool 后按指令聚合（`aggregate_by_instruction`），verdict 与合成单 span 等价（与 agent 路断段无害同理）。

**target 名对 root 无意义**：skill / agent 路靠声明名（`name` 契约）切，root 只认作用域（cc 的 `parent_tool_use_id` 是否为空 / opencode 的 `info.agent` 字段）——切主 agent 不看它叫什么、target 名忽略。这也是 root 能成为整 session 审计地基的原因：它不依赖某个子单元被点名调用，而是恒定的顶层作用域。

exit code 不变（root 不引入新 gate）：cc 主作用域在**非空 transcript** 下恒非空（至少起头 user turn 在主），never-invoked 不触发 exit 2；**完全空 transcript 仍触发 exit 2**（无 record → 主作用域也为空，never-invoked）；opencode 缺 `session.info.agent`（无法定主 agent）→ exit 2。

### 7.10 怎么读 audit 结果：别只数 fail + 来源徽章

读 audit 结果时记住一句话：**别只数 fail 条数**——每条 finding 带序号。**来源徽章**显示这条 finding 是从哪个 / 哪些 `InstructionSource` 抽出来的（CONSTRAINT / ANTI_FABRICATION / HEADING_STEP / TOOL_REQUIREMENT / REFERENCE_USAGE / OUTPUT_FORMAT）。当同一句 SKILL.md 文本同时匹配多个抽取模式（如同句既是约束、又是反捏造）、且同 transcript 同 verdict 时，会**合并成一条**，徽章就显示两个来源（如 `CONSTRAINT + ANTI_FABRICATION`）——证明同文重复已去重、没有虚增 fail。而同一行为跨两条**不同**规则（如没读某 reference 同时违反"条件性"+"步骤"两条）仍会是两条，但互标"↔ 同源违规 #N"。所以 fail 条数可能 > 真实问题数，逐条看 evidence 对照"哪个行为"，别数条数。

### 7.11 derived_from_transcript（反推）

findings 里 `derived_from_transcript: true` 的不是从 SKILL.md 抽出来的，而是从 transcript 实际行为**补**出来的检查（**只增不减** = 只新增 finding，不会替换或删掉从 SKILL.md 抽出来的指令）。**目前只补一类**：transcript 里 agent 真实 Read 过的输入/参考文件 → 各补一条"读取 \<path\>"（REFERENCE_USAGE，构造上即命中 → PASS，不膨胀 fail）。另外两类不反推——工具调用反推（`required_tools`，Bash 命令分不清业务专用还是通用）和产物文件反推（`required_outputs`，历史 trace 的产物未必还在盘，且"产出文件检查"属评分断言、不属遵循性指令），因为映射不干净、容易误判。

### 7.12 逐条判别：某条 FAIL 是工具噪声还是 skill 问题

fixture 下遵循性的 FAIL 混着工具噪声和真 skill 问题（见 §7.14）。报告现在给每条 FAIL **自动标**一个启发式判别（per-run findings、整体汇总 by_instruction、单次 audit-report 的违规表都带徽章 + hover 理由）：

| `noise_label` | 含义 | 典型信号（命中即标）|
|---|---|---|
| `tool_noise`（疑似工具噪声）| 改 skill 治不了，根因在工具 | `source=HEADING_STEP` 且指令像资源链接（资源描述被当步骤）；evidence 自相矛盾（称"不涉及"却判 FAIL）；LLM 判定带跨类关联（`related` 非空）；聚合 NA 多 FAIL 少（条件性资源被当无条件步骤）|
| `skill_issue`（疑似 skill 问题）| 大概率真 skill 没做到 | 硬约束（`CONSTRAINT`/`ANTI_FABRICATION`）+ 程序判定 FAIL（确定、可复现）|
| `uncertain`（待确认）| 单条信号不足 | 其余 FAIL —— 看汇总分布或人工复核 evidence |

**怎么用**：看到 FAIL 先看徽章 + 理由（hover）。`tool_noise` → 别急着改 skill，先想 extractor/判定/evidence 是不是工具的洞（典型：SKILL「详细资源」段的链接描述被抽成步骤）；`skill_issue` → 这才是改 skill 的信号；`uncertain` → 人工看 evidence。优先级 `tool_noise > skill_issue > uncertain`（取最严：有一条噪声信号就归 tool_noise，**避免据噪声误改 skill**）。

**诚实说**：这是**启发式 + 疑似 + 理由，不是权威判决**（可能假阳/假阴）。它**治标**（帮解读现有噪声），没让 compliance 更准 —— 治本仍 `--refine-instructions`（判定前 LLM 砍伪指令，默认开）/ 结构化声明（frontmatter 声明 required_*）/ 真实 transcript audit。**别以为"标了 tool_noise = compliance 可信了"**。判别规则源码：`audit/noise.py`（纯函数，不调 LLM/不读 transcript）。

### 7.13 turn 定位（`turn_refs`）：可选的违规 turn 定位，默认关

audit 的 finding 默认告诉你「违反了什么指令 + evidence（中文理由）」，但**不告诉你在哪个 turn**——几百 turn 的 session 里手动翻找不现实。`turn_refs` 补这个：FAIL / INDETERMINATE 的 finding 带 `turn_refs` 列表（全局 turnIndex），能 link 到 turn 的消费方（如有 turn 浏览器的下游工具）拿它能直接跳到对应 turn 看上下文。

**但默认关（`--no-turn-refs`），要 `--turn-refs` 显式开——不是没做完，是开它有代价。**

#### 做了什么

开 `--turn-refs` 时，digest 重建给每条 assistant record 文本加全局 `[T{idx}]` 前缀（`[T142] assistant: …`），judge prompt 据此要求 LLM 在 FAIL / INDETERMINATE 时输出 `turn_refs`。段级缺席（「全程未发生」，如「调用 subagent」全程没调）→ 留空；turn 级违规（某具体 turn 做错）→ 给 seq。`locate_turns` 参数贯穿 rebuild（控前缀）→ judge（控 prompt 的 turn 引用规则）→ audit CLI 开关。

#### 对结果的影响：引入条件分支过严

加 `[T{idx}]` 前缀 = 动了喂 LLM 的 prompt，实测让判定**变严**：step / prohibition 类里「条件不满足→本就该跳过」的指令（如「读取 PLAN.md」「读取 probe」，本 session 条件没触发、该判 N/A），LLM 见到 turn 信号就倾向 FAIL。实测加前缀后 FAIL 数会明显上涨，多出的**不是新违规、是条件分支误判**（真违规判定没退化：原本就成立的 turn 级违规 FAIL + turn_refs 全保留）。

故默认关：多数场景（尤其 CLI 直接看报告、没有能 link 到 turn 内容的消费方）开了只多收条件分支误判噪声、拿不到定位收益；要定位且能接受多报时才 opt-in 开。

#### 三轮「判定层纠偏」尝试皆败（教训，勿重蹈）

发现过严后，试过三轮在**判定层**修正它，全败——记录于此免得未来开发者重蹈：

| 尝试 | FAIL | 为什么败 |
|---|---|---|
| 调松 judge prompt 措辞 | 17（过宽） | 丢真违规（如「最多重试 2 次」→N/A） |
| 加 `premise_held` 独立二判 | 30 | LLM 把 premise_held 当捷径、25 PASS→N/A + 丢真违规 |
| 判后纠偏 pass（rectify） | 36（最接近） | **漏报**：喂去 `[T{idx}]` 的 stripped transcript 再判，但「条件分支该跳过」与「真违规该做没做」在 stripped 文本里长得一样（都 = 没出现），LLM 分不清、全判 N/A，误伤 ~10 真违规段级缺席 |

**根因**：判定层二判无法区分两类「缺席」——stripped 掉 turn 信号后，「本就不该做」和「该做却没做」都是「没出现」，必漏报。审计最忌漏报（违规被掩盖、带「纠偏」标记还误导用户以为「不涉及」），比多报（可识别排除）危险得多。

**最终落地：做成可选开关，而非判定层纠偏。** 判定层没法「既保 turn_refs 定位又免条件分支过严」，故不在判定层二选一——两个形态都保留交用户选：默认给干净的（no-seq），要定位且能接受多报时 opt-in 开 seq。比任何判定层纠偏都干净。

#### 开 `--turn-refs` 时怎么读报告（triage）

- **`turn_refs` 空** = 段级缺席（全程未发生）。两可能：条件分支没触发（本就该跳过 → 其实 N/A，属多报）／真违规该做没做（真问题）。需复核 evidence。
- **`turn_refs` 有 seq**（如 `[142]`）= 确定的 turn 级违规，直接定位那个 turn。

条件分支误判集中在「empty turn_refs」那批，看 seq 能快速分流。

#### 已知限制 / 后续

- **opencode 切片的 turn_refs 是段内 local、非全局**：opencode 切片经 message 子集往返重建、按段内位置 re-stamp；cc / structured records 不受影响（dict 引用子集，`_turn_index` 经 JSON 往返保全局）。opencode 路开 `--turn-refs` 时 seq 是段内的。
- **OUTPUT / CONDITIONAL 类不带 turn_refs**：`--turn-refs` 只让 prohibition / step 类（走 transcript_text + turn 引用的 LLM judge）输出 turn_refs。OUTPUT 是程序化子串匹配、CONDITIONAL（TOOL_REQUIREMENT / REFERENCE_USAGE）的 FAIL 是段级缺席（trace 全程未调用 / 未读取 X）——两者都无具体 turn 可定位、恒空；只有 prohibition / step 的 turn 级违规才带 seq。
- **想减条件分支误判**：别再试判定层纠偏（三轮皆败）。靠报告层 triage（empty/有 seq），或改单轮 prompt / conditional 判定器本身的「条件不满足→N/A」引导。

### 7.14 这一层的边界：fixture 下边际价值低，真实 transcript 下价值高

诚实说，遵循性在不同路径下价值差别很大：

- **路径 3（fixture 评测）**：compliance 的边际价值偏低。fixture transcript 是按剧本 spawn 出来的，模型的行为高度受 skill-eval 的 prompt 框定；这里 compliance 查出来的"违规"，有相当一部分根因不在 skill 而在工具自身——典型就是上面讲的 extractor 把示例文字误当指令，导致大量 N/A。换句话说，fixture 下 compliance 更多暴露"工具的洞"，而非"skill 的洞"。要"找 skill 问题 → 改 skill"，主信号是 **scoring（A/B delta）+ 静态分析（analyze/vet）**，不是 compliance。
- **路径 2（audit 真实 transcript）**：价值高。真实会话里的违规是**诊断资产**——它如实反映 skill 在实际使用中被遵循成什么样，反过来暴露 skill 指令设计清不清晰、容不容易被遵循。这里的违规不是噪声，正是你想看的。

所以：做 fixture A/B 评测，别对 compliance 的数字过度解读（尤其 N/A 多时，先想 extractor 误抽）；要评估 skill 的真实遵循质量，拿真实会话跑 audit。

### 7.15 transcript 怎么变成可对账的 digest：三种来源同一条重建路

audit 对账前，先得把 transcript **重建**成一个结构化 digest——把"模型读了哪些文件、写了什么、改了什么、调了哪些工具"从原始会话记录里还原出来。这里有个关键架构：**cc 会话、opencode 会话、结构化输入三种来源，归一到同一种中间表示，再走同一个重建器**，所以重建逻辑只维护一份。

```
cc session (.jsonl)            opencode session (.json)         结构化 records (.json)
逐行 JSON record               opencode export                  friendly JSON
tool_use / tool_result         {info, messages, parts}          {role, tool_calls}
分两条记录，靠 tool_use_id 连   tool 的 input+output 在一个 part
       │                              │                                │
  _records_from_cc           _records_from_opencode          load_structured_records
  (近恒等)                   (转换：见下表三件事)             (工具名归一)
       │                              │                                │
       └─────────────► list[cc-record 形] ◄─────────────────────────────┘
                         （统一中间表示 / canonical）
                                 │
                                 ▼
                     _digest_from_records（共享重建器，单处定义）
                     · pass1：tool_use_id → result 映射
                     · pass2：按序演进文件状态
                         Read → 初始内容；Write → 覆盖；
                         Edit/MultiEdit → old→new 替换（找不到 → demoted）；
                         Bash → 记命令 + bash_touched
                     · 收 tool_trace / user_turns / transcript_text
                                 │
                                 ▼
                          SessionDigest
                                 │
                                 ▼   ← opencode 特例：skill_used 从 info.agent 补回
                                     （它没 Skill tool_use，重建器抽不到 → opencode 侧补）
                                 │
                                 ▼
                   TranscriptAuditor.audit()（对账）
                   · 多 skill 会话已先切分 → 每段各自重建、audit 跨段聚合
                     （cc 按 Skill tool_use；opencode 按 agent，切原始 session）
                   · 从 SKILL.md 提指令（6 类 → 4 类，见 §7.2）
                   · 每条指令：命中 tool_trace → PASS（程序，不调 LLM）；
                                否则 → LLM 语义判（transcript_text）
                   · 五态 verdict（见 §7.4），跨 transcript 聚合违规率
```

**为什么是这个形状**：cc-record 形（`{message:{role, content:[块]}, parent_tool_use_id}`）是仓库里早就有的 canonical 中间表示——结构化输入 importer 一直在用（把各种格式归一成它再喂重建器）。cc 近乎恒等（它本来就是这形状）；opencode 一度是唯一没接上、自己 inline 了一份弱化重建的（只 read/write/bash，无 edit 重建、无 unreconstructed 告警——技术债）。修法不是推倒重来，而是让 opencode 也走那条已验证的路（`structured.py` 是现成模板）。

opencode 的转换器（`_records_from_opencode`）做三件事抹平格式差异：

| opencode 的样子 | 转成 cc-record 形 |
|---|---|
| 工具名小写 `read`/`write`/`edit` | 归一大写 `Read`/`Write`/`Edit`（`_norm_tool`，共享映射表）|
| 字段 camelCase `filePath`/`oldString`/`newString`/`replaceAll` | 映回 snake_case `file_path`/`old_string`/...（`_map_opencode_input`）|
| tool 的 input+output 同在一个 part | 拆成 tool_use 块 + 尾随 tool_result 块，合成 tool_use_id（凑成 cc 的两趟关联形态）|

**read 输出的清洗不归转换器、归共享重建器**：真 opencode 导出 / 库里 read 存成 `<path>…</path>\n<type>file</type>\n<content>\nN: line…</content>`（带 XML 包裹 + `N: ` 冒号行号），不剥会污染 `input_files` 内容、还会让多行 edit 的 `old_string` 匹配不上而误判不可重建。这步清洗（`_digest_from_records` 里的 `_clean_read_result`）按输出**以 `<path>` 起手** + `<content>` 包裹触发（opencode read 恒以 `<path>` 起手、cc 恒以 `\d+\t` 起手，首字符就分化，比"含某字面"稳、假阳趋近零，且不绑 `<type>` 值）——**三种来源都走同一个**，所以结构化输入（观测平台 / 自建 harness 等）直喂这种 opencode 形 read 也自动洗净；cc / 裸内容（不以 `<path>` 起手）只剥自己的 `\d+\t` 行号，正文里合法的 `N: ` 文本、乃至含 `<content>`/`<path>` 字面的文件正文都不误伤。

抹平之后，Read/Write/Edit/MultiEdit 重建 + demoted/unreconstructed 推导（含上面的 read 清洗）对三种来源完全一致。**一个例外**：opencode session 没有 `Skill` tool_use（它的 skill 标识在 `info.agent`），所以 `skill_used` 由 opencode 侧从 `info.agent` 补回（重建器只从 `Skill` tool_use 抽，抽不到）。

**多 skill 切分**也在归一之后口径统一：cc 按 `Skill` tool_use 切（父子作用域），opencode 按 per-message `agent` 切连续段（扁平，在转换前切原始 session）。切完每段各自重建 → 各自对账。

> **对 audit 判定无影响**：重建出的 `produced_files`/`input_files` 内容、`unreconstructed` 这些字段，audit 其实**不读**——audit 只读 `tool_trace`（程序匹配，大小写不敏感）+ `transcript_text`（LLM 语义判）+ `user_turns`（场景）。所以 opencode 接到共享路之后 digest 更完整（edit 过的文件也重建、内容干净、不可重建的会告警），但 audit 的 verdict 不受影响——变的是 digest 的"保真度"，不是判定结果。

**结构化输入（structured records）——给外部 producer 的通用入口**

上面三种来源里，cc 和 opencode 各绑一种原生格式（cc `.jsonl`、opencode export `.json`）。**结构化输入**是第三条路：一个**故意做"友好"的通用 JSON 格式**，给任何**已经把会话归一成 turn + 工具调用**的外部系统用——它不必伪造 cc 的 jsonl、也不必伪造 opencode 的 `{info,messages,parts}`。形状（顶层 `format == "skill-eval-records"` 作识别，跟 opencode 的 `{info,messages}` 区分）：

```json
{"format": "skill-eval-records",
 "records": [
   {"role": "user", "text": "..."},
   {"role": "assistant", "agent": "...", "parent_tool_use_id": null, "text": "...",
    "tool_calls": [{"name": "skill", "input": {"name": "x"}, "result": "..."},
                   {"name": "read",  "input": {"file_path": "..."}, "result": "..."}]}
 ]}
```

**它最通用、最不绑任何框架**：cc 绑 Claude Code、opencode 绑 opencode export，结构化输入只要求"有 turn + 工具调用"——任何 agent 框架 / 观测平台 / 自建 harness / 别的会话存储（像某些观测平台那样把原生格式转成自己 schema 的）都能吐。已有实例在用，但**不专属任何一家**：producer 不用拼 cc 的 tool_use / tool_result 拆分、不用管 `Skill` 大小写、不用管 read 输出的 `<content>` 包裹——这些归一全由 skill-eval 这边做（`load_structured_records` 展开成 cc-record 形；read 清洗、Edit/MultiEdit 重建都走共享重建器），producer 只管按 turn 吐真实数据。事实上 canonical 重构就是把 opencode **照着结构化输入这条已验证的路**接上共享重建器的（`structured.py` 是现成模板），结构化输入是三者里的参照系，不是为哪家定做的小门。

**对接要点**（给想接的 producer）：每个 turn 一条 record；工具调用放 `tool_calls`（`name` 用原始小写即可，skill-eval 归一）；skill 调用参数键用 `input.name` 或 `input.skill` 都行（reader 归一到 cc 认的 `input.skill`）；`parent_tool_use_id` 是**作用域标记**——主 turn 填 `null`，子 agent 里跑的 turn 填该子 agent 的稳定 id（用 producer 自己的子 agent 稳定 id 字段，缺则回退父执行 id）。

**`parent_tool_use_id` 决定多 skill 切段的作用域归并**：`--kind skill` 的切片器（`slice.py:_scope_of`）按它把 record 归作用域——`null` = 主，其他值 = 某子 agent 的独立作用域；作用域内 last-invoked-owns（每个 `Skill` 调用起段，到同作用域下一个 `Skill` 调用或作用域结束）。**子 agent 里调用过 skill 的 session 必须填它**：若子 agent turn 也填 `null`，它们会和主 turn 挤进同一个主作用域，主作用域的噪声（别的工具调用 / 别的 skill）就按 last-invoked-owns 混进本 skill 的段 → 假证据（实测跨 scope 噪声 40%+）。填对之后，子 agent 的 skill 段被干净隔离进自己的作用域。零嵌套（子 agent 不再派生子 agent）数据下这是充分的：切片器的 depth-1 closure（靠 `Agent` tool_use 配对 tool_result）在结构化输入没合成 `Agent` 块时是 no-op，归段只靠 `parent_tool_use_id`——所以 producer **不必伪造 `Agent` tool_use**，把 scope id 填对即可。守卫测试：`test_structured.py::test_slice_isolates_subagent_scope_from_main`。

**`tool_calls[].result` 是可选的——做对账(audit)的 producer 建议不传**：`result`（工具返回值，如 Read 整个文件、skill 正文、大段命令输出）在真实 session 里常达 10MB+，而 **audit 的判定根本不读它**——auditor 只取 `digest.user_turns`(场景)+ `tool_trace`(工具调用的**输入**)+ `transcript_text`(assistant 文本)，三项都不含工具结果；连"反推指令"(`derive_declarations`)也只读 `input_files` 的 **路径**(路径来自 Read 的 `file_path` **输入**，不靠 result)。所以全量回传 `result` 是纯死重量：会让 `--transcript` 的 session.json 撑到 20MB+，序列化 / 落盘 / skill-eval 解析全慢。做 audit 的 producer 因此**不传 `result`**（payload 实测 18MB→7MB，砍掉 10MB+ 的结果体）。reader 对缺 `result` 的 tool_call 本就容错（不产 tool_result 块，`input_files` 内容空但路径在、tool_trace 完整），对账 verdict 不变。仅在需要重建文件**内容**(produced_files 还原、非对账)时才传 `result`。

---

## 第八章 校准

校准（Calibration）是把前两层（评分 + 遵循性）的矛盾**暴露**出来的那一层——它回答"这次执行的**结果**与**过程**协不协调"。

### 8.1 结果×过程 coherence：为什么是这两个轴配一对

为什么要拿"结果"和"过程"这两个轴来配？因为"一次 skill 执行可不可信"恰好有这两个**互相独立**的维度：

- **结果轴**——输出对不对（有没有达到通过线）。
- **过程轴**——过程规不规矩（有没有违反硬性规则）。

它们独立到可以任意组合：你可以**输出对了但过程错了**（抄近路、跳过校验、碰巧蒙对——skill 的关键步骤没真执行，答案却对了），也可以**输出错了但过程对了**（skill 逻辑没问题，只是执行失败）。**只看结果**会漏掉前者（"蒙对答案"看着像满分，却不能归功于 skill）——这正是校准要抓的危险情况。所以把这两个轴交叉，专门把"结果对 + 过程错"这一格标出来（`HIGH_SCORE_SUSPECT`），就是校准存在的理由。换别的组合（比如结果 × 工具调用数）就没这层意义。

矩阵是结果轴（对 / 错）× 过程轴（对 / 错 / 未知），折叠成 5 态。它**不是**凑出来的置信度数，而是分诊信号。

**这两个轴怎么"配成一对"——不是逐条对账。** 容易想成"第 N 条期望 ↔ 第 M 条指令"逐条配对，但代码并不这么做（也没有公共键可配：期望来自 eval、规则来自 SKILL.md，是两份不同文档）。它把结果轴压成一个聚合率（输出达标没），过程轴压成"哪些硬类规则被违反"，两个**聚合信号**在 2×2 矩阵里交叉。所以"结果对了但过程错了"只是观察到**这次执行：输出达标 AND 碰了某条硬规则**这两个事实共存——**因果没建立**（不判断"输出对是*因为*还是*尽管*碰了那条规则"）。这正是那一格叫 `HIGH_SCORE_SUSPECT`（存疑、值得看一眼）而不是"已证矛盾"的原因：它只报"两个面这次没对齐，人来看一下"，不下因果结论。

### 8.2 两轴都押在实测高精度的信号上

为了让判决稳，两轴都尽量避开方差大的信号：

- **结果轴**：优先押**客观** `pass_rate_programmatic ≥ 0.8` → 结果对。`0.8` 与 report 里 pass_rate 的"绿档"徽章同值（`RESULT_PASS_THRESHOLD` 常量，非 CLI 可配），是条经验线——足够严格、又给抖动留了容差。纯主观题（无客观锚）才回退主观率，并在 `scoring_rate_basis=mixed` 标注"基于主观判分"。
- **过程轴**：只看遵循性里 **prohibition（禁令）/ output（输出格式）两类**违规——这两类**实测精度高**（prohibition 是 LLM 主动找违规、判定较硬；output 是确定性子串匹配、零方差），一旦 FAIL 就值得翻判决。有硬类违规 → 过程错；无 → 过程对；compliance 缺失 / 无可判定 → 过程未知。而 step / conditional 判的是"走过没 / 该不该触发"，**实测方差更大**，FAIL 只记进 `soft_notes`、不翻判决（否则一次 LLM 抖动就推着 verdict 摆）。

> 关键：硬 / 软的分界**不是"程序判 vs LLM 判"**（prohibition 其实也是 LLM 判），而是**实测精度**——prohibition 的"主动查违规"和 output 的"子串匹配"都够稳，step / conditional 的"走过没"够噪。这分类来自实测，不是按判定方式硬分。

### 8.3 五态矩阵

| verdict | 结果 | 过程 | 含义 |
|---------|------|------|------|
| `TRUSTED`（可信） | 对 | 对 | 未发现过程问题 |
| `HIGH_SCORE_SUSPECT`（高分存疑） | 对 | 错 | 输出对了但过程违反硬性规则，建议核对（列 evidence） |
| `FIXABLE`（可修复） | 错 | 对 | 输出未达标，过程无明显违规 |
| `SKILL_FAILED`（技能失效） | 错 | 错 | 输出未达标且过程违反硬性规则 |
| `PROCESS_UNKNOWN`（过程未知） | — | 无可对账 | compliance 缺失或无可判定项（如纯推理题） |

`HIGH_SCORE_SUSPECT` 是校准最有价值的一态——它标出"抄近路蒙对答案"的情况（表里的"高分"指结果轴通过了，但过程违反了硬性规则）。

`calibration.json` 字段：`verdict` / `result_pass` / `process_status`（ok/violated/unknown）/ `hard_violations`（每条 category+instruction+evidence）/ `soft_notes` / `scoring_rate` / `scoring_rate_basis` / `scoring_rate_programmatic` / `scoring_rate_llm` / `compliance_rate`。

**跑出来长啥样——同题多 run，verdict 会摆。** 同一道题跑多个 run，verdict 常在两态间摆：有的 run 结果没过 0.8、过程干净 → `FIXABLE`；有的 run 同样没过 0.8，但那次 cc 碰巧犯了某条 prohibition 硬禁令（如 log-error-analyzer-zh 的「必须为报告的每个错误引用对应的日志原文行」）→ 过程 `violated` → `SKILL_FAILED`。差别**不在结果轴**（几个 run 的客观率都摆不大、都判 `result_pass=False`），**在过程轴**——硬违规在不在，verdict 就跟着翻。这就是「校准是 per-run 分诊、不是稳定 skill 判决」的活例子（详见 8.4），也是它不进 gate **decision** 的直接原因（但 HIGH_SCORE_SUSPECT 会作 soft warning 显出，见 8.4）。

### 8.4 为什么不进 gate 的 decision（只作 soft warning）

校准是 **per-run 分诊**，不是稳定的 skill 级判决——同一道题，verdict 会跨 run 摆（结果轴相对稳，过程轴的硬违规在不在会摆，verdict 跟着摆）。所以校准**不进 gate 的 decision**（门禁的 PASS/FAIL 走稳定的 `delta.pass_rate_programmatic` + 盲评胜率）；但它的 HIGH_SCORE_SUSPECT 会作 **soft warning** 聚合进 `calibration_summary`、显在 gate.json + viewer（分诊辅助人判断，**不改 decision/exit_code**）。只在注入了 skill 的 side 产（with/without 模式 = with_skill 侧；v1/v2 模式两边都产）。

更根本的原因是**校准成立有个前提，而工具不验证它**。结果轴查的是 eval 关心的、过程轴查的是 SKILL.md 要求的——只有当 eval 测的正是 skill 该干的活，"结果对 + 过程错"才是真矛盾（该守的硬规矩没守、却侥幸输出对了 = 潜在 bug）。但工具**假设**相关、不去检查"这条硬规则和这个 eval 的成功标准相不相关"。毫不相关时（eval 测 SQL 正确性、SKILL.md 某硬规则却管"输出编码格式"），cc 输出对了 SQL 又没守格式 → 机械落 `HIGH_SCORE_SUSPECT`，但这只是两个独立事实共存、不是 coherence 矛盾，人看一眼 evidence 就能 dismiss。所以校准只能当**分诊**（让人核 evidence、判断那条硬规则和这次输出到底有没有关系），不能当**定罪**——门禁走的客观 delta + 盲评胜率都不依赖这个"相关性未验证"的弱 coherence，故校准再保守/激进都不会让 CI 误绿误红（HIGH_SCORE_SUSPECT 只作 warning、不改 exit_code，CI 绿红仍由客观 delta + 盲评胜率定）。

---

## 第九章 run 过程与产物解析

这一章下钻到 per-run 产物的字段级含义和聚合链路，告诉你"每个 json 里什么字段什么意思"。

### 9.1 一次 run 的链路

```
spawn claude -p
   → transcript.jsonl（完整会话）+ outputs/（产物文件）
   → grading.json（评分：expectations + assertions，双率）
   → compliance.json（遵循性：AuditReport，五态 + method）         [注入 skill 的 side 才产]
   → calibration.json（校准：结果×过程 5 态）                       [有 compliance 才产]
   → benchmark.json（聚合：per-run → run_summary + delta + 盲比较 + 分析）
```

### 9.2 per-run 产物逐个解读（字段级）

#### grading.json（评分）

顶层 `GradingResult`：

| 字段 | 含义 |
|------|------|
| `expectations[]` | 每条 expectation / assertion 的判定（见下表） |
| `summary` | 汇总：`passed`/`failed`/`total`/`pass_rate` + 双率分项 |
| `execution_metrics` | 工具调用统计：`tool_calls`{工具名→次数}、`total_tool_calls`、`total_steps`、`files_created[]`、`errors_encountered`、`output_chars`、`transcript_chars` |
| `timing` | `total_tokens` / `duration_ms` / `total_duration_seconds` / `grader_token_usage`（Phase 2：per-run grader LLM token，rollup 进 `benchmark.json.llm_token_usage.scoring`）/ `grader_duration_seconds`（Phase 2.5：per-run grader 耗时，rollup 进 `llm_duration_seconds.scoring`）|
| `claims[]` | grader 抽取的事实声明（`claim`/`type`/`verified`/`evidence`） |
| `user_notes_summary` | grader 标的不确定项：`uncertainties[]` / `needs_review[]` / `workarounds[]` |
| `eval_feedback` | grader 对这道 eval 设计本身的反馈（`suggestions[]` / `overall`） |
| `error` | 非空表示评分过程出错 |
| `cache_key` | 内容感知续跑键（见 5.5） |

每条 `ExpectationResult`：

| 字段 | 含义 |
|------|------|
| `text` | expectation 文本 |
| `passed` | 是否通过（bool） |
| `evidence` | 证据引用（强制非空） |
| `source` | `programmatic` / `llm`（决定进哪个双率桶） |
| `verdict` | `PASS` / `FAIL` / `UNEVAL` / `TIMEOUT` / `ERROR` |
| `retry_count` | 这条经过了几次重试 |

`summary`（`GradingSummary`）：`passed`/`failed`/`total`/`pass_rate` + `passed_programmatic`/`total_programmatic`/`pass_rate_programmatic` + 对应的 `*_llm`。某类没有 expectation 时，对应分项为 `null`（不报假 0）。注意 `failed = total − passed`，所以 UNEVAL/TIMEOUT/ERROR（`passed=false`）在数值上算"未通过"，会拉低 `pass_rate`——但报告里它们与 FAIL 显示不同图标（见 5.4）。

#### compliance.json（遵循性，仅注入 skill 的 side 产）

= `AuditReport`（与 `audit` 命令的 `audit-report.json` 同 schema）：

| 字段 | 含义 |
|------|------|
| `skill_name` / `transcripts[]` | 被审 skill + transcript 文件名 |
| `summary` | 计数 `{total, pass, fail, na, unresolved, indeterminate}` |
| `findings[]` | 每条指令的对账结果（见下） |
| `by_instruction[]` | 按指令跨 transcript 聚合：`pass_count`/`fail_count`/`na_count`/`unresolved_count`/`indeterminate_count`/`total`/`fail_rate`/`fail_in_transcripts[]`（单 transcript 也有，每指令 1 条） |
| `warnings[]` | 顶部警告（没判成建议重跑、判不准占比偏高等） |
| `token_usage` | 本次 audit 所有 **LLM 判定调用**（refine + 条件/禁令/步骤判官 × 批）的 token 用量合计：`{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, total_cost_usd, calls}`。空 `{}` = 无 LLM 调用或旧 report（`call_claude` json 模式抓 usage 累加；audit 默认就开）。**注意是判官 token**——与 run 侧 `RunResultMetrics.tokens`（executor 跑任务的 token，走 stream-json）不同；run 的 grader/comparator/analyzer token 已计入 `benchmark.json.llm_token_usage`（Phase 2：`scoring` 段含 grader+comparator+analyzer；本 audit token rollup 进 `audit` 段） |

每条 `AuditFinding`（两个正交维度 `verdict` × `method`）：

| 字段 | 含义 |
|------|------|
| `instruction_text` | 指令原文 |
| `source` | 规则类型（`InstructionSource`：CONSTRAINT / HEADING_STEP / TOOL_REQUIREMENT / REFERENCE_USAGE / ANTI_FABRICATION / OUTPUT_FORMAT） |
| `verdict` | 五态：`PASS` / `FAIL` / `N/A`（不适用）/ `UNRESOLVED`（没判成，可重跑）/ `INDETERMINATE`（判不准，重跑无益） |
| `method` | `PROGRAMMATIC`（程序对账）/ `LLM`（语义判定）——"怎么查的"，与 verdict 正交 |
| `category` | 语义类：`conditional` / `prohibition` / `step` / `output` |
| `evidence` | 判定理由（INDETERMINATE 时即判不准的理由） |
| `transcript` | 对应 transcript 文件名 |
| `derived_from_transcript` | 是否反推声明来的（见 7.6） |
| `seq` | `#N` 序号（去重后编排，供 `related` 引用 + 报告定位） |
| `sources[]` | 同文去重合并的全部来源（单条 = `[source]`） |
| `related[]` | 同行为跨类关联的其它 finding 序号（如 `["#9"]`） |

遵循度 = `pass / (pass + fail)`，排除 N/A + UNRESOLVED + INDETERMINATE（三者都不进分母）。

#### calibration.json（校准，有 compliance 才产）

`CalibrationReport`——结果×过程 coherence 判决（机制见 [8.3](#83-五态矩阵)）：

| 字段 | 含义 |
|------|------|
| `verdict` | 5 态：`TRUSTED` / `HIGH_SCORE_SUSPECT` / `FIXABLE` / `SKILL_FAILED` / `PROCESS_UNKNOWN` |
| `result_pass` | 结果轴是否通过（客观率 ≥ 0.8，或无客观锚时回退主观率达标）|
| `process_status` | 过程轴：`ok` / `violated` / `unknown`（无可对账）|
| `hard_violations[]` | 硬类（prohibition / output）违规明细，每条 `{category, instruction_text, evidence}` |
| `soft_notes` | `{"step": N, "conditional": N}`——这两类 FAIL 的计数（软类，只记提示、不翻判决）|
| `scoring_rate` | 驱动判决的率：客观优先，无客观锚时回退主观 |
| `scoring_rate_basis` | `programmatic`（押客观）/ `mixed`（无客观锚、含主观）|
| `scoring_rate_programmatic` | 客观通过率（参考；`None` = 该 run 无客观断言）|
| `scoring_rate_llm` | 主观通过率（参考）|
| `compliance_rate` | = `pass/(pass+fail)`（显示用）；过程未知时为 `None` |

> gate 读 calibration_summary 的 HIGH_SCORE_SUSPECT 作 **soft warning**（不进 decision）。`hard_violations` 里列的就是值得人秒辨真伪的"高分存疑"证据。

#### run_debug.json

单次执行的运行时快照（调试、聚合时被读）：

| 字段 | 含义 |
|------|------|
| `config` | 配置名（with_skill / without_skill，或 v1/v2 标签）|
| `eval_name` | eval 真名（聚合时被读出来填进 benchmark，否则只显 "Eval N"）|
| `eval_id` / `run_num` | eval id + run 编号 |
| `prompt` | 发给 claude 的 prompt（截断 500 字符）|
| `duration_ms` | 单次执行耗时（毫秒）|
| `total_tokens` | 本次 token 数 |
| `num_turns` | claude 跑的轮数 |
| `stop_reason` | 结束原因（`end_turn` / `max_turns` / `timed_out` …；含 `timed_out` 会被异常检测抓）|
| `tool_calls` | 工具调用计数 |
| `tool_trace[]` | 工具调用序列（逐条明细）|
| `result_text` | claude 最终回复（截断 2000 字符）|
| `transcript_preview` | transcript 预览（截断 2000；完整会话在 `transcript.jsonl`）|
| `grading_summary` | 本次的 grading summary（双率）|

#### timing.json

`TimingData`，三个字段：

| 字段 | 含义 |
|------|------|
| `total_tokens` | 总 token |
| `duration_ms` | 耗时（毫秒）|
| `total_duration_seconds` | 耗时（秒）|

聚合时若 grading.json 的 `timing` 缺失，会回退读它——是 timing 的兜底源。

### 9.3 聚合成 benchmark.json

所有 per-run 跑完后，`BenchmarkAggregator.aggregate()` 把它们聚合成 `BenchmarkData`：

| 顶层字段 | 含义 |
|---------|------|
| `metadata` | `skill_name` / `skill_path` / `executor_model` / `comparator_model` / `analyzer_model`（executor 跑题 / comparator 盲比较 / analyzer 深度分析 三个 LLM 角色，当前锁定三者用同一个 `--model`；真实模型名都借 executor 从 transcript 抓，故同源）/ `timestamp` / `evals_run`（跑过的 eval id 列表）/ `runs_per_configuration` |
| `runs[]` | 每个 (eval, run) 一条 `RunResultEntry`（见下） |
| `run_summary` | 每 config 一组统计 + `delta`（见下） |
| `anomaly_report` | 异常检测（超时/离群） |
| `comparisons[]` | 盲比较逐条（读 `outputs/`，不是 benchmark） |
| `comparison_summary` | 盲比较聚合 `{total, a_wins, b_wins, ties, failed, valid, win_rate, label_a, label_b}` |
| `analysis` | 深度分析（见 12.3） |
| `selection` | （可选，仅 `--changed-from` 子集跑时）`{mode, ids, affected, total}`——标识本次为受影响子集跑、delta 非整体测量 |
| `llm_token_usage` | （Phase 2）本次评测的 LLM token 用量 `{scoring, audit, executor, total}`：`scoring`=grader+comparator+analyzer 合计（带 cost/calls,calls=各子项真实调用次数之和）；`audit`=compliance rollup 自 per-run `compliance.json`；`executor`=被测 cc token（仅 token 数无 cost，对应 `run_summary.tokens`）；`total`=全加 token、cost=scoring+audit。**executor 是被测 token 效率（无 cost），scoring+audit 才是评测系统付出的 LLM 成本**。viewer 头部「LLM token 用量」分列 + 总计；旧 report 无此字段 → `{}` |
| `llm_duration_seconds` | （Phase 2.5）本次评测耗时（秒）`{scoring, audit, executor, total}`：分列=各类调用时长加总（grader+comparator+analyzer / compliance / 各 run）；**`total`=墙钟**（run 命令头尾独立测）。与 token 根本不同：token 是消耗品可累加（total=分列和）；time 是墙钟、各段并行重叠（workers/线程池），**分列和 ≥ total 正常**，total 不由分列加、viewer 不对账分列和 vs total。分列看"哪段重"，total 看"等多久"；旧 report 无此字段 → `{}` |

每条 `RunResultEntry`：`eval_id` / `eval_name` / `configuration` / `run_number` / `result`（`RunResultMetrics`：`pass_rate`/`passed`/`failed`/`total`/`time_seconds`/`tokens`/`tool_calls`/`errors` + 双率 `pass_rate_programmatic`/`pass_rate_llm`）/ `expectations[]` / `compliance`（**通常为 null**——聚合器不嵌完整 compliance，完整 AuditReport 在 per-run `compliance.json` 里，report 用 `find_runs` 另读）/ `notes[]` / `is_anomaly` / `anomaly_reason` / `retry_count`。

`run_summary`：每个 config 一组——`pass_rate` / `pass_rate_programmatic` / `pass_rate_llm`（后两者可能为 null）/ `time_seconds` / `tokens`，各带 `{mean, stddev, min, max}`；再加 `delta`：全是**有符号字符串**（如 `"+0.27"`），= 候选 − 基线，双率项可能为 null（任一侧无该类 expectation 时不报假 0）。

### 9.4 retry 机制

UNEVAL / 出错时自动重试（`--max-retries`，默认 2），每次重跑完整的 Claude + 评分 + 遵循性。重试目录形如 `run-1-retry-1`。聚合时 `index_runs` 只取**最终 retry**（否则被取代的失败 base 会被当成独立低分 run 计入，拉低均值）。`retry_count` 记在 RunResultEntry 里。

---

## 第十章 重跑归档

重跑同一个 skill 时，skill-eval 会先回答两个问题：① **上次跑完了吗**（`benchmark.json` 在不在）？② **inputs 变了吗**（现有 slot——即每个 (eval, run) 槽位，如 `eval-1/run-2`——的 `cache_key` 还匹不匹配）？再结合 `--runs` 是不是调大，落进 resume / accumulate / fresh 三种行为之一。

**为什么要关心重跑归档**——它支撑的是 report 里的两种 delta，别混：

- **横向 A/B delta**（report「通过率变化 Δ」卡）：**同一次 run 内**，with vs without 之差——回答"这个 skill 到底有没有用"。
- **纵向 vs-上次 delta**（report「vs 上次」卡）：**跨两次 run**，同一个 side 之差——回答"这次比上次进步/退步了吗、复现性怎样"。

第二种 delta 赖以成立的前提，就是 fresh 重跑时把旧树挪进 `previous/` 保留下来。所以归档不只是"旧数据去哪了"，而是纵向对比的根基。

### 10.1 三态判定：resume / accumulate / fresh

| 情况 | `--runs` vs 已有 | 行为 | previous/ |
|------|------------------|------|-----------|
| 上次没跑完（`benchmark.json` 不在） | 任意 | **resume**：cache 续跑已完成的槽，补跑剩下的 | 不产 |
| 上次跑完 + inputs 没改 | 调大 | **accumulate**：旧槽 cache 复用 + 补新 run 号，聚合算全部 | 不产 |
| 上次跑完 + inputs 改了 | 任意 | **fresh**：旧树 MOVE 到 `previous/` + 全部 fresh 重跑 | ✅ 产生 |
| 上次跑完 + inputs 没改 | 同 / 变小 | **fresh**：MOVE 旧树 + fresh 重跑（看复现性 / 主观侧方差） | ✅ 产生 |

**resume（续跑）**——上次没跑完就中断了（`benchmark.json` 不在 = 聚合没成功）。不清空、不归档，在原地接着跑：已完成的 slot（`cache_key` 仍匹配）跳过，缺的 slot 补跑。典型场景：跑到一半 Ctrl+C / 崩了 / 撞限流中断，重跑同一条命令把没跑完的补上，省 token。

**accumulate（累加）**——上次完整跑完了、inputs 一点没改、这次只是把 `--runs` 调大（想要更多样本）。不归档，旧 slot（已有 run-1/2）cache 复用、不重跑 Claude，只补跑新增的 run 号（run-3…），最后聚合把新旧全部算进去。典型场景：3 runs 看完觉得样本不够，改 `--runs 5` 接着跑，只多花 2 runs 的钱。

**fresh（全新重跑）**——两种触发：① inputs 改了；② inputs 没改但 `--runs` 没调大（想看复现性 / 主观侧方差）。两种都把旧树整体 MOVE 到 `previous/`（单槽覆盖），原位清空 → 所有 slot cache miss → 必然从头重跑。典型场景：改了 skill 想看新效果；或同样配置再跑一遍看主观侧抖不抖。

> **accumulate vs fresh 的分界**：两者都是"跑完了再跑"，但 accumulate 复用旧 slot（inputs 没变、只是要更多样本），fresh 丢弃旧 slot 重跑（inputs 变了，旧样本是旧输入的产物、不能混进新聚合）。判据就是现有 slot 的 `cache_key` 还匹不匹配。

**"inputs 改了"具体指什么**：SKILL.md 正文 / evals.json 内容（含删 eval、改 prompt / expectations / assertions / fixtures / 换 sides）/ `--model` / scoring 配置（hybrid/programmatic/llm）/ skill-eval 代码版本。任一改动 → `cache_key` 变 → fresh → 产 `previous/`。已知缺口：eval 引用的 `source_dir` / `references/` 文件**内容**改动不检测（见 5.5），改这些请用新 `-o` 强制重跑。

### 10.2 为什么 fresh 用 MOVE 不用 COPY

重跑要看的就是**方差**——Claude 非确定性，主观侧有真方差（约 23%，客观侧 programmatic 稳）。如果 copy 旧树（`grading.json` 还在原位），cache 会全 skip 出"和上次逐字相同"的结果，`previous/` 和 current 一模一样——归档了个寂寞。MOVE 清空原位 → 所有槽 cache miss → 必然 fresh 重跑，`previous/` 里才有真东西可比。accumulate 的"复用旧"靠 cache_key 匹配自然达成（不 MOVE → grading.json 在位 → skip），它和 fresh 的分界就是 inputs 变没变。

**归档范围：只 move run 自己的产物，不卷其他命令的。** output 目录里常不只有 run 的东西——同目录可能还跑过 `audit`（产 `audit-report.json`/`.html` + `slices/`）、`gate`（`gate.json`）、`report`（`report.html`）。fresh 归档只 move run 的产物：config 目录（`with_skill/`…含 `eval-*/run-*`）+ `benchmark.json`/`scorecard.json`/`vet-report.json`/`analysis-*.json`；其他命令的产物**原地不动**，不会被卷进 `previous/`。归档的语义是"归档上次 run 的结果"，不是"清空目录"——audit 审的是独立 transcript、gate/report 是 run 之后才出的判决/报告，都不属于"这次 run 的产物"，卷走会丢你的数据。

### 10.3 几个诚实的设计取舍（价值）

- **样本数不同就标出来**：fresh 重跑若 runs 变了（previous 3 vs current 6），delta 卡显式提醒。均值仍可比，但别对一个小 delta 过度解读。
- **新增 run 无对照 = 隐藏，不是 bug**：6 个 run 里 run-4/5/6 是本次新增、`previous/` 没有 → 文件级"上次输出"面板对它们隐藏；聚合卡照常算（比的是均值）。两层分开、不矛盾。
- **双率哲学在纵向的延续**：客观侧跨 run 稳、主观侧会漂。vs-上次卡上你会看到客观 △ 多半小、主观 △ 可能大——看到主观 △ 大别慌（可能是 cc 方差而非 skill 退步），看到客观 △ 大才真该去查。
- **move 的诚实**：重跑 = "重新测一遍"，承认不复用旧数据。比 copy 假装"既有对比又省 token"诚实——这俩在"输入没改 + 同 runs"时不可兼得，copy 会产出无意义的 identical previous。
- **单槽 previous，不做历史**：永远只留上一次，不搞 `previous-1/`/`previous-2/` 叠层（对比关心"现在 vs 上次"，叠层反而引来"到底比哪两个"的歧义）。想留多次快照？多次重跑间及时把 `previous/` 拷出去自己存。

### 10.4 怎么看 / 怎么用

**四个典型场景：**

- **(a) 改了 skill，想看进步** → 同 runs 重跑 → fresh → `previous/` 存住上次 → report「vs 上次」卡看客观 / 主观 △（客观 △ 才是 skill 真改观；主观 △ 看方向）。
- **(b) 啥也没改，就想再看一次** → 同参数重跑 → fresh → vs-上次卡看主观侧漂移（复现性 / 噪声底）。**重跑必然 fresh（`benchmark.json` 在）——这是设计，不是漏了 cache。**
- **(c) 想加样本更稳** → 调大 `--runs`（3→6）→ accumulate → 旧 3 个复用 + 补 3 个，聚合算 6 个（无 vs-上次卡；这是一个在长的数据集，不是一次对比）。
- **(d) 上次撞限流断了** → 直接重跑 → resume → 跳过已完成的、补跑剩下的（不产 `previous/`）。

**没有"强制 fresh"开关**：重跑默认就是 fresh（只要上次跑完了）。accumulate 是"调大 `--runs`"这一个明确动作的专属——想 fresh 就直接重跑，想复用旧样本就调大 runs，语义清晰无歧义。

**报告里"vs 上次"那一栏怎么看**：这一栏把这次和上次的指标并排放，让你看变化。分三类读：客观通过率——看绝对水平和变化幅度（变化小说明稳）；主观率 / 盲评胜率——看趋势方向（变化大别马上怪 skill，先想是不是 cc 这次发挥的方差）；样本数那一行（比如 ⚠ 这次 6 runs vs 上次 3 runs）告诉你这次对比有多可信——样本数差很多，delta 就别太当真。上次没跑某项的话，那格直接不显示，不会拿 0 凑数。

---

## 第十一章 门禁 gate

`gate` 对已有 benchmark.json 出 PASS/FAIL 判决。它是 **post-hoc 纯函数**——不重跑 Claude、毫秒级、可反复改阈值重判（和 `report` / `audit` 一样，都只读 run 的产物做后处理，不重新跑题）。

`gate` / `report` / `audit` 这三者都只消费 run 的产物、不重新跑题，和 `run` 的分工如下：

| | 角色 | 干什么 | 碰 cc 吗 |
|---|---|---|---|
| `run` | **生产者** | spawn cc 跑题 → 产出 grading / compliance / transcript 等产物 | 费 token、慢、有方差 |
| `gate` | **消费者** | 读 `benchmark.json` → 出 PASS/FAIL 判决 + gate.json | 不碰（毫秒级） |
| `report` | **消费者** | 读 benchmark.json + gate.json + previous/ → 渲染 report.html | 不碰（纯渲染，毫秒级） |
| `audit` | **消费者** | 读 transcript(cc `.jsonl` 或 opencode `.json`)+ SKILL.md → 对账遵循性 | 调 LLM 判定，但审**历史** transcript，**不 re-run cc** |

三者共性 = **post-hoc（跑在 run 之后、以 run 产物为输入）+ 不 spawn cc 跑题 + 可反复跑（改参数重跑不必重跑 cc）**。落到 `gate` 上就是它的核心卖点：改门禁阈值不用重跑 cc、毫秒级重判——调阈值、试不同门禁策略随便反复跑，不心疼 token。

### 11.1 两轴判决

```bash
skill-eval gate output/benchmark.json                        # 仅客观回归守卫
skill-eval gate output/benchmark.json --min-win-rate 0.5     # 加主观 gate
skill-eval gate output/benchmark.json --no-programmatic-guard --min-win-rate 0.6
```

| 轴 | 默认 | 判法 |
|----|------|------|
| 客观侧 programmatic 回归守卫 | **常开** | `delta.pass_rate_programmatic < 0` → FAIL；`≥ 0` → PASS；纯主观题（无客观断言 → 该值为 null）→ 此轴不适用 |
| 主观侧 comparison | opt-in（`--min-win-rate`） | tie-排除胜率 ≥ 阈值 → PASS；< 阈值且决定性比较数（= a_wins + b_wins，排除平局和失败，默认 ≥ `--min-valid`=3）→ FAIL；样本不足 → UNCLEAR |

### 11.2 AND + UNCLEAR/N/A

判决是 **AND**：任一适用轴 FAIL → FAIL。没有 FAIL 但有适用轴信号不足 → **UNCLEAR**（判不准）；无适用轴（守卫关/未请求/无信号）→ **N/A**（不适用）；否则 PASS。

为什么 N/A 也 exit 2 不放行？因为不适用常意味着门禁根本没评上（如配置漏：纯主观题 + 没给 `--min-win-rate` + 关了 programmatic 守卫）。放行会让"没评"伪装成"通过"，有风险。

### 11.3 exit code

```
0 = PASS
1 = FAIL
2 = N/A（不适用）/ UNCLEAR（判不准）/ 使用错误（文件/解析错误）
```

N/A、UNCLEAR、使用错误都退 2，靠 `gate.json` 是否存在 + 其 `decision` 字段区分。

### 11.4 CI 集成

```bash
skill-eval run v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/
skill-eval gate out/benchmark.json --min-win-rate 0.5
echo $?   # 0=PASS(放行) / 1=FAIL(卡合并) / 2=N/A 或 UNCLEAR，消费方定
```

门禁阈值与 CI 落点（分支保护、check 名、N/A/UNCLEAR 是否阻塞）归消费方配置——skill-eval 只提供机制（三态 exit + gate.json）。

### 11.5 gate.json 怎么读

`gate` 跑完写一份 gate.json 到 benchmark.json 同目录（或 `-o` 指定）。它是判决的**完整账本**——CI 靠它分清 exit 2 到底是 N/A 还是 UNCLEAR（见 11.3），人靠它看"为什么这么判"。这是**读者第一次跑最常见的样子**（只给了客观守卫、没给 `--min-win-rate`）：

```json
{
  "decision": "PASS",
  "exit_code": 0,
  "reason": "客观侧无回归(+0.27)",
  "candidate": "with_skill",
  "baseline": "without_skill",
  "thresholds": {"min_win_rate": null, "min_valid": 3, "programmatic_guard": true},
  "axes": {
    "programmatic": {
      "verdict": "PASS",
      "applicable": true,
      "delta": 0.27,
      "reason": "客观侧无回归(+0.27)",
      "win_rate": null, "decisive": null, "a_wins": null, "b_wins": null, "ties": null
    },
    "comparison": {
      "verdict": "N/A",
      "applicable": false,
      "reason": "主观 gate 未请求(未给 --min-win-rate)",
      "delta": null, "win_rate": null, "decisive": null, "a_wins": null, "b_wins": null, "ties": null
    }
  },
  "warnings": []
}
```

读懂它抓 **3 个判断点**：

**① `decision`（4 态）vs `exit_code`（3 值）的映射**——11.3 说过"N/A 和 UNCLEAR 都退 2、靠 decision 区分"，这里收口：

| decision | exit_code |
|---|---|
| PASS | 0 |
| FAIL | 1 |
| N/A（不适用） | 2 |
| UNCLEAR（判不准） | 2 |

CI 脚本只读 exit code 分不清"N/A"和"UNCLEAR"，得读 gate.json 的 `decision` 字段——这正是 11.3 那句"靠 gate.json 区分"的落点。

**② `axes` 里 `applicable` 是读懂产物的钥匙**——为什么上面 comparison 是 `N/A`、一堆 `null`？因为 `applicable: false`（没给 `--min-win-rate`，主观轴根本没介入）。`applicable=false` 的轴**不参与 AND 合并**（11.2），verdict 恒 `N/A`、数值字段恒 `null`；`applicable=true` 但样本不足 → `UNCLEAR`。所以看到某轴 `N/A` 先查 `applicable`：`false` = 你没开这轴，`true` = 开了但判不准。reference 里的样本两轴都 `applicable: true` 都 PASS 是理想态；现实里只跑客观守卫时 comparison 几乎总 `N/A`，别被 null 吓到。

**③ `thresholds` 回显**——你传给 `gate` 的三个阈值原样记进 gate.json：可复现（同一 benchmark + 同一组阈值 → 同一判决），改阈值重判 thresholds 跟着变。`min_win_rate: null` = 没给主观阈值。

> 完整字段（含 `warnings` / `artifact` 等）见 [reference](skill-eval-reference.md) 的「gate.json」段。

---

## 第十二章 report HTML 布局

`skill-eval report output/ --open` 生成自包含的 report.html（run 末尾也会自动产）。它读 `gate.json`（若存在）在标题旁渲染门禁徽章，读 `previous/benchmark.json` 显"vs 上次"卡。两个标签页：

### 12.1 单次执行 tab

浏览单个 run 的 prompt、输出文件、评估详情（打分 + 遵循性 + 校准三层）。顶部下拉按 eval 分组，可直接跳到指定 run；方向键逐条翻。

### 12.2 整体汇总 tab

这是看全局的页面，自上而下：

1. **门禁徽章** — 读 gate.json 显 PASS/FAIL/N/A/UNCLEAR。
2. **评分卡** — 读 scorecard.json（11 项静态体检）。
3. **安全面板** — 读 vet-report.json。
4. **基准测试** — 聚合 Δ 卡（双率 delta）+ 双率对比。
5. **遵循性聚合** — 整个考试聚合的遵循性（per side 聚合 per-run findings，复用 audit 的 aggregate_by_instruction）。
6. **盲评对比** — comparison_summary 胜率 + 逐条比较（rubric/output_quality/expectation_results），揭盲标注 Output A/B 各是哪侧。
7. **深度分析** — 分析备注（见 12.3），有内容才显示。
8. **检查点总览** — per-eval 的程序率 / LLM 率 / 总率（这三个数即"3-dim"）一览（mean across runs），点行跳明细。
9. **逐项评测明细** — per-run 的 3-dim（程序/LLM/总率）列 + 检查项 ✓/✗ 矩阵。
10. **异常检测** — 超时/离群值。
11. **vs 上次** — 读 previous/benchmark.json 显纵向 delta 卡（仅 fresh 重跑产生 previous/ 时）。

> report 里有两套 `runs` 数据来源容易混：`benchmark.runs`（聚合器产出，compliance 恒 null）和 viewer 自己 `find_runs(workspace)` 扫描的 per-run compliance.json。遵循性聚合面板走的是后者（逐个读 per-run compliance.json 再聚合），所以它能在 benchmark.runs.compliance 为 null 时仍然显示遵循性。

### 12.3 深度分析（analysis）

benchmark.json 的 `analysis` 字段来自两个 LLM 分析步骤：

- **analyze_benchmark** → `analysis-notes.json`（整体分析备注）。
- **analyze_comparison** → 深度分析栏（赢家侧的 strengths / improvement_suggestions，只在有清晰赢家时产）。

**赢家怎么定**：winner 取**整体多数票**（所有有效比较里哪侧赢得多，排除 tie/failed；平局或无有效胜负 → 没清晰赢家 → 不产深度分析），代表判词也取整体胜者侧赢的一个比较——不用可能反整体的 `comparisons[0]`（它只是 `(eval_name, run_n)` 字典序第一对，不代表整体）。所以深度分析反映**整体趋势**，不是某一道题。（局限：比较结果不带 eval/run 回指，代表比较的精确 transcript 取不到，分析用的是 config 第一个 run 的 transcript。）

深度分析栏只在 `analysis && (winner_strengths || improvement_suggestions)` 时渲染——所以它出现需要两个条件：① 盲比较有清晰赢家（tie/空不算）；② 那次 analyze_comparison LLM 调用成功。没出现不一定是 bug，可能是 tie 或 LLM 步骤没成功。

---

## 第十三章 路径 1：真任务模式（live）

路径 1 让 Claude 在真实环境里跑真实任务，而不是用 fixture 剧本。

### 13.1 怎么用

```bash
skill-eval run path/to/skill/ --task "重构 X 模块，拆成三个文件" --cwd ./repo --runs 2 -o /tmp/live
skill-eval run path/to/skill/ --task "重构 X 模块，拆成三个文件" --cwd ./repo --executor opencode -o /tmp/live   # 改用 opencode 跑（产 transcript.json）
```

给 `--task` 就进入真任务模式：**忽略 evals.json**，spawn Claude 在 `--cwd` 里跑这个真任务，产 `run-*/transcript.jsonl`。每 run 在终端打印 operational 摘要（耗时/turns/tokens/stop_reason/tool_calls + 结果头部，读自 run_debug.json）。此模式 `--evals`/`--scoring`/`--workers` 等大多不生效。

**执行器**：默认 `cc`（claude code）；给 `--executor opencode` 改用 opencode 跑（产 `run-*/transcript.json`，需 opencode CLI 在 PATH，模型在 `opencode.json` 里配，省略 `--model`）；`--executor cc,opencode` 两个执行器各跑 `--runs` 次。两种 transcript 都能直接喂 audit。

### 13.2 拷贝隔离

`--cwd` 会被**拷贝隔离**——Claude 不碰你的原目录。这是为了安全（真任务可能改文件），也为了让多次 run 互不污染。

### 13.3 与 audit 闭环

路径 1 产的 transcript 可以直接喂给 audit：

```bash
skill-eval audit path/to/skill/ --transcript /tmp/live/run-1/transcript.jsonl -o /tmp/live-audit
```

所以路径 1 + audit = "在真环境跑真任务 → 对账有没有按 skill 做"的完整闭环。`--task` 必须对口 skill（不对口 → audit 假 FAIL）。

### 13.4 路径 1 的边界：它是什么、不是什么

路径 1 信号最真（真实目录 + 真实任务，cc 真跑），但有几条边界别误用：

- **它不判"skill 好不好 / 有没有用"——那是路径 3（A/B 对照）的活。** 路径 1 产的是单次真任务 transcript，没有 with/without 对照；scoring 的 A/B 需要基线。想判 skill 值不值得用 → 路径 3 `run`（带 evals.json），不是这里的 `--task`。路径 1 的产出喂 audit（遵循性诊断）。
- **它是"真任务模式"，不是"真环境模式"。** `--task` 是你给的受控真任务，不是生产系统自然产生的任务流——信号真，但仍是你指定的任务。
- **`--task` 必须对口 skill**（不对口 → cc 不按 skill 走 → audit 假 FAIL；这是使用约束，不是工具 bug）。
- **cc 产物写在隔离拷贝 `output/workspace/`、不写回原目录。** 想看 cc 这次产了什么，去那里看。
- **transcript 是 skill 演进快照。** 审的是 spawn 那一刻 skill 的执行；skill 改了，旧 transcript 审旧版，要重新审得重跑。

---

## 第十四章 batch / batch-report

批量评测多个 skill，按依赖拓扑顺序并行执行。

### 14.1 batch

```bash
skill-eval batch path/to/skills-dir/           # 目录自动 discover
skill-eval batch manifest.json -o batch-workspace/
```

位置参数给 manifest JSON 或 skill 目录（目录则自动扫描其中的 skill）。manifest 描述依赖：

```json
{
  "name": "my-project-skills",
  "skills": [
    {"path": "skills/data-transform", "depends_on": []},
    {"path": "skills/report-gen", "depends_on": ["skills/data-transform"]}
  ]
}
```

按 DAG（有向无环图）拓扑顺序同层并行执行（`--workers` 控并发）。注意 `batch` 的 `--runs` 默认是 3（与 `run` 的默认 1 不同）。

### 14.2 batch-report

```bash
skill-eval batch-report batch-workspace/ --open
```

`batch-report` 是独立顶层命令（非 batch 子命令），生成多 skill 对比 HTML：多 skill 对比、通过率对比、盲评胜率、各 skill 详情。

---

## 第十五章 触发准确度（trigger-accuracy，第 4 维）

前三层（评分 / 遵循性 / 校准）回答的都是 skill 已经"上场"之后的问题——输出对不对、过程遵不遵循、结果×过程协不协调。但有一个更上游的问题它们都没碰：**cc 到底会不会调用这个 skill？**

### 15.1 上游缺口：三层都假设 skill 已"上场"

一个 skill 在真实会话里被不被调用，靠的**只有 frontmatter 的 `description`**——cc 扫到一份 skill、读它的 description、据此决定这次任务要不要够它。description 写得不到位，cc 永不触发，skill body 再好也是死重量。

但前三层都跳过了这一步：

- **评分（run A/B）**：`_build_non_interactive_prompt` 把 skill body **强制注入** cc prompt，开头就是 "You have the following Skill loaded. Follow its instructions"——cc 对"要不要够这个 skill"没得选，触发能力**从不被测**。
- **遵循性（audit）**：审的是 skill 被调用**之后**的行为（默认它已触发）。
- **校准**：结果×过程 coherence，与触发无关。

`description` 这一触发判定唯一依赖的输入，三维都不评。trigger-accuracy 补的就是这个缺口——它和前三层正交：前三层评"上场后表现如何"，trigger 评"会不会上场"。

### 15.2 怎么测：中性 prompt + 干净 HOME

```bash
skill-eval trigger path/to/skill/ --runs 2 -o /tmp/out
```

`trigger` 命令把被测 skill 放进 `<cwd>/.claude/skills/<name>/`，给 cc 一条**中性 prompt**——不点名 skill、不暗示该用它，就是一个普通用户请求。然后看 cc 会不会仅凭 description 自动发现并调用本 skill。检测复用遵循性引擎的 `audit/slice.invoked_skills`（扫 transcript 里的 `Skill` tool_use，看本 skill 名在不在里面）。

**干净 HOME 隔离**：spawn 时把 `HOME` 指向一个临时空目录，压掉用户全局 plugin——否则结果就依赖"测试机今天装了哪些 plugin"，换台机器 / 上 CI 就变，不可移植。隔离后 cc 只看到沙箱里的本 skill（按 description 自动发现）+ 内置 skill（恒定基线）；auth 走 env、不受影响。这条隔离是 trigger 可信的前提：确保触发**只由沙箱里本 skill 的 description 决定**，不被用户环境的其它 skill / plugin 噪声干扰。

### 15.3 两种探题：recall 与 false-trigger 是不同失效模式

trigger 探题分两个方向，各测一种失效：

- **should-trigger**（`expect_trigger: true`）：该 skill 该处理的真任务，措辞**中性**（在 prompt 里点名 skill 就只是测字面匹配，不是测触发）。
- **should-not-trigger**（`expect_trigger: false`）：看着相关但 skill **不该**触发的任务——测 description 会不会过宽、把不该接的活也揽下来。

两种是**不同的失效模式**，所以聚合时**分开报、不合成一个分**：

| 率 | 来自 | 测的失效 | 方向 |
|----|------|---------|------|
| `recall` | should-trigger 方向 trigger_rate 均值 | 漏召（该触发没触发） | 越高越好 |
| `false_trigger_rate` | should-not 方向 trigger_rate 均值 | 误触（不该触发却触发） | 越低越好 |

为什么不合？因为"漏召"和"误触"指向相反的修法：recall 低 → description 太窄 / 缺关键词，要加触发线索；false_trigger 高 → description 太宽 / 太泛，要收窄 scope。合成一个分会把这两个相反信号互相抵消，丢掉诊断价值。

### 15.4 判定与样本

每条 probe 跨 N runs 算 `trigger_rate = fired 次数 / runs`（触发带随机性，`--runs` 多跑聚合成率更可信），阈值 0.5 二值化 → PASS / FAIL。spawn 失败 / 超时 / **cc 非零退出且没触发**（限流 / 后端不可用：cc 实测 exit 1 + 错误打 stdout、stderr 空，`run_trigger` 不 raise 而返正常输出）→ 该 probe = `UNRESOLVED`，**不进 recall / false_trigger_rate 的分母**，且其 `trigger_rate` 置 `null`（没判成、不显假数值率，避免把"没跑出来"误读成"触发率 0"）——宁少报不乱报，避免单次异常污染率（同遵循性"没判成不乱报"的哲学）。某方向 0 probe → 对应率 `null`（N/A，不是假 0.0）。

每个率都带自己的样本量（`n_should_trigger` / `n_should_not`），别脱离样本量解读一个率：init 默认每方向 5 道，<3 道会告警"样本少"。同 1.6 的噪声分工，trigger 侧也有"答题噪声 B"（cc 触发带随机性）——靠 `--runs` 多跑抹平。

### 15.5 出题：`init --trigger-probes`

```bash
skill-eval init path/to/skill/ --trigger-probes                 # 默认每方向 5 道，追加到 trigger_probes 段
skill-eval init path/to/skill/ --trigger-probes --num-evals 8   # 每方向上限 8（trim 多余、不补下限）
```

LLM 读 skill 的 description 生成两方向探题，追加到 evals.json 的 `trigger_probes` 段（新 id 接续已有 `max+1`）。**不重生成已有 scoring evals**——载入已有 evals.json、只加 trigger 探题（`--trigger-probes` 单用、不加 `--append` 也保留原 scoring 题）。

**`--num-evals` 是每方向上限、不是下限**（关键）：LLM 生成的探题多于这个数会被 trim，少于这个数**原样保留**、不强行凑满——硬凑会出重复 / 充数题，反而劣化信号质量。这是有意的"欠交付"：宁可少几道真信号，不要凑数的噪声。所以实际跑出来某方向可能少于你给的 `--num-evals`，别当成 bug。

### 15.6 边界：诊断定位，无门禁

trigger-accuracy 是**诊断工具，不是门禁**：

- **无 gate / 不出 exit-code 判决**——只产 `trigger-report.json` + 终端摘要。recall 低或 false_trigger 高是"该改 description"的信号，不是合并阻断（那是 skill 内容要改、不是 PR 该卡的红灯）。
- **独立 artifact，不并入 `benchmark.json`**——trigger 回答的是上游独立问题，和 scoring / compliance / calibration 不耦合，benchmark 的双率 delta / 盲评胜率都不含触发信号。
- **`description_hash`**（sha256(frontmatter `description`)[:16]）锚定触发判定唯一依赖的输入——跨版本对比时，hash 变了说明 description 改了，recall 的变化才有"是 description 改动引起"的归因依据；hash 没变而 recall 变了，更可能是 cc 触发随机性（看 `--runs`）。

> **成本**：每条 probe = 1 次 cc spawn（有 LLM 成本，但比 `run` 轻——无 with/without 双 spawn、无 grader、无 compliance judge）。配合前三层用：先 `trigger` 看 description 能不能让 cc 够到这个 skill，再 `run` 看上场后表现。

### 15.7 怎么读报告 + 怎么改 description

跑完 `trigger` 产三样：`trigger-report.json`（机器读）+ `trigger-report.html`（人读，**always 写**，与 JSON 并列、对齐 audit，`--open` 浏览器打开）+ 终端摘要。

**终端进度 + 摘要**——跑的过程实时显示进度（每个 probe 开头 `probe i/N`（该触发 / 不该触发）+ cc 的实时动作 `🔧 …` / `✓ 完成`，同 `run` 命令）；跑完是一行汇总 + 逐 probe + 操作建议：

```
recall 75%(5 should-trigger) · false-trigger 10%(5 should-not) · 2 runs · 1 unresolved · 耗时 12.3s · 28400 tok
  [+] probe-1: 100% → PASS (842 tok · 4.5s)
  [-] probe-2: 0%   → FAIL (318 tok · 2.1s)
  [?] probe-3: ?    → UNRESOLVED (0 tok · 0.0s)
操作建议：
  [rule:recall] 漏召:…考虑补触发关键词:
    · probe-2:「…」
  [llm:llm_rewrite] 修订 description:…\n理由:…
Written to …/trigger-report.json · …/trigger-report.html
```

- 汇总行：`recall`（绿，越高越好）/ `false-trigger`（红，越低越好）各带样本量 + runs + unresolved + 耗时（cc spawn 总墙钟，不含 LLM 建议那次调用）+ 总 token。逐 probe `[+/-/?] probe-N: 触发率 → verdict (token · 耗时)`：`+` PASS、`-` FAIL、`?` UNRESOLVED（触发率显 `?`、消耗显 0，因为没判成）。

**怎么改 description**——报告 `suggestions[]` 直接给方向，两路：

| 信号 | 含义 | 怎么改 |
|---|---|---|
| `recall`（should-trigger FAIL） | 漏召：description 没够到该处理的任务 | 补与失败 prompt 匹配的触发关键词 / 动作动词 |
| `false_trigger`（should-not FAIL） | 误触：description 过宽 | 收窄 scope / 加边界排除语（明确不处理这类） |
| `unresolved` | 没判成（限流 / 异常） | 加大 `--runs` 重跑确认，别据未判成下结论 |
| `all_good` | 零 FAIL 零 unresolved | description 触发边界够精准，无需改 |
| `llm_rewrite`（source=llm） | LLM 起草的修订 description 草案 | 可直接抄改（有失败才自动产，带「理由:」） |

规则建议（`recall`/`false_trigger`/`unresolved`/`all_good`）永远给、零成本；LLM 建议（`llm_rewrite`）**有失败才自动跑**（喂当前 description + 失败 probe 的 prompt，起草可抄改的 description + 一行理由），全 PASS 跳过。相对 trigger 本体（N probe × run 次完整 cc spawn）这是单次轻 LLM 调用、<5%，故默认开、无 flag；调用失败只丢这条、规则建议照在。

> `trigger-report.json` 字段全表见 reference 的 `trigger-report.json` 段。建议口径是**诊断、不是门禁**——给「改 description 的方向」，不下 PASS/FAIL 判决（同 15.6）。

---

## 附录

### A. 命令速查

| 命令 | 作用 | 成本 |
|------|------|------|
| `analyze` | 11 项静态体检 | 零 API，<1s |
| `vet` | 4 扫描器安全审查 | 零 API（内置） |
| `init` | 生成 evals.json（LLM/transcript/模板） | LLM（除非纯模板） |
| `add` | 追加单道 eval | LLM（--idea）或零 |
| `inspect` | 题集体检，不下判决 | 零 API |
| `freeze` | 冻结基线快照 | 零 API |
| `run` | A/B 评测主流程 | Claude + LLM |
| `gate` | post-hoc 门禁判决 | 零 API，毫秒 |
| `audit` | 真实 transcript 对账 | LLM |
| `trigger` | 触发准确度（第 4 维，诊断、无门禁） | Claude spawn |
| `report` | 生成 HTML 报告 | 零 API |
| `batch` | 批量评测（DAG） | Claude + LLM |
| `batch-report` | 批量对比 HTML | 零 API |

### B. exit code 全表

| 命令 | 0 | 1 | 2 | 3 |
|------|---|---|---|---|
| `gate` | PASS | FAIL | N/A / UNCLEAR / 使用错误 | — |
| `run` | 产数据成功 | （`--security-gate` 触发） | 输入错误 | 执行错误 |
| `trigger` | 跑完（诊断，无门禁） | — | 输入错误 | 执行错误（cc 缺失/超时） |
| `vet` | SAFE / REVIEW_NEEDED | BLOCKED | 输入错误 | — |
| `analyze` | score 满 | score 未满 | 输入错误 | — |
| `init`/`inspect`/`freeze`/`report` | 成功 | — | 输入错误 | — |

### C. 三层在 run 结果里怎么看

- **结果面（scoring）** → `grading.json` 的 `summary`（双率）；
- **过程面（compliance）** → `compliance.json` 的 `summary`（五态计数）+ findings；
- **结果×过程协调（calibration）** → `calibration.json` 的 `verdict` + `hard_violations`；
- 全开 = `--runs 3`（自动带 compliance + calibration）且不加任何 `--no-*`。`--no-*` 家族：`--no-compliance`（跳遵循性）/ `--no-calibration`（跳校准）/ `--no-static`（跳静态评分卡）/ `--no-vet`（跳安全审查）/ `--no-report`（跳 HTML 报告）。

### D. 第一次跑的最小路径

```bash
# 1. 静态体检 + 安全（零成本）
skill-eval analyze path/to/skill/
skill-eval vet    path/to/skill/

# 2. 出卷
skill-eval init path/to/skill/

# 3. A/B 跑 3 轮（自动带遵循性 + 校准 + report）
skill-eval run path/to/skill/ --runs 3 -o out/

# 4. 看报告
skill-eval report out/ --open

# 5. （CI）出判决
skill-eval gate out/benchmark.json --min-win-rate 0.5
```

### E. 常见坑

- **delta 接近 0 别急着下"skill 没用"**：先排查 references/`source_dir` 有没有正确拷进工作区（skill 依赖读不到 → 开关 skill 效果一样）。
- **expected_output 不要写进 prompt**：它是阅卷参考，拼进发给 Claude 的 prompt = 答案泄露，A/B 失真。
- **遵循性"0 FAIL"≠ 全过**：看报告顶部 UNRESOLVED 警告，没判成的条可能藏真违规，建议重跑。
- **改了 references 内容要强制重跑**：cache_key 不检测引用文件内容改动，用新 `-o` 重跑。
- **门禁 N/A 不是通过**：exit 2 可能是"根本没评上"（配置漏），别当放行。
- **深度分析栏没出现**：通常是盲比较 tie 或那次 LLM 分析没成功，不一定是 bug。
