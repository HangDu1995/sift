---
name: skill-eval-guide
description: >
  Use this skill whenever you need to evaluate, test, benchmark, or quality-check any skill (SKILL.md).
  Trigger on: "evaluate", "test", "benchmark", "跑评测", "评测", "跑一下", "测试", "全面测评",
  "跑所有测评", "验证skill", "检查skill质量", "安全审查", "vet", "静态分析", "analyze", "过拟合",
  "overfit", "集成测试", "batch", "跑batch", "多skill评测", "扫描skills", "scan", "生成配置",
  "generate-config", "优化触发", "optimize-trigger", "report", "报告", "init", "初始化evals".
  This skill provides EXACT commands to run for every skill-eval operation.
type: guide
domain: quality-assurance
---

# Skill Eval Guide — Agent 操作手册

用 `skill-eval` CLI 对任意 skill 执行评测。

## 环境安装

### 如果 skill-eval 未安装

向用户索要 GitHub URL，然后 clone 并安装。**如果不能确定 URL，必须先问用户。**

```bash
# 用户提供 URL，例如 https://github.com/example/skill-eval
git clone <用户提供的URL> /tmp/skill-eval
cd /tmp/skill-eval
pip install -e .
skill-eval --help   # 验证
```

### 环境检查

```bash
claude --version          # 必须：skill-eval 依赖 claude -p
skill-eval --version      # 必须：评测工具
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
| 做 diverse eval / 过拟合检测但没有 source project | "过拟合检测需要源项目路径（如 Android 项目），请提供" |
| 做 batch 评测但没有 skills 目录 | "batch 评测需要一个包含多个 skill 的目录或 manifest.json，请提供" |
| 做 regression 但没有上次 benchmark | "regression 需要 baseline benchmark.json，请提供" |

---

## 13 个命令的意图映射

根据用户的话选择正确的命令：

| 用户说了什么 | 命令 | 说明 |
|---|---|---|
| "安全审查" / "vet" / "安全检查" | `vet` | 多扫描器安全审查 |
| "静态分析" / "analyze" / "评分卡" / "打分" | `analyze` | 11 项结构化评分 |
| "生成 evals" / "init evals" / "初始化" | `init` | LLM 自动生成 evals.json |
| "跑测试" / "A/B 测试" / "run" | `run` | with vs without skill 对比 |
| "一键评测" / "full" / "全流程" / "完整评测" | `full` | vet + analyze + run + overfit + report |
| "跑所有测评" / "全面测评" | 先 `full` → 再 `vet-overfit` → 再 `batch` + `batch-report` |
| "批量评测" / "batch" / "多 skill" | `batch` + `batch-report` | 多 skill unit/integration/regression |
| "过拟合检测" / "overfit" / "泛化" | `vet-overfit` | domain vs general 对比 |
| "扫描 skills" / "覆盖率检查" / "scan" | `scan` | 检查哪些 skill 缺少 evals |
| "生成配置" / "config" / "generate-config" | `generate-config` | LLM 生成 eval-config.yaml |
| "优化触发" / "trigger" / "描述优化" | `optimize-trigger` | 优化 SKILL.md description |
| "生成报告" / "report" / "报告" | `report` | 生成 HTML 报告（单 skill） |
| "批量报告" / "batch-report" | `batch-report` | 生成 HTML 报告（多 skill） |
| "导入数据" / "import" | `import` | 导入 skill-monitor JSONL |

---

## 命令速查

### `vet` — 安全审查

```bash
skill-eval vet /path/to/skill                 # 终端表格输出
skill-eval vet /path/to/skill --format json   # JSON 输出
skill-eval vet /path/to/skill --skip-external # 只跑内置扫描器
```

4 个扫描器：aguara、skill-scanner、secrets-scan、structure-check。
结果自动写入 `SKILL_PATH/vet-report.json`。

### `analyze` — 静态分析

```bash
skill-eval analyze /path/to/skill
```

11 项检查：description、name、行数、示例、防幻觉等。输出 `scorecard.json`。

### `init` — 生成 evals.json

```bash
# 基本用法（LLM 生成高质量 evals，推荐）
skill-eval init /path/to/skill --output /path/to/skill/evals.json

# 限制数量
skill-eval init /path/to/skill --num-evals 10

# 生成 general-domain evals（用于过拟合检测，需提供源项目）
skill-eval init /path/to/skill --source-dir /path/to/android-project --domains web,cli
```

**init 自动做的事：**
1. 解析 SKILL.md 结构，用 LLM 生成针对性测试用例
2. 自动生成 programmatic assertion（FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS 等）
3. 检查 expectation 质量，避免模糊表述

**注意：** 如果用户要测过拟合但没给源项目路径，必须先追问。例如："要做过拟合检测的话，需要提供一个源项目目录（如 Android 项目），我可以帮你生成 diverse-domain evals。请提供项目路径。"

### `scan` — L0 覆盖率扫描

```bash
skill-eval scan /path/to/skills-directory/
```

检查 evals.json 存在性、assertion 类型合法性、覆盖率。

### `generate-config` — 生成配置文件

```bash
skill-eval generate-config /path/to/skills-directory/
```

LLM 扫描所有 skill frontmatter 生成 `eval-config.yaml`（domain_templates、integration_scenarios、pipelines）。

### `run` — A/B 测试

```bash
skill-eval run /path/to/skill \
  --runs 1 --timeout 300 --eval-samples 5 \
  --output test-results/NAME/
