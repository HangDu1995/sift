# Example Skill: log-error-analyzer

`fixtures/example-skills/log-error-analyzer/` 是 sift 的**主力示例 skill**(canary:工具改评测逻辑后最先用它复验判定没退化)——结构规整、可被评测与审计两面共用,兼作 audit(transcript 对账)有效性的验证夹具。

## 为什么有它

audit 的有效性需要正面验证:**真 LLM 在干净输入下能不能抓到真实违规**。此前两个 case 都不给力:

- **真实 skill**(dispatching-parallel-agents)是教程型,extractor 把示例文字当指令(49 条伪步骤),源头污染——LLM 再准也 0 真问题。(2026-07-21 起)此类教程型伪指令污染现可经 `audit`/`eval` 的 `--refine-instructions` 在判定前 LLM 精筛砍掉(默认开,精度>召回);log-error-analyzer 仍坚持 extractor 友好写法,但别处遇教程型 skill 不必再手改 SKILL。
- **mock 集成测试**(`test_audit_integration`)mock 了 LLM,只证管线逻辑(违规→FAIL 检出),不证"真 LLM 判得准"。

log-error-analyzer 量身定做:SKILL.md 写得 extractor 友好(真步骤/禁令/reference,**无代码块**避 OUTPUT_FORMAT 假阳性),合成 transcript 植入**真实感**违规,真 LLM 跑 audit 看抓不抓得到。

## 评测与审计两面共用

example skill 的 `SKILL.md` 在评测与审计两面(以及 `--task` 真任务模式)都用作被测对象:

| 用法 | 怎么跑 | example skill 用什么 | 状态 |
|---|---|---|---|
| **评测**(fixture eval) | `init` → `eval`(evals.json 剧本 spawn;`--from-transcript` 是剧本来自 transcript 的变体) | `SKILL.md` + `evals.json` / transcript | ✅ 已验证 |
| **审计**(transcript 对账) | `audit --transcript`(不 run,审历史 trace) | `SKILL.md` + `transcripts/synthetic-violation.jsonl` | ✅ 已验证(canary) |
| **`--task` 真任务模式** | `run --task/--cwd` spawn 执行器(默认 cc;`--executor opencode`)在真任务上真跑(--cwd 拷贝隔离),审 re-run 的新 trace | `SKILL.md` + `--task`(真任务)+ `--cwd`(真代码库/真数据,非 fixture) | ✅ 已实现 |

> **`--task` 真任务模式**:`run --task/--cwd` 不读 evals.json,直接 spawn 跑真任务(默认 `cc`;`--executor opencode` 改用 opencode 跑,或 `--executor cc,opencode` 两个都跑;--cwd 真代码库/真数据,拷贝隔离)。example skill 自带的 `evals.json` 仍是 fixture(用它 `eval` = 评测);`--task` 用 example skill 的 `SKILL.md` 当被测对象,配 `--task` + `--cwd`——code-review-auditor 即此模式的验证(审 sift 真代码)。审 opencode 跑出的 session 已支持:audit `--transcript` 除 cc `.jsonl` 外也吃 opencode session `.json`。
>
> 审计和评测的 `--from-transcript` 变体**共用同一份 transcript**(audit 对账它、init --from-transcript 挖它)。

## 文件结构

```
fixtures/example-skills/log-error-analyzer/
├── SKILL.md                          # extractor 友好(Steps/Rules/References,无代码块)
├── references/
│   ├── error-codes.md                # 错误码表(合成违规里"没读"它 + 编造不存在的码)
│   └── security-policy.md            # 安全升级规则(条件触发 → N/A 验证)
├── evals/
│   └── evals.json                    # L1 规则理解 + L2 执行(读日志写报告)
└── transcripts/
    └── synthetic-violation.jsonl     # 植入 3 个已知违规的金标准会话
```

## 合成验证结论(有效性下界)

真 LLM 跑 audit,**抓到全部 3 个植入违规**:

| 植入违规 | verdict | LLM 证据(可核对) |
|---|---|---|
| 没读 `references/error-codes.md` 就分类 | **FAIL** | 场景应触发但 trace 未读取该文件 |
| 编造 `E-FAKE-999`(error-codes.md 里没有) | **FAIL** | 名称带 FAKE + 标注 unknown failure,凭空编造 |
| 没写 `error-report.json`,只口头总结 | **FAIL** | 明确说 skip writing |

做对的也正确 **PASS**(Read 工具命中、Read 日志步骤、反推 app.log);`security-policy.md` 正确 **N/A**(日志无安全事件,场景不触发)。

