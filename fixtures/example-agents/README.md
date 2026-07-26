# example-agents — agent-audit 可运行示例

> 与 [example-skills/](../example-skills/) 平级:那边演示**审 skill**(`Skill` 调用切段),
> 这边演示**审 agent**(`agent` 归属切段——被 dispatch 的 agent,无 `Skill` 调用锚点)。

## 这是什么

两个**域无关**的 agent 声明,加一份它们被 dispatch 过的 multi-agent session,用来演示
`skill-eval audit` 对 agent 的对账。agent 和 skill 在 skill-eval 里是**同一个抽象**
(一份声明的指令 + 一段执行段),所以审 agent 用**同一个 `audit` 命令**——区别只在切段
靠 `agent` 字段(被 dispatch)而非 `Skill` 调用(被 invoke)。

| agent | 角色 | 可核验的指令 |
|---|---|---|
| [test-runner/](test-runner/) | 跑测试 + 核查覆盖率 + 落报告 | 跑 `pytest`、覆盖率 ≥80%、**严禁** `pytest -x`、写 `TEST_REPORT.md`、禁止捏造 |
| [lint-reviewer/](lint-reviewer/) | 跑 linter + 复查 | 使用 `./lint.sh`、跑 `ruff check`、必须报全错误、修复后必须复查 |

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

### 期望 verdict(混合 = 对账有信号)

审 **test-runner**:
- ✅ PASS:跑了 `pytest`(程序命中 `match_tool_call`)、写了 `TEST_REPORT.md`;
- ❌ FAIL:用了 `pytest -x`,违反「**严禁** `-x`」约束;
- 二次 dispatch → 报告里 test-runner 出现**两段**执行。

审 **lint-reviewer**:
- ✅ PASS:用了 `./lint.sh`、跑了 `ruff check`;
- 段里**不含** test-runner 的 `pytest` 调用(按 `agent` 字段隔离)。

(真 LLM 实跑:种下的 `pytest -x` 违规被 FAIL 抓到,遵守的 PASS——见
`tests/test_agent_audit_example.py::test_full_audit_real_llm`。)
