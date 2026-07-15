#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性 builder:合成一个多 skill transcript fixture。

产出 ``multi-skill.jsonl`` —— 单个 claude-code 会话 transcript,里面交错调用了三个
不同的 example skill(其中 log-error-analyzer 调两次 + 一个子 agent 调 code-review-auditor),
用于验证 ``audit --by-skill`` 的 scope-aware 切片纯度。

设计要点:
- 完全确定性(无 Date/random):每个 id/uuid 都是固定字面量 → 输出可复现、可 diff。
- record 结构照搬真实 claude 2.1.139 transcript(见 /tmp/nest、/tmp/nest2),但内容是
  合成的、刻意精简的(不拷贝真实 skill 正文)。
- 每个 skill 用 DISTINCT 的文件路径,污染一旦发生显而易见:
    * log-error-analyzer → logs/*.log / references/error-codes.md / error-report.json
    * code-review-auditor → src/*.py / references/code-patterns.md / review-report.json
    * release-checklist-zh → pytest/ruff/git tag / CHANGELOG.md / version.py
- **切片纯度是本 fixture 验证的核心属性**(scope-aware 切分不串味)——不是用来确定性抓违规。
  各 skill 产物刻意不完美(如 error-report.json 缺字段、CHANGELOG 缺段),但 audit 能否
  抓到取决于 auditor 判定层,不取决于切片:lea/cra 的 Output 要求是散文(extractor 只对
  代码块抽 OUTPUT_FORMAT 指令→这俩零条)、rcz 的 OUTPUT 检查对 assistant 措辞不对写入
  文件,故这些"违规"多为 LLM 辅助判定、非确定性。slice 纯度才是 --by-skill 本身保证的东西。

重新生成:
    .venv/bin/python fixtures/example-transcripts/build_multi_skill.py
"""
from __future__ import annotations

import json
from pathlib import Path

SESSION = "synth-multi-skill"
MODEL = "glm-5.2"

# skill 名必须与 fixtures/example-skills/<dir>/SKILL.md frontmatter 的 name: 一致
# (audit 解析 skill_path → name,slicer 用它匹配 Skill tool_use 的 input.skill)。
LEA = "log-error-analyzer"
CRA = "code-review-auditor"
RCZ = "release-checklist-zh"

# 全局递增序号,保证所有 id/uuid 唯一(不同前缀 → 跨前缀也不撞)。
_seq = 0


def _id(prefix: str) -> str:
    global _seq
    _seq += 1
    return f"{prefix}_{_seq:03d}"


# ---- record 构造辅助(对齐真实 transcript 形状)----------------------------


def assistant(content: list, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": _id("msg"),
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "context_management": None,
        },
        "parent_tool_use_id": parent,
        "session_id": SESSION,
        "uuid": _id("uuid"),
    }


def user(content, parent: str | None = None) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": parent,
        "session_id": SESSION,
        "uuid": _id("uuid"),
    }


def tool_use(name: str, inp: dict, parent: str | None = None) -> tuple[dict, str]:
    """发 tool_use 记录,返回 (record, tool_use_id)。"""
    tid = _id("tu")
    rec = assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], parent=parent)
    return rec, tid


def tool_result(tid: str, content: str, parent: str | None = None) -> dict:
    return user(
        [{"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": False}],
        parent=parent,
    )


def text_block(t: str, parent: str | None = None) -> dict:
    return assistant([{"type": "text", "text": t}], parent=parent)


# ---- 业务级行为辅助 -------------------------------------------------------


def invoke_skill(name: str, parent: str | None = None) -> None:
    """Skill tool_use + 'Launching skill' result(真实 transcript 形状)。"""
    rec, tid = tool_use("Skill", {"skill": name}, parent=parent)
    R.append(rec)
    R.append(tool_result(tid, f"Launching skill: {name}", parent=parent))


def read_file(path: str, content: str, parent: str | None = None) -> None:
    rec, tid = tool_use("Read", {"file_path": path}, parent=parent)
    R.append(rec)
    R.append(tool_result(tid, content, parent=parent))


def write_file(path: str, content: str, parent: str | None = None) -> None:
    rec, tid = tool_use("Write", {"file_path": path, "content": content}, parent=parent)
    R.append(rec)
    R.append(tool_result(tid, f"Wrote {path}.", parent=parent))


def bash(cmd: str, out: str, parent: str | None = None, desc: str = "run command") -> None:
    rec, tid = tool_use("Bash", {"command": cmd, "description": desc}, parent=parent)
    R.append(rec)
    R.append(tool_result(tid, out, parent=parent))


# ---- 内容(合成数据)------------------------------------------------------

LOG_APP = """\
2026-07-14 10:22:01 ERROR [E1002] disk quota exceeded on /var/data
2026-07-14 10:23:15 WARN retrying connection to db-1
2026-07-14 10:24:02 ERROR [E9001] unknown auth failure for user alice
"""

ERROR_CODES = """\
# Error codes
- E1002: disk quota exceeded
- E1003: network timeout
"""

# VIOLATION: 缺 total_errors(必填整数计数) + 缺 unclassified(E9001 不在码表应归此)
ERROR_REPORT_BAD = json.dumps({
    "errors": [
        {"code": "E1002", "message": "disk quota exceeded on /var/data", "line_number": 1},
        {"code": "E9001", "message": "unknown auth failure for user alice", "line_number": 3},
    ],
}, ensure_ascii=False, indent=2)

PAYMENT_PY = """\
def charge(amount):
    if amount = 0:
        return None
    return process(amount)
"""

CODE_PATTERNS = """\
# Patterns
- assignment-in-conditional: `if x = y:` is a bug (use ==)
- bare-except: `except:` swallows errors
"""

# VIOLATION: 缺 summary(必填 string)
REVIEW_REPORT_BAD = json.dumps({
    "file": "src/payment.py",
    "issues": [
        {"line": 2, "severity": "high", "category": "assignment-in-conditional",
         "description": "assignment in conditional; use == instead of ="},
    ],
}, ensure_ascii=False, indent=2)

# VIOLATION: 缺 "### 修复"(skill changelog 格式要求 新增 + 修复 两段)
CHANGELOG_BAD = """\
## [未发布]
### 新增
- payment 模块错误处理改进
"""

LOG_AUTH = """\
2026-07-14 11:00:10 ERROR [E1003] network timeout contacting auth-service
"""

LOG_SECURITY = """\
2026-07-14 11:30:00 ERROR [E1002] disk quota exceeded on /tmp
"""

ORDERS_PY = """\
def fulfill(order):
    except:
        pass
    return True
"""

# 子 agent 的 review-report.json(同样缺 summary —— 与主作用域 cra 一致的不完美产物)
REVIEW_REPORT_BAD_SUB = json.dumps({
    "file": "src/orders.py",
    "issues": [
        {"line": 2, "severity": "medium", "category": "bare-except",
         "description": "bare except swallows all errors"},
    ],
}, ensure_ascii=False, indent=2)


# ---- 拼 transcript --------------------------------------------------------

R: list[dict] = []

# 系统初始化(精简,仅示结构)。
R.append({
    "type": "system", "subtype": "init", "cwd": "/tmp/multi-skill-demo",
    "session_id": SESSION,
    "tools": ["Skill", "Read", "Write", "Edit", "Bash", "Agent"],
    "model": MODEL + "[1m]", "uuid": "init-000",
})

# === Step 1: Skill(log-error-analyzer) 第一次调用 ==========================
invoke_skill(LEA)
read_file("logs/app.log", LOG_APP)
read_file("references/error-codes.md", ERROR_CODES)
write_file("error-report.json", ERROR_REPORT_BAD)          # 植入违规(缺 total_errors)

# === Step 2: Skill(code-review-auditor) ====================================
invoke_skill(CRA)
read_file("src/payment.py", PAYMENT_PY)
read_file("references/code-patterns.md", CODE_PATTERNS)
write_file("review-report.json", REVIEW_REPORT_BAD)        # 植入违规(缺 summary)

# === Step 3: Skill(release-checklist-zh) ===================================
invoke_skill(RCZ)
bash("pytest -q", "3 passed in 0.42s", desc="run tests")
bash("ruff check .", "All checks passed!", desc="run linter")
read_file("references/dependency-policy.md", "# Dependency policy\n- no new high-risk deps\n")
write_file("CHANGELOG.md", CHANGELOG_BAD)                  # 植入违规(缺 "### 修复")
bash("git tag v1.2.0", "Tagged v1.2.0", desc="tag release")

# === Step 4: Skill(log-error-analyzer) 第二次调用(测多调用)===============
invoke_skill(LEA)
read_file("logs/auth.log", LOG_AUTH)
write_file("error-report.json", ERROR_REPORT_BAD)          # 同样违规(缺 total_errors)

# === Step 5: 主 agent 起 Agent → 子 agent 调 Skill(code-review-auditor)=====
agent_rec, agent_tid = tool_use(
    "Agent",
    {
        "description": "Review orders module",
        "subagent_type": "general-purpose",
        "prompt": (
            "Use the code-review-auditor skill to review src/orders.py for bugs, "
            "then write review-report.json."
        ),
    },
    parent=None,                                            # 主作用域发起
)
R.append(agent_rec)

# 子 agent 内部记录(parent_tool_use_id = agent_tid → 归子作用域)
invoke_skill(CRA, parent=agent_tid)
read_file("src/orders.py", ORDERS_PY, parent=agent_tid)
bash("grep -n except src/orders.py", "2:    except:", parent=agent_tid, desc="find except blocks")
write_file("review-report.json", REVIEW_REPORT_BAD_SUB, parent=agent_tid)

# Agent tool_result:子 agent 返回(主作用域记录,tool_use_id == agent_tid)。
# 这条主作用域 record 触发 slicer 的 depth-1 闭包,结束子 agent 作用域。
R.append(user(
    [{"type": "tool_result", "tool_use_id": agent_tid,
      "content": [{"type": "text", "text": "Reviewed the module and wrote the review report."}]}],
    parent=None,
))

# === Step 6: 主 agent 继续 log-error-analyzer 工作(子 agent 返回后)=========
# 关键污染测试:此时主作用域 active 仍是 log-error-analyzer(子 agent 调的 cra 不应改写它),
# 所以这里的 logs/ 工作归 lea span2,不归 cra。
read_file("logs/security.log", LOG_SECURITY)
write_file("error-report.json", ERROR_REPORT_BAD)
R.append(text_block("Triage complete: 3 error lines classified across logs/app.log, logs/auth.log, logs/security.log."))

# 最终结果行(精简)。
R.append({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "Multi-skill triage complete.", "session_id": SESSION,
    "uuid": "result-000",
})


def build() -> list[dict]:
    return list(R)


def main() -> None:
    out = Path(__file__).parent / "multi-skill.jsonl"
    records = build()
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records -> {out}")


if __name__ == "__main__":
    main()