> **fail 怎么数 / 去重后**(标准答案按"SKILL 几条规则 × 这会话违反几条"数全):4 个行为 = 3 植入 + 规则"必须引用日志原文行"被违反(助手只报行号、没引原文)。同句"禁止编造"被 extractor 抽成 `CONSTRAINT` + `ANTI_FABRICATION` 两条同文——**已去重合并成一条**(来源徽章显两者),故 **fail=5**(不是 6)。另:没读 error-codes.md 同时违反 REFERENCE_USAGE + HEADING_STEP 两条**不同**规则——不合并(确是两条规则),但报告里 **related 互标 #N** 可一眼看到是同一违规。读 evidence 对照**行为**,别数条数。

**这是首次正面证明:真 LLM 在干净输入下能抓违规。** 此前 dispatching(伪指令污染)和 mock 测试(mock LLM)都没覆盖。

### 验证中暴露并修复的 audit 缺口

合成验证暴露了一个真 bug:`_judge_step` 只把 `transcript_text`(assistant 文字)喂给 LLM,**不喂 `tool_trace`**。导致涉及工具调用的步骤,LLM 看不到实际调用,只看文字声称——"Read input log" 步骤明明调了 Read,却因 transcript 只剩"我先把日志读一下"的文字声称而**误判 FAIL**。同一 Read 调用,条件性判定(命中 tool_trace)却看到 → PASS,自相矛盾。

修复:`tool_trace` 进 step 判定 prompt(`auditor.py`),修复后 "Read input log" 正确 **PASS**,证据"实际工具调用记录中有 Read /tmp/.../app.log"。详见 `tests/test_auditor.py::TestJudgeStep::test_tool_trace_included_in_prompt`。

## 中文版与中英对比

`log-error-analyzer-zh` 是英文版的**中文对照**(同 3 步骤/同 reference/同 3 违规,全中文),覆盖中文 extractor 分支(`禁止/必须` 约束 + `调用` 工具词 + `编造` 反捏造)——这些分支在英文 example 上从未触发。

中英 audit 结果**完全一致**(都 pass=3/fail=5/na=1,同文去重后),3 个植入违规都抓到,中文证据同样具体可核对(E-FAKE-999 暗示 FAKE、"第1行"不算引用原文)。**LLM 对中文判定没有退化**。

## 复杂 skill:extractor 容忍度

`release-checklist-zh` 是**受控版 dispatching**——故意复杂(7 步骤 + 3 reference + 嵌套条件 + 代码块 + 示例列表),测 audit 在真实复杂 skill 下真违规能否穿透 extractor 噪音被抓。

extractor 抽出 28 条,含可预见噪音:5 个**示例当步骤**(`breaking change 时 MAJOR 加一`、`<新功能>`、`<bug 修复>` 等)+ 1 个**代码块当 OUTPUT_FORMAT**(Bug B)+ 工具词重复(`调用 Bash`+`运行 pytest` 各抽一次)。

audit 结果(pass=11/fail=12/na=5):**真违规大多穿透噪音被抓**——没查 lint、没读 references、breaking change 没审批、没生成 changelog、没更新版本号都 FAIL,证据靠 `tool_trace` 修复精准定位"文字声称但无实际调用"。但有噪音代价:3 个假 FAIL(`运行 pytest/ruff/git` 被当独立工具,实际是 Bash 子命令,match 不命中)+ 1 个 OUTPUT_FORMAT 假 FAIL(Bug B);1 个漏报(谎报 lint 的禁令没抓,但"查 lint"步骤角度补救了)。

**结论:简单 skill 干净准确(3 违规全抓、0 假);复杂 skill 信噪比下降,但真违规仍可抓。** 这是 audit 的真实上限——复杂 skill 的噪音是 extractor 已知局限(治本要等结构化声明),audit 的价值在于真违规仍能穿透噪音被定位。

## 怎么跑

```bash
# audit —— 合成验证(真 LLM,约 1-3 分钟)
.venv/bin/sift audit fixtures/example-skills/log-error-analyzer/ \
  --transcript fixtures/example-skills/log-error-analyzer/transcripts/synthetic-violation.jsonl \
  -o /tmp/example-audit

# canary 回归(真 LLM,默认 skip;工具改动后手动跑复验判定逻辑没退化)
RUN_SLOW=1 .venv/bin/pytest tests/test_example_skill_canary.py -q
```

## 真跑结论(真实性)

`eval --runs 1`(spawn Claude with_skill 真跑 L2)产生**真实(非植入)transcript**——Claude 做对了:Read×3(读了 `app.log` + `error-codes.md` + `security-policy.md`)+ Write(写了 `error-report.json`),无编造。

audit 这个真实 transcript:**total=10, pass=10, fail=0, na=0**——全 PASS,零假 FAIL。

这验证真实有效性的另一面:**干净会话不乱报**。与合成验证互补:

