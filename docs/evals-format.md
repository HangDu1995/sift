# evals.json 考卷格式参考

> evals.json 是 `run`(**路径3 fixture run**)的考卷:每条 eval 是一道题(prompt + 期望 + 断言 + 测试夹具)。`init` 生成它,`run` 跑它,`inspect` 体检它。
>
> **谁不读 evals.json**:`audit`(路径2,读 transcript 对账)、`run --task`(路径1,真任务)。只有路径3 的 `run` 读它。

## 顶层结构

```json
{
  "skill_name": "my-skill",
  "evals": [ {EvalCase}, {EvalCase} ]
}
```

## EvalCase 字段(一道题)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | int | ✅ | 题号(唯一) |
| `prompt` | str | ✅ | 喂给 claude 的用户请求 |
| `name` | str | — | 题名(显示用;inspect 的 problem 靠 `name (id=N)` 定位) |
| `level` | str | — | 难度层级(如 `L1`/`L2`,inspect 按此统计分布;默认 `L1`) |
| `expectations` | list[str] | — | free-text 期望,**LLM 判**(主观,见下) |
| `assertions` | list[Assertion] | — | 结构化断言,**机械 / LLM 判**(客观,见下) |
| `fixtures` | list[Fixture] | — | 内嵌测试数据(run 时写入 claude 工作目录) |
| `expected_output` | str | — | 期望输出(参考) |
| `files` | list[str] | — | 相关文件名 |
| `timeout` | int\|null | — | per-eval 超时秒(覆盖全局;null 走全局) |
| `flags` | list[str] | — | 后处理标记(`degraded:`/`review:`,见下) |
| `mode` / `source_dir` / `source_dir_as` | str | — | 高级:源目录注入模式 |

## expectations vs assertions(最易混)

| | `expectations` | `assertions` |
|---|---|---|
| 形式 | free-text 字符串 | `{name, type, params, tier}` 对象 |
| 谁判 | **永远 LLM**(主观) | 由 `type` 决定:程序化 type=机械,LLM type=语义 |
| 适合 | "应检查 X""不得编造""语气专业" | "文件存在""JSON 含字段""调了 Read 工具""步数 2-10" |

> `inspect` 的「稳定性」只统计 **assertions** 的程序化占比;`expectations` 全算 LLM 判,在另一行单独报。所以一份考卷 = 客观题(assertions 里的程序化 type)+ 主观题(expectations + assertions 里的 LLM type)。

## Assertion 字段

```json
{"name": "报告存在", "type": "FILE_EXISTS", "params": {"filename": "report.json"}}
```

| 字段 | 说明 |
|---|---|
| `name` | 断言名(显示 + 写进 evidence) |
| `type` | `AssertionType` 枚举(见下表) |
| `params` | 该 type 的参数(见下表) |
| `tier` | `TIER1`(程序化,默认)/ `TIER3`(LLM 语义判) |
| `retry_on_uneval` | LLM 判返回 UNEVAL 时是否重试(默认 true) |
| `retry_max` | 重试上限(默认 2) |

> ⚠️ **tier 坑**:用 LLM 类型断言(`LLM_GRADE`/`LLM_FACT`/`LLM_NOT_DELEGATE`)时**必须显式 `"tier": "TIER3"`**——默认是 TIER1,不标会被当程序化 type 执行,因无对应 handler 而报 `Unknown assertion type`。`init` 生成时会自动带(LLM 类型配 TIER3),手写时别漏。

## AssertionType 全表

### 程序化(TIER1,机械判,稳)

| type | params | 判定 |
|---|---|---|
| `FILE_EXISTS` | `filename` | 文件存在 |
| `FILE_CONTAINS` | `filename`, `pattern`(正则) | 指定文件含正则 |
| `CONTAINS_PATTERN` | `pattern`, `path`(可选) | 含正则;不给 `path` 则递归全目录 + transcript 兜底 |
| `JSON_HAS_FIELD` | `filename`, `field`(支持 `a.b.c` 嵌套) | JSON 文件含字段 |
| `NOT_CONTAINS` | `pattern`, `path`(可选) | 不含正则(递归全目录) |
| `LINE_COUNT` | `min` / `max`, `path`(可选) | 行数在 [min,max](不给 path 则全目录合计) |
| `TOOL_CALLED` | `tool_name`, `min_count`(默认 1) | 某工具调用次数 ≥ min_count |
| `STEP_COUNT` | `min` / `max` | agentic 步数(turns)在 [min,max] |
| `CUSTOM_SCRIPT` | `script`, `args`(可选) | 在工作目录跑脚本,exit 0 过(60s 超时) |
| `CODE_COMPILES` | `include`(可选 glob) | 括号 `{}``()``[]` 配对的基本语法检查 |
| `CROSS_FILE_CONSISTENCY` | `include`(可选) | 相对 import(`./x`)都能解析到文件 |
| `CUSTOM_PLUGIN` | `plugin_name` | 调注册的自定义插件(需 plugin registry) |

