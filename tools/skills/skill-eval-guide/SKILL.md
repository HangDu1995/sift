---
name: skill-eval-guide
description: >
  Use this skill whenever you need to evaluate, test, benchmark, or quality-check any skill (SKILL.md).
  Trigger on: "evaluate", "test", "benchmark", "跑评测", "评测", "跑一下", "测试", "全面测评",
  "跑所有测评", "验证skill", "检查skill质量", "安全审查", "vet", "静态分析", "analyze",
  "batch", "跑batch", "多skill评测", "report", "报告", "init", "初始化evals".
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
python3 --version         # 必须：>= 3.10
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
| 做 batch 评测但没有 skills 目录或 manifest.json | "batch 评测需要一个包含多个 skill 的目录或 manifest.json，请提供" |

---

## 命令意图映射

| 用户说了什么 | 命令 | 说明 |
|---|---|---|
| "安全审查" / "vet" / "安全检查" | `vet` | 多扫描器安全审查 |
| "静态分析" / "analyze" / "评分卡" / "打分" | `analyze` | 11 项结构化评分 |
| "生成 evals" / "init evals" / "初始化" | `init` | LLM 自动生成 evals.json |
| "跑测试" / "A/B 测试" / "run" | `run` | with vs without skill 对比 |
| "跑所有测评" / "全面测评" | 先 `vet` → `analyze` → `run` → `report` |
| "批量评测" / "batch" / "多 skill" | `batch` + `batch-report` | 多 skill 单元测试 |
| "生成报告" / "report" / "报告" | `report` | 生成 HTML 报告（单 skill） |
| "批量报告" / "batch-report" | `batch-report` | 生成 HTML 报告（多 skill） |

---

## 命令速查

### `vet` — 安全审查

```bash
skill-eval vet /path/to/skill                 # 终端表格输出
skill-eval vet /path/to/skill --format json   # JSON 输出
skill-eval vet /path/to/skill --skip-external # 只跑内置扫描器
```

4 个扫描器：aguara、skill-scanner、secrets-scan、structure-check。

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
```

**init 自动做的事：**
1. 解析 SKILL.md 结构，用 LLM 生成针对性测试用例
2. 自动生成 programmatic assertion（FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS 等）
3. 检查 expectation 质量，避免模糊表述

### `run` — A/B 测试

```bash
skill-eval run /path/to/skill \
  --runs 1 --timeout 300 \
  --output test-results/NAME/
```

关键参数：`--runs`（默认 1，不足 3 时自动跳过遵循性检查，推荐 3）、`--timeout`、`--eval-samples`、`--workers`、`--max-turns`、`--scoring`、`--no-compliance`、`--compliance-gate`。

每个 eval 独立并发执行，产出 grading.json、compliance.json、calibration.json。

### `report` — 生成报告

```bash
skill-eval report test-results/NAME/iteration-1/                    # 生成静态 report.html
skill-eval report test-results/NAME/iteration-1/ --open             # 生成并打开
```

自动发现 benchmark.json、scorecard.json、vet-report.json。

### `batch` — 批量评测

```bash
# 用 skills 目录
skill-eval batch /path/to/skills-directory/ \
  --layers unit --runs 1 --output test-results/batch/

# 用 manifest.json
skill-eval batch manifest.json --runs 3
```

Manifest 格式：
```json
{"name": "project", "skills": [{"path": "skills/s1", "depends_on": []}]}
```

### `batch-report` — 批量报告

```bash
skill-eval batch-report test-results/batch/
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

# Step 3: A/B 测试（默认 runs=1 快速验证，加 --runs 3 获得统计有效性）
skill-eval run "$SKILL" --output "$OUT"

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

# Step 3: A/B 测试（runs=3 获得统计有效性 + 遵循性检查）
skill-eval run "$SKILL" --runs 3 --compliance-gate --output "$OUT"

# Step 4: 验证结果
ls "$OUT/iteration-1/benchmark.json"
ls "$OUT/iteration-1/report.html"

# Step 5: 打开报告
skill-eval report "$OUT/iteration-1/" --open
```

### "快速验证"

```bash
skill-eval run /path/to/skill --eval-samples 3 --output test-results/quick/
```

### "批量评测多个 skill"

```bash
skill-eval batch /path/to/skills-dir/ --runs 1 --output test-results/batch/
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
if (it/'benchmark.json').exists():
    b=json.load(open(it/'benchmark.json'))
    ws=[r for r in b.get('runs',[]) if r['configuration']=='with_skill']
    if ws: print(f'pass_rate: {sum(r["result"]["pass_rate"] for r in ws)/len(ws):.0%}')
print('ALL OK' if ok else 'ISSUES')
PYEOF
```

## 常见问题

| 问题 | 解决 |
|---|---|---|
| skill-eval 未安装 | 向用户索要 GitHub URL，clone + `pip install -e .` |
| evals 质量差（模糊 expectation） | `skill-eval init /path/to/skill` 重新用 LLM 生成 |
| pass_rate 全 0% | expectation 太模糊，重新 init；或用 `--scoring llm` |
| 超时 | `--timeout 600`，`--eval-samples 5` |
| without_skill 无 grading | 正常（无 skill 时 Claude 可能不产出可评判内容） |
| calibration UNRELIABLE | 检查是否有反复 UNEVAL 的断言，考虑重新设计 evals |
| benchmark.json 缺失 | aggregation 失败，单独 `skill-eval report iteration-1/` |
| report 面板空白 | 缺对应 JSON 文件，把 vet-report/scorecard 文件 cp 到 iteration-1/ |
| batch 显示 0 skills | 输入不是 skills 目录或 manifest.json 格式不对 |
| LLM 断言返回 "Not evaluated" | 断言会自动重试（最多 2 次）；仍 UNEVAL 则标记为 INDETERMINATE |
| 单次运行方差大 | `--runs 1` 默认仅快速验证；推荐 `--runs 3` 启用异常检测和统计有效性 |
| 遵循性率低 | SKILL.md 指令有矛盾或太难遵循，重写指令 |

---

## 断言设计原则

### 两层断言体系

| 层级 | 类型 | 判定 | 不适用 |
|------|------|------|--------|
| **Tier 1** 确定性 | `FILE_EXISTS`, `CONTAINS_PATTERN`, `NOT_CONTAINS`, `TOOL_CALLED`, `STEP_COUNT` | 正则/文件检查 | 语义判断 |
| **Tier 3** 语义性 | `LLM_GRADE`, `LLM_FACT`, `LLM_NOT_DELEGATE` | LLM 打分+举证 | 简单字面匹配 |

### 关键规则

1. **同一维度只用一层**：不要同时放 `NOT_CONTAINS "spec-evolver"` 和 `LLM_NOT_DELEGATE`
2. **Tier 3 必须标注 `retry_on_uneval: true`**：LLM 评判可能返回 "Not evaluated"，启用自动重试
3. **不要为每条 expectation 自动生成 CONTAINS_PATTERN**：expectation 是 LLM 评的语义判断，CONTAINS_PATTERN 是机器正则检查——两者量的是不同东西
4. **Tier 1 只做文件内容检查**：检查 agent 输出的文件里有什么/没什么，不检查 transcript