```

关键参数：`--runs`、`--timeout`、`--eval-samples`、`--workers`、`--max-turns`、`--scoring`。

### `full` — 一键全流程（推荐首选）

```bash
skill-eval full /path/to/skill --output test-results/NAME/
```

自动执行：vet → analyze → load evals → A/B test → overfit → report。
默认 `--timeout 600`、`--max-turns 20`。
用 `--eval-samples 5` 先验证，再全量。

### `batch` — 批量评测

```bash
# 用 skills 目录
skill-eval batch /path/to/skills-directory/ \
  --layers unit,integration --runs 1 --output test-results/batch/

# 用 manifest.json
skill-eval batch manifest.json --layers unit,integration,regression
```

三层测试：unit（A/B）、integration（chain/routing/contract/pipeline）、regression（与上次对比）。
Manifest 格式：
```json
{"name": "project", "skills": [{"path": "skills/s1", "depends_on": []}]}
```

### `batch-report` — 批量报告

```bash
skill-eval batch-report test-results/batch/ --open
```

### `vet-overfit` — 过拟合检测

```bash
skill-eval vet-overfit \
  --domain-bench iteration-1/benchmark.json \
  --general-bench general/benchmark.json \
  --output iteration-1/overfitting_report.json
```

有 `--general-bench` 才有 gap analysis；没有则用基础评分。

### `report` — 生成报告

```bash
skill-eval report test-results/NAME/iteration-1/ --open
```

自动发现 benchmark.json、scorecard.json、overfitting_report.json、vet-report.json。

### `optimize-trigger` — 优化触发描述

```bash
skill-eval optimize-trigger /path/to/skill --eval-set trigger-evals.json --max-iterations 5
```

### `import` — 导入外部数据

```bash
skill-eval import monitor-data.jsonl --benchmark iteration-1/benchmark.json
```

---

## 标准操作流程

### 首次评测一个 skill（无 evals.json）

```bash
SKILL="/path/to/skill"
OUT="test-results/NAME"

# Step 0: 检查 evals.json
if [ ! -f "$SKILL/evals.json" ]; then
    echo "No evals.json found, generating..."
    skill-eval init "$SKILL" --output "$SKILL/evals.json"
fi

# Step 1: 安全审查
skill-eval vet "$SKILL"

# Step 2: 静态分析
skill-eval analyze "$SKILL"

# Step 3: A/B 测试
skill-eval full "$SKILL" --output "$OUT"

# Step 4: 打开报告
skill-eval report "$OUT/iteration-1/" --open
```

### "跑所有测评" / "全面测评"

```bash
SKILL="/path/to/skill"
OUT="test-results/NAME"

# Step 1: 安全审查
skill-eval vet "$SKILL"

# Step 2: 静态分析
skill-eval analyze "$SKILL"

# Step 3: 确保 evals.json 质量
python3 -c "
import json
e = json.load(open('$SKILL/evals.json'))
evals = e.get('evals',[])
vague = sum(1 for ev in evals for x in ev.get('expectations',[]) if 'follows' in str(x).lower())
if vague > len(evals)*0.3:
    print('NEEDS_REGEN')
"
# 如果需要，重新生成：
# skill-eval init "$SKILL" --output "$SKILL/evals.json"

# Step 4: A/B 测试（默认已包含 vet + analyze + overfit + report）
skill-eval full "$SKILL" --output "$OUT"

# Step 5: 验证结果
ls "$OUT/iteration-1/benchmark.json"
ls "$OUT/iteration-1/report.html"

# Step 6: 打开报告
skill-eval report "$OUT/iteration-1/" --open
```

### "快速验证"

```bash
skill-eval full /path/to/skill --eval-samples 3 --runs 1 --output test-results/quick/
```

### "批量评测多个 skill"

```bash
skill-eval batch /path/to/skills-dir/ --layers unit --runs 1 --output test-results/batch/
skill-eval batch-report test-results/batch/
```

---

## 报告完整性检查

```bash
python3 << 'PYEOF'
from pathlib import Path; import json
it = Path("test-results/NAME/iteration-1")
ok = True
for f in ['benchmark.json','report.html']:
    if not (it/f).exists(): print(f'MISSING: {f}'); ok = False
if (it/'scorecard.json').exists():
    s=json.load(open(it/'scorecard.json')); print(f'scorecard: {s["score"]}/{s["max_score"]}')
if (it/'vet-report.json').exists():
    v=json.load(open(it/'vet-report.json')); print(f'vet: {v["verdict"]}')
if (it/'overfitting_report.json').exists():
    o=json.load(open(it/'overfitting_report.json')); print(f'overfit: {o["overfitting_score"]:.2f}')
if (it/'benchmark.json').exists():
    b=json.load(open(it/'benchmark.json'))
    ws=[r for r in b.get('runs',[]) if r['configuration']=='with_skill']
    if ws: print(f'pass_rate: {sum(r["result"]["pass_rate"] for r in ws)/len(ws):.0%}')
print('ALL OK' if ok else 'ISSUES')
PYEOF
```

## 常见问题

| 问题 | 解决 |
|---|---|
| skill-eval 未安装 | 向用户索要 GitHub URL，clone + `pip install -e .` |
| evals 质量差（模糊 expectation） | `skill-eval init /path/to/skill` 重新用 LLM 生成 |
| pass_rate 全 0% | expectation 太模糊，重新 init；或用 `--scoring llm` |
| 超时 | `--timeout 600`，`--eval-samples 5` |
| without_skill 无 grading | 正常（无 skill 时 Claude 可能不产出可评判内容） |
| calibration UNRELIABLE | 改进 evals 具体性，加 programmatic assertion |
| benchmark.json 缺失 | aggregation 失败，单独 `skill-eval report iteration-1/` |
| report 面板空白 | 缺对应 JSON 文件，把 vet-report/scorecard/overfitting 文件 cp 到 iteration-1/ |
| batch 显示 0 skills | 输入不是 skills 目录（需含 skills/ 子目录）或 manifest.json 格式不对 |