示例:

```json
{"name": "报告存在", "type": "FILE_EXISTS", "params": {"filename": "error-report.json"}}
{"name": "报告含 total_errors", "type": "JSON_HAS_FIELD", "params": {"filename": "error-report.json", "field": "total_errors"}}
{"name": "读了日志", "type": "TOOL_CALLED", "params": {"tool_name": "Read", "min_count": 1}}
{"name": "无 SQL 注入", "type": "NOT_CONTAINS", "params": {"pattern": "DROP TABLE", "path": "output.json"}}
```

### LLM(TIER3,语义判,漂)

| type | 判定 |
|---|---|
| `LLM_GRADE` | LLM 按断言描述打分(整体质量) |
| `LLM_FACT` | 事实正确性核对 |
| `LLM_NOT_DELEGATE` | 未发生不当委托 |

这 3 个由 LLM grader 处理(`programmatic` 跳过 TIER3),`params` 放判定指引文本给 LLM。**记得配 `tier: TIER3`。**

## fixtures(内嵌测试数据)

```json
"fixtures": [{"path": "logs/app.log", "content": "2024-01-01 E-AUTH-001 login failed\n..."}]
```

`run` 时把 `content` 写入 claude 工作目录的 `path`,claude 读它干活——**不用外部提供真实文件**。这就是为什么没日志也能 `run`:日志是考卷内嵌的夹具。

## flags(后处理标记)

- `degraded:no-llm`:`init` 降级 fallback(LLM 失败,未挖到结构化信号,assertions 可能为空);`inspect` 标 ⚠。
- `review:...`:需人工复核。

## 完整示例(log-error-analyzer,逐字段注释)

```jsonc
{
  "skill_name": "log-error-analyzer",
  "evals": [
    {
      "id": 1,                                    // 题号
      "name": "L1-错误分类规则理解",               // 题名(inspect 用它定位)
      "prompt": "Based on the skill, what steps must you follow to classify errors?",  // 喂给 claude
      "level": "L1",                              // 层级
      "expectations": [                           // 主观期望,LLM 判(这道题纯主观)
        "应使用 Read 工具读取输入日志",
        "应读 references/error-codes.md 查错误码表来分类",
        "不得编造 error-codes.md 里不存在的错误码",
        "输出 error-report.json 含 total_errors、errors、unclassified 三个字段"
      ],
      "assertions": []                            // 零程序化断言 → inspect 标「纯主观」
    },
    {
      "id": 2,
      "name": "L2-分析日志写报告",
      "prompt": "Analyze the errors in logs/app.log and write error-report.json.",
      "level": "L2",
      "fixtures": [                               // 内嵌测试数据:造一份日志给 claude
        {"path": "logs/app.log", "content": "2024-01-01 E-AUTH-001 login failed\n2024-01-01 E-DB-002 db lost\n2024-01-01 some weird line"}
      ],
      "expectations": [                           // 主观
        "E-AUTH-001 和 E-DB-002 被正确分类",
        "无码的第三行归入 unclassified 而非编造码"
      ],
      "assertions": [                             // 客观(机械判)
        {"name": "报告文件存在",  "type": "FILE_EXISTS",    "params": {"filename": "error-report.json"}},
        {"name": "报告含 total_errors", "type": "JSON_HAS_FIELD", "params": {"filename": "error-report.json", "field": "total_errors"}},
        {"name": "报告含 errors",  "type": "JSON_HAS_FIELD", "params": {"filename": "error-report.json", "field": "errors"}}
      ]
    }
  ]
}
```

`inspect` 这份考卷的输出:`程序化 3 (100%) / LLM 0 · expectations 7(全 LLM 判)`,problem 标 `L1-错误分类规则理解 (id=1)` 零程序化(纯主观)——L1 是概念题只能 LLM 判,L2 才有硬断言兜底。

---

> 命令用法见 [skill-eval-reference.md](skill-eval-reference.md);示例 skill 见 [fixtures/example-skills/](../fixtures/example-skills/)。
