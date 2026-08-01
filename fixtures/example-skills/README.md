# example-skills — 主力示例 skill

5 个示例 skill + 1 条多 skill `--kind skill` 切分 demo transcript,演示 skill-eval 的评测路径。**每个示例擅长的路径不同**(取决于自带 `evals.json` / `transcript` / `trigger_probes` 与否),照下表选,照命令跑。

## 能力速查

| skill | 领域 / 语言 | 路径3 `run`(evals.json) | 路径2 `audit`(transcript) | 路径1 `--task`(真任务) | 路径4 `trigger`(触发) | analyze / vet |
|---|---|---|---|---|---|---|
| [log-error-analyzer/](log-error-analyzer/) | 日志分诊 / 英 | ✅ 自带 evals.json | ✅ 自带 transcript | — | 需 `init --trigger-probes` | ✅ |
| [log-error-analyzer-zh/](log-error-analyzer-zh/) | 日志分诊 / 中 | ✅ 自带 evals.json | ✅ 自带 transcript | — | 需 `init --trigger-probes` | ✅ |
| [code-review-auditor/](code-review-auditor/) | 代码 review / 英 | 需先 `init` | — | ✅ 专为真任务设计 | 需 `init --trigger-probes` | ✅ |
| [release-checklist-zh/](release-checklist-zh/) | 发布自检 / 中 | 需先 `init` | ✅ 自带 transcript | — | 需 `init --trigger-probes` | ✅ |
| [incident-reporter/](incident-reporter/) | 事故分诊 / 中 | — | — | — | ✅ 自带 trigger_probes | ✅ |

> **三条路径**:路径3 `run` 跑 evals.json 评测题 · 路径2 `audit` 对账历史 transcript(不 run)· 路径1 `--task --cwd` 跑真任务(不读 evals.json)。详见 [docs/example-skill.md](../../docs/example-skill.md)。

> **关于示例 transcript(分三类、各做什么)**:每个 audit 示例的 `transcripts/` 里通常有两三类:
> - **`synthetic-violation.jsonl`** — 人为构造、**故意违规**的合成会话(不是真 session)。audit 的职责是判"transcript 遵不遵循 skill",全做对的干净 transcript 只会全 PASS、演示不出价值,所以这份当**反面教材**给 audit 抓。下面每个示例的「预期 audit 结果」列了具体植入的违规。
> - **`synthetic-clean.jsonl`** — 人为构造的**正确执行**合成会话(读该读的 references、写对产物、不编造)。是 `init --from-transcript` **出卷验证的正源**——违规 transcript 是边缘案例(没产出 skill 声明的产物),挖不到客观题;clean 才能验证 mining 真能挖出客观断言。
> - **`real-clean.jsonl`**(仅 log-en / log-zh)— **真跑** path-1(`run --task --cwd` spawn cc)产出的真实正确会话,和 `synthetic-clean` **同任务同输入**,用来**对照手构 vs 真跑**的出卷效果(真跑 transcript 大/杂、25+ 条 vs 手构 8 条)。release-zh 7 步太复杂、真跑难一次跑对,故只手构。
>
> 另两个不带这种 transcript:`code-review-auditor` 走路径1 真任务(自己 run 出来再审),`multi-skill-demo` 靠 `build_multi_skill.py` 脚本生成(验的是切分纯度,不是确定性违规)。
>
> **opencode 格式 transcript(`opencode-sessions/`)**:audit `--transcript` 除 cc `.jsonl` 外也吃 **opencode session `.json`** —— 即真实 `opencode export <sessionID>` 的产出(顶层 `info`/`messages`,parts 嵌在 message 里、role/agent 在 `message.info`;与 cc fixture 用真 cc jsonl 同理,用真 opencode export 格式,非自造)。两者重建成同一 `SessionDigest`、对账等价。`log-error-analyzer-zh/opencode-sessions/synthetic-violation.json` 是 cc 同名 `.jsonl` 的 opencode 镜像(同 prompt、同 3 植入违规、同样不写报告);多 skill 版见下面 multi-skill-demo 的 `build_multi_skill_opencode.py`。生产导出真会话:`opencode session list` 找 id → `opencode export <id> > session.json` → audit。**拿到一个 .db 文件**(别处拷来的库):直接 `audit --db handed.db`——内部自动 list/export/audit(单 session 自动选,多个列出挑;底层三步见 [reference 的 audit 段](../../docs/skill-eval-reference.md))。

## 每个怎么跑

### log-error-analyzer(主力示例,canary)

自带 evals.json(2 题)+ transcript,三条路径都能走:

