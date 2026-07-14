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

> analyze / vet 默认只终端输出(显式 `-o` 才落盘);run 默认产齐 benchmark + scorecard + vet-report 到 `output/iteration-1/`,随后 `skill-eval report output/iteration-1/` 出自包含 HTML 报告。完整命令参考 [skill-eval-guide](../../tools/skills/skill-eval-guide/SKILL.md)。
