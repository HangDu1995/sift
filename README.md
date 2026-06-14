# Skill-Eval
| 平台 | 命令 |
|------|------|
| macOS / Linux | `bash install.sh` |
| Windows | `install.bat` |


与 [skill-creator](https://github.com/anthropics/skill-creator) 对比，skill-eval 的核心差异：

- **程序化断言** — 确定性验证（文件是否存在、内容是否匹配），零 LLM 成本，不可作弊
- **遵循性检查** — 验证 Claude 是否真的按 SKILL.md 指令执行了，而不只是输出看起来对
- **评分校准** — 交叉验证程序化 vs LLM 判分，告诉你分数本身是否可信
- **CI 就绪** — `--format json` + exit code，合入 CI pipeline

---

## 安装

### 一键安装

```bash
git clone https://github.com/fuxi-ailabs/skill-eval.git
cd skill-eval
bash install.sh     # macOS / Linux
# install.bat       # Windows
```

这会完成三件事：
1. `pip install -e .` — 安装 skill-eval CLI
2. 将 skill-eval-guide skill 安装到 Claude Code (`~/.claude/skills/`) 和 opencode (`~/.config/opencode/skills/`)
3. 验证环境

### 手动安装

```bash
# 安装 CLI
git clone https://github.com/fuxi-ailabs/skill-eval.git
cd skill-eval
pip install -e .

# 安装 skill-eval-guide（可选，让 Claude 自动选择评测命令）
# macOS / Linux:
cp tools/skills/skill-eval-guide/SKILL.md ~/.claude/skills/skill-eval-guide/SKILL.md
cp tools/skills/skill-eval-guide/SKILL.md ~/.config/opencode/skills/skill-eval-guide/SKILL.md

# Windows:
copy tools\skills\skill-eval-guide\SKILL.md %USERPROFILE%\.claude\skills\skill-eval-guide\SKILL.md
copy tools\skills\skill-eval-guide\SKILL.md %USERPROFILE%\.config\opencode\skills\skill-eval-guide\SKILL.md
```

前置条件：Python 3.10+，Claude Code CLI >= 2.1.0（仅动态评测需要）。

---

## 快速开始

---

## 命令

### `vet` — 安全审查

4 扫描器（2 内置 + 2 可选外部），检测密钥泄露、prompt 注入、危险命令。判定 SAFE / REVIEW_NEEDED / BLOCKED。

```bash
skill-eval vet path/to/skill/
skill-eval vet path/to/skill/ --format json --skip-external
```

### `analyze` — 静态分析

11 项检查：描述质量、命名规范、文件行数、示例覆盖、反捏造等。零 API 成本。

```bash
skill-eval analyze path/to/skill/
```

### `init` — 生成评测用例

LLM 自动生成 L1/L2 测试用例，自动推导程序化断言（FILE_EXISTS / CONTAINS_PATTERN 等）。

```bash
skill-eval init path/to/skill/
```

### `add` — 追加评测用例

LLM 辅助或手动添加 eval 到已有的 evals.json。

```bash
# LLM 模式：一句话想法 → 完整 eval
skill-eval add pdf/ --idea "test encrypted PDF decryption" --level L2

# 手动模式
skill-eval add pdf/ --prompt "Decrypt file.pdf..." --expectation "produces output.pdf" --level L2

# 预览不保存
skill-eval add pdf/ --idea "..." --dry-run
```

### `run` — A/B 评测

with_skill vs without_skill 对照执行，程序化断言 + LLM 评分 + 遵循性检查 + 校准，产出 benchmark.json。

```bash
skill-eval run path/to/skill/
skill-eval run path/to/skill/ --runs 3 --model claude-sonnet-4-20250514
skill-eval run path/to/skill/ --scoring programmatic   # 仅程序化，零 LLM 评分成本
skill-eval run path/to/skill/ --no-compliance           # 跳过遵循性检查
```

关键参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--runs` | 1 | 每次评测运行次数（推荐 3） |
| `--scoring` | hybrid | hybrid / programmatic / llm |
| `--compliance-gate` | off | 遵循性率 < 0.5 时直接判定失败 |
| `--no-compliance` | auto | 跳过遵循性检查（--runs < 3 时默认开） |
| `--model` | — | 模型名（也控制盲比较/赛后分析是否执行） |

### `report` — HTML 报告

```bash
skill-eval report output/iteration-1/ --open
```

### `batch` — 批量评测

多 skill 按 DAG 拓扑顺序并行执行。

```bash
skill-eval batch path/to/skills-dir/
skill-eval batch manifest.json --runs 3
```

### `batch-report` — 批量报告

```bash
skill-eval batch-report batch-workspace/ --open
```

---

## 评测体系

### 三层质量保障

| 引擎 | 问题 | 产出 |
|------|------|------|
| **Scoring** | 输出是否正确？ | grading.json |
| **Compliance** | 过程是否遵循 SKILL.md？ | compliance.json |
| **Calibration** | 评分是否可信？ | calibration.json |

三个维度独立运行、交叉验证。输出对但过程未遵循 → 高分不可信；输出错但过程遵循 → 逻辑没问题，改指令即可。

### 断言类型

| 层级 | 判定方式 | 示例 |
|------|---------|------|
| **Tier 1** | 程序化（确定性） | FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS, JSON_HAS_FIELD, TOOL_CALLED |
| **Tier 3** | LLM 语义判分 | LLM_GRADE, LLM_FACT, LLM_NOT_DELEGATE |

### 遵循性引擎

从 SKILL.md 自动提取 6 类指令，程序化 + LLM 双模检查：

| 指令类型 | 检查方式 | 示例 |
|---------|---------|------|
| HEADING_STEP | LLM_JUDGE | "Step 1: Read input data" |
| TOOL_REQUIREMENT | TOOL_CALL_CHECK | "run scripts/validate.py" |
| CONSTRAINT | LLM_JUDGE | "NEVER fabricate data" |
| ANTI_FABRICATION | LLM_JUDGE | 防捏造规则 |
| OUTPUT_FORMAT | PATTERN_MATCH | 输出格式模板 |
| REFERENCE_USAGE | TOOL_CALL_CHECK | "see references/schema.md" |

### 校准引擎

交叉验证程序化断言和 LLM 评分的一致性。可信度分四级：

| 等级 | 含义 |
|------|------|
| HIGH | 两套评分高度一致，结果可信 |
| MEDIUM | 少量分歧，基本可信 |
| LOW | 分歧较多，需人工复核 |
| UNRELIABLE | 严重分歧，exit code 1 |

---

## 评测产物

```
output/iteration-1/
├── benchmark.json          # 聚合结果 (runs, summary, delta, comparisons, analysis)
├── report.html             # 自包含 HTML 报告
├── with_skill/
│   └── eval-<id>/run-<n>/
│       ├── outputs/        # Claude 产生的文件
│       ├── grading.json    # 评分 (expectations + assertions pass/fail)
│       ├── compliance.json # 遵循性检查（仅 with_skill）
│       └── calibration.json
└── without_skill/
    └── eval-<id>/run-<n>/  # 同上（无 compliance）
```

---

## CI 集成

```bash
skill-eval vet path/to/skill/ --format json
skill-eval analyze path/to/skill/ --format json
skill-eval run path/to/skill/ --runs 3 --format json --compliance-gate
```

Exit code: 0 = 通过，1 = 失败，2 = 输入错误，3 = 执行错误。

---

## 测试

```bash
pytest tests/ -v
```
