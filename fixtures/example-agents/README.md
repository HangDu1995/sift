# example-agents — agent 评测/审计可运行示例

> 与 [example-skills/](../example-skills/) 平级:那边演示**审 skill**(`Skill` 调用切段),
> 这边演示 agent 的两条路——**审 agent**(`audit --kind agent` 按 `agent` 归属切段)和
> **评测 agent**(`critique --kind agent` 内容质量 + `init --kind agent` 出题 + `run --kind agent` 打分)。

## 这是什么

三份**域无关**的 agent 声明,演示 skill-eval 对 agent 的两条产品线:

- **审计线**(test-runner / lint-reviewer):两份短、规则化的 agent + 一份它们被 dispatch 过的
  multi-agent session,演示 `audit` 对 agent 的遵循性对账。agent 和 skill 在 skill-eval 里是
  **同一个抽象**(一份声明的指令 + 一段执行段),审 agent 用同一个 `audit` 命令——区别只在
  切段靠 `agent` 字段(被 dispatch)而非 `Skill` 调用(被 invoke)。
- **评测线**([pr-reviewer.md](pr-reviewer/pr-reviewer.md)):一份**特意为展示 critique + 出题 + 打分而设计**的
  agent——角色(PR diff 审查)够丰富、写作质量有精心安排的强弱分布(失败机制 / 可执行步骤 /
  高风险黑名单写得实,但**故意没给完整 worked example**),让 `critique` 抓到真实缺口、
  `init` 出多样且对口角色的题(含反面/边界题),`run` 再用这些题打**绝对质量分**(无 A/B 对照)。
  另带一份 [transcripts/synthetic-clean.jsonl](pr-reviewer/transcripts/synthetic-clean.jsonl)
  (pr-reviewer 审一个含硬编码密钥 + 未引用 `rm -rf` 的 PR 的 session),演示
  `init --kind agent --from-transcript` **mining 出题**(从 agent 真实使用 session 挖题,
  prompt 走 agent 导向,区别于从角色直接出题)。

| agent | 角色 | 演示 | 可核验/可出的指令 |
|---|---|---|---|
| [test-runner/](test-runner/) | 跑测试 + 核查覆盖率 + 落报告 | audit | 跑 `pytest`、覆盖率 ≥80%、**严禁** `pytest -x`、写 `TEST_REPORT.md`、禁止捏造 |
| [lint-reviewer/](lint-reviewer/) | 跑 linter + 复查 | audit | 使用 `./lint.sh`、跑 `ruff check`、必须报全错误、修复后必须复查 |
| [pr-reviewer.md](pr-reviewer/pr-reviewer.md) | 审查 PR diff 给行级建议 | critique + 出题 + 打分 | 失败机制(大 diff/AI 生成/批量重命名)+ 黑名单(硬编码密钥/`rm -rf`/测试绿放行)+ 边界(不改码/不 nitpick) |

### 为什么域无关(不绑特定运行环境)

agent 的指令必须是在**任何 dev 环境都能真做**的动作(pytest / ruff / 写文件),这样示例
session 的 verdict 才是"对账抓到了 misbehavior"的信号,而不是"环境缺失导致全 FAIL"的噪声。
绑特定运行环境的真实 agent(需要真实构建产物 / 硬件 / 数据集的那类),其有意义对账要靠
**真实 session**(真有那些环境产物),由调用方端到端跑,不靠这种自包含 fixture。

## 示例 session:[multi-agent-session.json](multi-agent-session.json)

一个 main 依次 dispatch 了 **两个** agent、还**二次** dispatch 了 test-runner:

```
main → test-runner(sub-tr1)→ lint-reviewer(sub-lr)→ 再 test-runner(sub-tr2)
```

每个 test-runner 段都**种了**一个可被对账抓到的违规:用了 `pytest -x`(违反 test-runner
的「严禁 `-x`」约束)。遵守的规则(跑了 `pytest`、写了报告、lint 用了统一入口)照做。

session 是**结构化 records** 形(producer 契约:子 agent turn 带 `agent` 字段、不带 `result`
——audit 判定不读工具结果)。这正是调用方(观测平台 / harness 等)吐的形态。

## 怎么跑

### 审计线:test-runner / lint-reviewer

agent 声明已经物化成 `<name>/SKILL.md`(真实接入时,调用方把 agent `.md` 正文物化成
`SKILL.md` 喂 audit;此处预先物化好,示例可直接跑):

```bash
# 审 test-runner(只切 test-runner 的执行段,不含 lint-reviewer 的)
skill-eval audit fixtures/example-agents/test-runner/ \
  --transcript fixtures/example-agents/multi-agent-session.json \
  --kind agent -o /tmp/test-runner-audit

# 审 lint-reviewer
skill-eval audit fixtures/example-agents/lint-reviewer/ \
  --transcript fixtures/example-agents/multi-agent-session.json \
  --kind agent -o /tmp/lint-reviewer-audit
```

> 审 agent 用 `--kind agent`(按 `agent` 归属切段,区别于审 skill 的 `--kind skill` 按
> `Skill` 调用切)。详见 [docs/skill-eval-handbook.md](../../docs/skill-eval-handbook.md) §7.7。

### 评测线:pr-reviewer(critique + 出题 + 打分)

