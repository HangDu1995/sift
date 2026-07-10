# Example Skill: log-error-analyzer

`fixtures/example-skills/log-error-analyzer/` 是 skill-eval 的**金丝雀示例 skill**——结构规整、可被三条评测路径共用,兼作 audit(路径2 transcript 对账)有效性的验证夹具。

## 为什么有它

audit(路径2)的有效性需要正面验证:**真 LLM 在干净输入下能不能抓到真实违规**。此前两个 case 都不给力:

- **真实 skill**(dispatching-parallel-agents)是教程型,extractor 把示例文字当指令(49 条伪步骤),源头污染——LLM 再准也 0 真问题。
- **mock 集成测试**(`test_audit_integration`)mock 了 LLM,只证管线逻辑(违规→FAIL 检出),不证"真 LLM 判得准"。

log-error-analyzer 量身定做:SKILL.md 写得 extractor 友好(真步骤/禁令/reference,**无代码块**避 OUTPUT_FORMAT 假阳性),合成 transcript 植入**真实感**违规,真 LLM 跑 audit 看抓不抓得到。

## 三路径共用

| 路径 | 命令 | 用 example skill 的什么 |
|---|---|---|
| 路径1 live | `init` → `run` | `SKILL.md` + `evals/evals.json` |
| 路径2 audit | `audit --transcript` | `SKILL.md` + `transcripts/synthetic-violation.jsonl` |
| 路径3 fixture | `init --from-transcript` → `run` | `SKILL.md` + transcript |

路径2 和路径3 **共用同一份 transcript**(audit 对账它、init --from-transcript 挖它)。

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

**这是首次正面证明:真 LLM 在干净输入下能抓违规。** 此前 dispatching(伪指令污染)和 mock 测试(mock LLM)都没覆盖。

### 验证中暴露并修复的 audit 缺口

合成验证暴露了一个真 bug:`_judge_step` 只把 `transcript_text`(assistant 文字)喂给 LLM,**不喂 `tool_trace`**。导致涉及工具调用的步骤,LLM 看不到实际调用,只看文字声称——"Read input log" 步骤明明调了 Read,却因 transcript 只剩"我先把日志读一下"的文字声称而**误判 FAIL**。同一 Read 调用,条件性判定(命中 tool_trace)却看到 → PASS,自相矛盾。

修复:`tool_trace` 进 step 判定 prompt(`auditor.py`),修复后 "Read input log" 正确 **PASS**,证据"实际工具调用记录中有 Read /tmp/.../app.log"。详见 `tests/test_auditor.py::TestJudgeStep::test_tool_trace_included_in_prompt`。

## 怎么跑

```bash
# 路径2 audit —— 合成验证(真 LLM,约 1-3 分钟)
.venv/bin/skill-eval audit fixtures/example-skills/log-error-analyzer/ \
  --transcript fixtures/example-skills/log-error-analyzer/transcripts/synthetic-violation.jsonl \
  -o /tmp/example-audit

# canary 回归(真 LLM,默认 skip;工具改动后手动跑复验判定逻辑没退化)
RUN_SLOW=1 .venv/bin/pytest tests/test_example_skill_canary.py -q
```

## live 真跑结论(真实性)

`run --runs 1`(spawn Claude with_skill 真跑 L2)产生**真实(非植入)transcript**——Claude 做对了:Read×3(读了 `app.log` + `error-codes.md` + `security-policy.md`)+ Write(写了 `error-report.json`),无编造。

audit 这个真实 transcript:**total=10, pass=10, fail=0, na=0**——全 PASS,零假 FAIL。

这验证真实有效性的另一面:**干净会话不乱报**。与合成验证互补:

| 层 | 输入 | 结论 |
|---|---|---|
| 合成(有效性下界) | 植入 3 个违规的会话 | 真 LLM 全抓到(FAIL) |
| live(真实性) | Claude 真跑做对的会话 | 全 PASS,0 假 FAIL |

两层结合 = audit 在干净输入下**既抓得到真违规,又不冤枉干净会话**。对比 dispatching(伪指令污染 + 假 FAIL):干净 skill + 干净会话,audit 判定准确。

技术闭环也验证通:`run` dump 的 stream-json transcript(`Task 3`)被 audit 的 `build_digest` 正确解析——live 真跑产物能直接喂 audit。

scoring 层印证:with_skill 83% vs without_skill 54%(盲比较 with_skill 胜)——example skill 真实有用。