| 层 | 输入 | 结论 |
|---|---|---|
| 合成(有效性下界) | 植入 3 个违规的会话 | 真 LLM 全抓到(FAIL) |
| live(真实性) | Claude 真跑做对的会话 | 全 PASS,0 假 FAIL |

两层结合 = audit 在干净输入下**既抓得到真违规,又不冤枉干净会话**。对比 dispatching(伪指令污染 + 假 FAIL):干净 skill + 干净会话,audit 判定准确。

技术闭环也验证通:`run` dump 的 stream-json transcript(`Task 3`)被 audit 的 `build_digest` 正确解析——live 真跑产物能直接喂 audit。

scoring 层印证:with_skill 83% vs without_skill 54%(盲比较 with_skill 胜)——example skill 真实有用。

## scoring 层额外发现:total_errors 定义歧义(非植入,已修复)

这 skill 的植入缺陷是 **compliance 类**(过程违规,给 audit 抓,见「合成验证结论」)。scoring 层在 eval-1(log 含 2 个真码 + 1 行无码)上还顺手暴露一个**非植入**的输出问题,演示了双通道评分的价值:

- `SKILL.md` 只说「`total_errors` 整数」「错误总数」,**没钉死 total_errors 数什么**(只数 errors?还是 errors+unclassified?)。
- with_skill 据此自由发挥,产出 `total_errors=3`(= errors 2 + unclassified 1);eval 的程序化检查 `total_errors=2` 判 **FAIL**(三 run 全 FAIL)。
- 同一条「total_errors 应为 2」走双通道**结果打架**:客观(programmatic 正则)**FAIL**(3≠2,抓对),主观(LLM 语义)**PASS**(LLM grader 对精确数值不较真,假放行)。

不是 skill 坏了、也不是刻意埋的雷——是 skill 指令本身的含糊。留着它反而演示了 scoring 双通道的意义:**LLM 单判会假绿,程序化锚点兜住真值**,两通道之差(`pass_rate_programmatic` vs `pass_rate_llm`)就是信号。

> **已于 2026-07-21 在 SKILL.md 钉死 `total_errors = len(errors)`(不含 unclassified),消除该歧义。** 此 skill 现为干净范本(中英版同步);重跑后 with_skill 会产出 `total_errors=2`、三层信号一致,上述「双通道打架」演示不再复现。本段保留为历史记录,说明双通道如何暴露指令含糊。

## code-review-auditor:`--task` 真任务模式验证

`code-review-auditor` 是专为 **`--task` 真任务模式** 设计的 example skill——审 Python 代码找 bug/坏味道,任务天然对口真代码库(不像 log-error-analyzer 审日志)。用它验证 `run --task/--cwd` 真任务模式端到端。

### 三处分工
- **评测**(evals.json fixture):log-error-analyzer 已验(合成 + 真跑)。
- **审计**(audit 对账):log-error-analyzer 合成违规已验(canary)。
- **`--task` 真任务模式**:code-review-auditor + `run --task/--cwd`——本节。

### 怎么跑(`--task` 真任务模式)

```bash
# spawn cc with code-review-auditor,审 sift 真代码(--cwd 拷贝隔离不碰原)
.venv/bin/sift run fixtures/example-skills/code-review-auditor/ \
  --task "审 audit/aggregate.py 找 bug 写 review-report.json" \
  --cwd src/sift --runs 2 --max-turns 15 -o /tmp/cra-live

# audit 审真任务产出的 trace(--task 模式 dump 的 run-*/transcript.jsonl)
.venv/bin/sift audit fixtures/example-skills/code-review-auditor/ \
  --transcript /tmp/cra-live/run-1/transcript.jsonl \
  --transcript /tmp/cra-live/run-2/transcript.jsonl -o /tmp/live-audit
```

### 端到端验证结论
- **cc 真实行为**:Read 目标代码 → Read `code-patterns.md` → 深入 grep/Read 核实 → Write `review-report.json`。报告**真问题引用行**(L17 未用 import、L65-66 bare else 静默归 NA 等),没编造。
- **audit**:total=35 pass=35 fail=0,证据硬(每条 Read/Write 命中 tool_trace、行号具体、NEVER fabricate 证据"grep 校验 import 确实未用"),**不是漏审的全 PASS 假象**。
- **验证面**:`--task` 真任务模式的"干净会话不乱报"——cc 真遵循 skill → audit 全 PASS 证据硬,与 log-error-analyzer 合成验证("违规抓得到")互补。

### 通用性
`run --task/--cwd` 对任何 skill 通用(code-review 审代码 / 算子生成按规格 / 日志分析),同一套参数。`--cwd` 是工作目录(任务材料),不绑代码——材料是目录里的真实文件,`--task` 决定怎么用。