pr-reviewer 是原生 `.md`(真实 agent 形态,非物化 SKILL.md),直接喂文件路径;init 出的考卷默认写 `pr-reviewer/evals/evals.json`(像 skill 的 `<skill>/evals/`):

```bash
# critique:审 agent 定义正文的内容质量(4 维读者反馈)
skill-eval critique fixtures/example-agents/pr-reviewer/pr-reviewer.md --kind agent -o /tmp/pr-reviewer-critique

# init:给 agent 出题(从角色造 role-matching 考题)——默认写 pr-reviewer/evals/evals.json(像 skill)
skill-eval init fixtures/example-agents/pr-reviewer/pr-reviewer.md --kind agent

# init --from-transcript:mining 出题(从 agent 真实使用 session 挖题,另一条出题路)
skill-eval init fixtures/example-agents/pr-reviewer/pr-reviewer.md --kind agent \
  --from-transcript fixtures/example-agents/pr-reviewer/transcripts/synthetic-clean.jsonl

# run:用出的题打 agent 绝对质量分(body 经 --append-system-prompt 注入 cc system 层,prompt 原样)
skill-eval run fixtures/example-agents/pr-reviewer/pr-reviewer.md --kind agent \
  --evals fixtures/example-agents/pr-reviewer/evals/evals.json --runs 2 -o /tmp/pr-reviewer-run
```

**期望产出(真 LLM 实跑验证过):**

`critique` → 4 维 **3 满足 + 1 部分**(混合 = 有诊断信号,非橡皮图章):
- ✅ 失败机制 / 可执行具体性 / 高风险黑名单 都满足——三块都写了「栽的原因 / 怎么核 / 为什么会害死人」,不是空喊;
- 🟡 示例驱动 部分——有零碎真实碎片(空变量炸 `/`、glob 改名误伤),但**没有完整 worked example**(贴一段真实 hunk 演示 Read→Grep→产出)。这是 pr-reviewer **故意留的真实缺口**,演示 critique 抓改进点的价值。

`init` → 数道 L1 题,**覆盖 pr-reviewer 的三类规则**——失败机制(大 diff skim / AI 生成幻觉 / 批量重命名误伤)、高风险黑名单(硬编码密钥 / `rm -rf` 未引用 / 测试绿放行)、边界;含考 agent 正确地「不做某事」的**反面边界题**(无阻塞时克制不 nitpick)。每道 rationale 都精准映射到一条具体规则。**每道带 fixtures**(待审代码文件落地,prompt 引用路径——agent 真 Read/Grep 文件而非读 prompt 文本);断言带 `scope:"transcript"`(查 agent 的 review 文本,fixture 是输入不查它)。

`init --from-transcript`(mining)→ 从 transcript 真实 session 挖题,产出 L1 expectations 为主(pr-reviewer 只读不产文件 → 无 `FILE_EXISTS`);**断言带 `scope:"transcript"`**(查 agent 的 review 文本,不查输入文件——只读 agent 的"答案"是 review 文字不在文件里,见 [evals-format.md](../../docs/evals-format.md) 程序化表)。transcript 种了硬编码密钥 + 未引用 `rm -rf` 双黑名单,挖出的题锚定 agent 正文职责(失败机制 / 黑名单 / 严重度),prompt 走 agent 导向(角色 / 工具白名单 / role-matching)。自带 [evals/evals.json](pr-reviewer/evals/evals.json) 即此 mining 版(提交进仓);init(从角色 或 mining)默认写此路径,重新生成会覆盖。

`run` → **绝对 pass_rate**(无 A/B 对照、无 delta / 胜率)。自带考卷是 mining 版(5 道);pass_rate 单 run / 单卷浮动——直出 7 道某次实跑混合约 **93%**:黑名单 / 边界题多满分,失败机制题在八成档——部分道会因 agent 用了**非字面表述**而漏个别 `CONTAINS_PATTERN` 断言(如认出了硬编码 key 却没引用字面 `AKIA`),客观率(programmatic,卡字面)因此低于混合率。这是「断言 vs 自然表述」的真实诊断信号,非全 0 / 全 100 空转。控制台只打 `Pass rate: agent NN%`(无 vs 噪音);产物落 `agent/eval-*/run-*/`(grading / checkpoints / transcript / compliance),`benchmark.json` 的 `run_summary` 只有 `agent` 一个 key、**无 `delta`**。

> 出题组成与 run 的 pass_rate 都会浮动(LLM 造题 + cc 方差),但稳定覆盖这三类规则、pass_rate 形态(绝对质量高分非空转)稳定;上面是定性描述,不写死具体道数 / 百分比。

### 期望 verdict(审计线,混合 = 对账有信号)

审 **test-runner**:
- ✅ PASS:跑了 `pytest`(程序命中 `match_tool_call`)、写了 `TEST_REPORT.md`;
- ❌ FAIL:用了 `pytest -x`,违反「**严禁** `-x`」约束;
- 二次 dispatch → 报告里 test-runner 出现**两段**执行。

审 **lint-reviewer**:
- ✅ PASS:用了 `./lint.sh`、跑了 `ruff check`;
- 段里**不含** test-runner 的 `pytest` 调用(按 `agent` 字段隔离)。

(真 LLM 实跑:种下的 `pytest -x` 违规被 FAIL 抓到,遵守的 PASS——见
`tests/test_agent_audit_example.py::test_full_audit_real_llm`。)