> **这份 `evals.json` 是手写考卷,不是 `init` 自动生成的**:2 题人工金标准——L1 测规则理解(读日志 / 查 `error-codes.md` / 不编造 / 报告三字段),L2 用**和违规 transcript 同一条 `app.log`** 但编码**正确答案**(E-AUTH-001 / E-DB-002 分类、无码第三行归 `unclassified` 而非编造),可作正确基线对照。别的示例(zh / code-review / release)都没自带考卷,得 `init` 自动生成(LLM 出题、需复核);注意跑 `init` 会**覆盖**这份手写版,默认 ship 的是手写。

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

> **出卷验证:用 clean transcript 挖(`init --from-transcript` 正源)**:挖客观题该用 `synthetic-clean.jsonl`(正确执行、产出了 error-report.json),不是 `synthetic-violation.jsonl`(边缘案例、没产出 → 挖不到客观题)。实测从 synthetic-clean 挖出 `FILE_EXISTS error-report.json` + `JSON_HAS_FIELD(total_errors/errors/unclassified)` + 真实错误码 `CONTAINS_PATTERN` 等客观断言(同任务多条细则合并成 1 题、零降级):
> ```bash
> skill-eval init fixtures/example-skills/log-error-analyzer/ \
>   --from-transcript fixtures/example-skills/log-error-analyzer/transcripts/synthetic-clean.jsonl   # 手构 clean(8 条)
> #   换 real-clean.jsonl(真跑,25+ 条)同任务同输入,可对照手构 vs 真跑的出题差异
> ```
>
> 实测对照(en/zh 一致):两者都挖出核心客观断言(`FILE_EXISTS error-report.json` + schema 字段 + 真实码);**手构**干净聚焦(同任务多条合并成 1 题),**真跑**更丰富(6-7 题,还多捕获 `security` 字段——真 cc 主动多读了 `security-policy.md`)。手构=确定可控,真跑=反映真实 cc 行为(含小怪癖),正是两者并存对照的意义。

### log-error-analyzer-zh(中文示例)

自带 transcript(中文对账)+ evals.json(路径3 也能直接 run):

```bash
skill-eval analyze fixtures/example-skills/log-error-analyzer-zh/
skill-eval run fixtures/example-skills/log-error-analyzer-zh/ --runs 3                               # 路径3:跑自带的 evals.json
skill-eval audit fixtures/example-skills/log-error-analyzer-zh/ \
  --transcript fixtures/example-skills/log-error-analyzer-zh/transcripts/synthetic-violation.jsonl   # 路径2(cc 格式)
# 同一份违规场景的 opencode 格式镜像(`opencode export` 产出的真格式,同 prompt/同 3 植入违规):
skill-eval audit fixtures/example-skills/log-error-analyzer-zh/ \
  --transcript fixtures/example-skills/log-error-analyzer-zh/opencode-sessions/synthetic-violation.json
# 想重新生成考卷(会覆盖自带版,两种方式):
skill-eval init fixtures/example-skills/log-error-analyzer-zh/                                       # 方式1:从 SKILL.md 直接生成
skill-eval init fixtures/example-skills/log-error-analyzer-zh/ \
  --from-transcript fixtures/example-skills/log-error-analyzer-zh/transcripts/synthetic-clean.jsonl  # 方式2:从 clean transcript 挖(自带版即此方式产出)
```

> **这份 `evals.json` 是 `init --from-transcript` 从 `synthetic-clean.jsonl` 产的(7 题),不是手写**——对比:英文版 log-error-analyzer 的 evals.json 是 2 题手写金标准(见上)。注意 `--from-transcript` **非纯挖**:7 道里**只有 eval-1(`happy_path`)的 fixture 逐字节来自 transcript**(agent 真实 Read 的 app.log),**其余 6 道是 LLM 综合 SKILL 规则 + transcript 任务生成的压测场景**(空日志 / 全未知码 / 相似码陷阱 / 注入陷阱 等),**fixture 不在 transcript 里**——它们不是 transcript 场景的变体,而是围绕 skill 规则编的独立测试场景(generator 的 prompt 同时喂 LLM 「SKILL 定义 + transcript digest」,要求生成 5-8 道,单 transcript 只够 1 个真实场景,其余必由 skill 规则衍生)。故这份考卷需复核(不像手写是人工金标准);跑 `init` 会**覆盖**自带版。

