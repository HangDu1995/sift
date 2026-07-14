# example-skills — 主力示例 skill

4 个结构规整的示例 skill,演示 skill-eval 的评测路径。**每个示例擅长的路径不同**(取决于自带 `evals.json` / `transcript` 与否),照下表选,照命令跑。

## 能力速查

| skill | 领域 / 语言 | 路径3 `run`(evals.json) | 路径2 `audit`(transcript) | 路径1 `--task`(真任务) | analyze / vet |
|---|---|---|---|---|---|
| [log-error-analyzer/](log-error-analyzer/) | 日志分诊 / 英 | ✅ 自带 evals.json | ✅ 自带 transcript | — | ✅ |
| [log-error-analyzer-zh/](log-error-analyzer-zh/) | 日志分诊 / 中 | 需先 `init` | ✅ 自带 transcript | — | ✅ |
| [code-review-auditor/](code-review-auditor/) | 代码 review / 英 | 需先 `init` | — | ✅ 专为真任务设计 | ✅ |
| [release-checklist-zh/](release-checklist-zh/) | 发布自检 / 中 | 需先 `init` | ✅ 自带 transcript | — | ✅ |

> **三条路径**:路径3 `run` 跑 evals.json 评测题 · 路径2 `audit` 对账历史 transcript(不 run)· 路径1 `--task --cwd` 跑真任务(不读 evals.json)。详见 [docs/example-skill.md](../../docs/example-skill.md)。

## 每个怎么跑

### log-error-analyzer(主力示例,canary)

自带 evals.json(2 题)+ transcript,三条路径都能走:

```bash
skill-eval analyze fixtures/example-skills/log-error-analyzer/          # 静态体检(<1s)
skill-eval vet fixtures/example-skills/log-error-analyzer/              # 安全审查
skill-eval run fixtures/example-skills/log-error-analyzer/ --runs 3     # 路径3:跑 evals.json
skill-eval audit fixtures/example-skills/log-error-analyzer/ \
  --transcript fixtures/example-skills/log-error-analyzer/transcripts/synthetic-violation.jsonl   # 路径2
```

> **预期 audit 结果(跑完对照)**:这份 transcript 是合成违规会话,audit 应抓到 **3 个植入违规**——
> - **没读 `references/error-codes.md` 就分类**(步骤2 要求读码表)→ FAIL(evidence:tool_trace 无 Read 该文件)
> - **编造 `E-FAKE-999`**(第 3 行无码应标未分类,却发明了不存在的码)→ FAIL(evidence:码带 FAKE、标 unknown failure)
> - **没写 `error-report.json`**,只口头总结(步骤3 + 输出要求)→ FAIL(evidence:明说 skip writing)
>
> 另:规则"必须引用日志原文行",助手只报"第 1 行/第 2 行"行号、没引原文 → **通常也 FAIL**(LLM 判,偶有方差)。做对的 **PASS**(读日志步骤,tool_trace 命中 Read app.log);`security-policy.md` **N/A**(日志没提安全事件,条件不触发)。
>
> 计数 **pass≈3 / fail≈5 / na≈1**(同文去重后,4 个行为):① 同文重复——编造那句被抽成 `CONSTRAINT` + `ANTI_FABRICATION` 两条,已**合并成一条**(来源徽章显两者,不再虚增 fail);② 同行为跨类——没读码表同时违反 条件性 + 步骤 两条**不同**规则,不合并(确是两条规则),但报告里 `related` **互标 #N** 一眼看到是同一违规。LLM 有 run-to-run 方差,**对照"3 个植入违规是否都出现在 FAIL、evidence 是否点中"即可,别钉精确条数**。

### log-error-analyzer-zh(中文 audit 示例)

自带 transcript(中文对账),无 evals.json——走 audit;要 run 得先 `init` 生成中文 evals.json:

```bash
skill-eval analyze fixtures/example-skills/log-error-analyzer-zh/
skill-eval audit fixtures/example-skills/log-error-analyzer-zh/ \
  --transcript fixtures/example-skills/log-error-analyzer-zh/transcripts/synthetic-violation.jsonl
# 想跑路径3 run,先 init 生成考卷 evals.json(两种方式):
skill-eval init fixtures/example-skills/log-error-analyzer-zh/          # 方式1:从 SKILL.md 直接生成(L1 测例)
skill-eval init fixtures/example-skills/log-error-analyzer-zh/ \
  --from-transcript fixtures/example-skills/log-error-analyzer-zh/transcripts/synthetic-violation.jsonl   # 方式2:从 transcript 挖
```

> **预期 audit 结果**:与上面英文版**完全一致**(同 3 步骤 / 同 reference / 全中文)——3 个植入违规都 **FAIL**(没读 error-codes.md、编造 E-FAKE-999、没写 error-report.json),外加"没引日志原文行"通常也 FAIL;读日志步骤 **PASS**,安全策略 **N/A**。计数与去重/关联拆解同英文版(pass≈3 / fail≈5 / na≈1,4 个行为);LLM 对中文判定不退化(中英对照已验),同样别钉精确条数。

### code-review-auditor(路径1 真任务)

专为 `--task --cwd` 设计(审 Python 代码,任务对口真代码库),无 evals.json / transcript:

```bash
skill-eval analyze fixtures/example-skills/code-review-auditor/
# 路径1:在真代码库上跑真任务(--cwd 拷贝隔离,不碰原),产物可喂 audit
skill-eval run fixtures/example-skills/code-review-auditor/ \
  --task "审 audit/aggregate.py 找 bug,写 review-report.json" \
  --cwd <真代码库路径> --runs 2
skill-eval audit fixtures/example-skills/code-review-auditor/ \
  --transcript <上面 run 产的 run-1/transcript.jsonl>
```

### release-checklist-zh(中文多步骤 audit)

自带 transcript,无 evals.json——走 audit:

```bash
skill-eval analyze fixtures/example-skills/release-checklist-zh/
skill-eval audit fixtures/example-skills/release-checklist-zh/ \
  --transcript fixtures/example-skills/release-checklist-zh/transcripts/synthetic-violation.jsonl
# 想跑路径3 run,先 init 生成考卷(两种方式):
skill-eval init fixtures/example-skills/release-checklist-zh/           # 方式1:从 SKILL.md 直接生成
skill-eval init fixtures/example-skills/release-checklist-zh/ \
  --from-transcript fixtures/example-skills/release-checklist-zh/transcripts/synthetic-violation.jsonl   # 方式2:从 transcript 挖
```

> **预期 audit 结果(跑完对照)**:这份 transcript 跑了测试 + 打了 tag(这两步该 **PASS**),但应抓到这些违规——
> - **breaking change 没审批**(承认有 breaking change 却"直接打 tag 发布",没等用户确认)→ **FAIL**
> - **没查 lint**(没跑 ruff check)/ **谎报 lint·依赖通过**(没读 dependency-policy.md 却说"确认过了")→ **FAIL**
> - **没读 references**(dependency-policy / changelog-format / version-rules 都没读)→ **FAIL**(evidence:tool_trace 无 Read)
> - **没生成 changelog、没更新 version.py** → **FAIL**
>
> 典型计数 **pass≈11 / fail≈12 / na≈5**(total≈28)。这个 skill 故意复杂(7 步骤 + 代码块 + 示例列表),extractor 会把"示例当步骤、代码块当输出格式"抽成噪音,所以信噪比低于简单 skill、会出现少量假 FAIL;但真违规仍穿透噪音被抓。同样别钉精确条数,对照"上面这些违规是否都出现在 FAIL"即可。

> analyze / vet 默认只终端输出(显式 `-o` 才落盘);run 默认产齐 benchmark + scorecard + vet-report 到 `output/iteration-1/`,随后 `skill-eval report output/iteration-1/` 出自包含 HTML 报告。完整命令参考 [skill-eval-guide](../../tools/skills/skill-eval-guide/SKILL.md)。