> **预期 audit 结果**:与上面英文版**完全一致**(同 3 步骤 / 同 reference / 全中文)——3 个植入违规都 **FAIL**(没读 error-codes.md、编造 E-FAKE-999、没写 error-report.json),外加"没引日志原文行"通常也 FAIL;读日志步骤 **PASS**,安全策略 **N/A**。计数与去重/关联拆解同英文版(pass≈3 / fail≈5 / na≈1,4 个行为);LLM 对中文判定不退化(中英对照已验),同样别钉精确条数。**opencode 格式镜像**(`opencode-sessions/synthetic-violation.json`)审出同一批违规——两种格式重建出同一 `SessionDigest`(cc↔opencode 等价门测试已锁),audit 结果一致。

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
  --from-transcript fixtures/example-skills/release-checklist-zh/transcripts/synthetic-clean.jsonl       # 方式2:从 clean transcript 挖(正源;violation 是边缘案例)
```

> **预期 audit 结果(跑完对照)**:这份 transcript 跑了测试 + 打了 tag(这两步该 **PASS**),但应抓到这些违规——
> - **breaking change 没审批**(承认有 breaking change 却"直接打 tag 发布",没等用户确认)→ **FAIL**
> - **没查 lint**(没跑 ruff check)/ **谎报 lint·依赖通过**(没读 dependency-policy.md 却说"确认过了")→ **FAIL**
> - **没读 references**(dependency-policy / changelog-format / version-rules 都没读)→ **FAIL**(evidence:tool_trace 无 Read)
> - **没生成 changelog、没更新 version.py** → **FAIL**
>
> 典型计数 **pass≈11 / fail≈12 / na≈5**(total≈28)。这个 skill 故意复杂(7 步骤 + 代码块 + 示例列表),extractor 会把"示例当步骤、代码块当输出格式"抽成噪音,所以信噪比低于简单 skill、会出现少量假 FAIL;但真违规仍穿透噪音被抓。同样别钉精确条数,对照"上面这些违规是否都出现在 FAIL"即可。

### incident-reporter(`trigger` 触发准确度演示)

上面 4 个演示 run / audit / task;这一个专为 [`trigger`](../../docs/skill-eval-reference.md)(第 4 维)演示——**frontmatter description 故意写得不精准**,让真跑容易暴露 description 的触发问题,演示 trigger 报告 + 改进建议怎么读。

description = 「处理文本内容,生成结构化报告。」——真实 scope 是**事故报告**(见 body),但 description 没说「事故/incident」,且「文本内容/报告」过宽。自带 5 条 `trigger_probes`(2 条该触发的 incident 任务 + 3 条不该触发的相近文本任务):

```bash
skill-eval trigger fixtures/example-skills/incident-reporter/ --runs 2 -o /tmp/trig
```

> **trigger 真跑 cc、结果动态**(每 probe × run = 1 次 cc spawn,凭 description 决定是否触发)——不像 audit example 能列固定预期违规。这个 description **故意不精准**以提高演示 FAIL 的概率:
> - **该触发的 incident 任务**(probe 1-2):description 没够到「事故/故障」措辞 → 可能**漏召**(recall 低 → FAIL);
> - **不该触发的文本任务**(probe 3-5:转文字稿 / 摘要 / 周报):description「处理文本内容 / 生成报告」过宽 → 可能**误触**(false-trigger 高 → FAIL)。
>
> 跑完看 `trigger-report.json` + `trigger-report.html` + 终端:recall / false-trigger 各带样本量 + 耗时 + 总 token;per-probe verdict + 消耗;**操作建议**区——规则建议(漏召 → 补「事故/incident」关键词;误触 → 收窄到 incident scope)+ 有失败时自动起草的 LLM 改写 description 草案。**实际结果取决于 cc**(可能只看到误触、只看到漏召、或混合),核心学怎么读 recall / false-trigger + 用建议改 description。trigger 定位诊断、无 gate(不进 `benchmark.json`)。

### multi-skill-demo(`--kind skill` 多 skill 切分对账)

上面 4 个是**单 skill** 示例;这一个演示**多 skill 会话的 `--kind skill` 切分**(audit 对账 / mining 出卷都支持)。transcript 里交错调了 log-error-analyzer / code-review-auditor / release-checklist-zh(log-error-analyzer 调两次 + 一个子 agent 调 code-review-auditor),`--kind skill` 只取指定 skill 被调用期间的段。两份 transcript **结构相同、产物不同**:

- `multi-skill.jsonl` —— **违规版**(各 skill 产物刻意缺字段/缺段),给 `audit --kind skill` 验切片纯度 + 各段独立抓违规。
- `multi-skill-clean.jsonl` —— **好版本**(各 skill 产物正确),给 `init --from-transcript --kind skill` 演示多 skill 切分出卷(各 skill 挖各的干净考卷)。

两份都由 `build_multi_skill.py` 确定性生成(重生成:`.venv/bin/python fixtures/example-skills/multi-skill-demo/build_multi_skill.py`)。

> **opencode 版**(`build_multi_skill_opencode.py`):同场景、同违规/好产物,但 opencode session 格式(`multi-skill.json` / `multi-skill-clean.json`,5 段 agent 切换 LEA→CRA→RCZ→LEA→CRA,用 per-message `agent` 字段而非 cc 的 `Skill` tool_use)。重生成:`.venv/bin/python fixtures/example-skills/multi-skill-demo/build_multi_skill_opencode.py`。**切分语义差异(诚实)**:cc 用 `parent_tool_use_id` 让主 LEA span 跨子 agent 不断;opencode `agent` 字段扁平,子 agent / `task` 工具 = 另一段 agent run → 不建模 cc 的"跨子 agent 交错"。故 opencode 版 LEA/CRA 各切 2 段、RCZ 1 段(切分数同,交错结构简化)。

**audit(违规版 `multi-skill.jsonl`)—— 抓各 skill 段的违规:**

```bash
# 只对账 log-error-analyzer 被调用的段(切出 2 段:主作用域两次调用,不含 src/CHANGELOG):
skill-eval audit fixtures/example-skills/log-error-analyzer/ \
  --transcript fixtures/example-skills/multi-skill-demo/multi-skill.jsonl --kind skill
# 换审 code-review-auditor(切出 2 段:主作用域 1 + 子 agent 里 1):
skill-eval audit fixtures/example-skills/code-review-auditor/ \
  --transcript fixtures/example-skills/multi-skill-demo/multi-skill.jsonl --kind skill
# release-checklist-zh(1 段):
skill-eval audit fixtures/example-skills/release-checklist-zh/ \
  --transcript fixtures/example-skills/multi-skill-demo/multi-skill.jsonl --kind skill
```

**opencode 版同样可审(`multi-skill.json`,切分按 per-message `agent`):**

```bash
skill-eval audit fixtures/example-skills/log-error-analyzer/ \
  --transcript fixtures/example-skills/multi-skill-demo/multi-skill.json --kind skill
```

**mining 出卷(好版本 `multi-skill-clean.jsonl`)—— 各 skill 挖各的干净考卷:**

```bash
skill-eval init fixtures/example-skills/log-error-analyzer/ \
  --from-transcript fixtures/example-skills/multi-skill-demo/multi-skill-clean.jsonl --kind skill
# 换挖 code-review-auditor / release-checklist-zh,同样加 --kind skill
```

> **切分出卷零串味(实测)**:一份 `multi-skill-clean.jsonl`(3 skill 交错 + 子 agent 调 code-review),加 `--kind skill` 各挖各的、断言路径全是本 skill 自己的产物——log-error-analyzer → `error-report.json`(5 eval)、code-review-auditor → `review-report.json` / `src/orders.py`(5 eval)、release-checklist-zh → `CHANGELOG.md`(7 eval);**互不污染**(log 的考卷不提 review-report/CHANGELOG,反之亦然)。不传 `--kind skill` + 检测到多 skill 会警告(防把别 skill 活动当本 skill 挖)。

> **跑时的进度行 `对账 transcript i/N`**:N = 切片数 = 该 skill 在 transcript 里被调用的次数(log-error-analyzer / code-review-auditor = 2,release-checklist-zh = 1),i 是第几段;每段切片落盘在 `<输出目录>/slices/`(文件名形如 `multi-skill-log-error-analyzer-1-1.jsonl`),想看每段具体切了啥可直接打开。

> **预期(这个 demo 验的是"切分不串味",不是确定性违规)**:每个 skill 的报告**只反映自己被调用那几段**——log-error-analyzer 的报告不提 `src/*.py` / CHANGELOG;code-review-auditor 的不提 `logs/*.log`;**子 agent 里那次 code-review-auditor 调用切到子 agent 返回,不污染主 agent 的 log-error-analyzer 归属**(scope-aware 切分的关键)。transcript 里各 skill 产物刻意不完美,但能否被抓取决于 auditor 判定层(这几个 skill 的输出要求是散文 / OUTPUT 检查对措辞不对文件,故多为 LLM 辅助判定、非确定性)——本 demo 的核心是**切片纯度**,不是"违规必抓"。归段细节(scope-aware / 子 agent 作用域 / 两限制)见 [reference 的 audit 段](../../docs/skill-eval-reference.md)。

> analyze / vet 默认只终端输出(显式 `-o` 才落盘);run 默认产齐 benchmark + scorecard + vet-report 到 `output/iteration-1/`,随后 `skill-eval report output/iteration-1/` 出自包含 HTML 报告。完整命令参考 [skill-eval-guide](../../tools/skills/skill-eval-guide/SKILL.md)。
