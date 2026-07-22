#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性 builder:合成多 agent(opencode)session fixture(两份)。

镜像 cc 的 build_multi_skill.py,但用 opencode per-message agent 字段表达多 skill
(而非 cc 的 Skill tool_use)。同一 5-run 交错场景(LEA → CRA → RCZ → LEA → CRA),
agent 切换 = skill 切换。每 run 自带 user 子任务 prompt + assistant 工具工作。

产出(与 cc .jsonl 平级新格式 .json,镜像 opencode session/messages/parts 表结构):
- multi-skill.json —— 违规版(各 agent 产物刻意缺字段/缺段)
- multi-skill-clean.json —— 好版本(产物正确)

与 cc 版差异(诚实):cc 用 parent_tool_use_id 让主 LEA span 跨子 agent 不断;opencode
agent 字段扁平,子 agent = 另一段 run。本 fixture 不做 cc 的「跨子 agent」交错(cc 专属),
只建模常见顶层 agent 切换 → LEA 切出 2 段、CRA 2 段、RCZ 1 段。

确定性(固定 id 序列、无 Date/random),输出可复现可 diff。
重新生成:.venv/bin/python fixtures/example-skills/multi-skill-demo/build_multi_skill_opencode.py
"""
from __future__ import annotations
import json
from pathlib import Path

SESSION = "synth-multi-skill-oc"
MODEL = "glm-5.2"
LEA = "log-error-analyzer"
CRA = "code-review-auditor"
RCZ = "release-checklist-zh"

_seq = 0


def _id(prefix: str) -> str:
    global _seq
    _seq += 1
    return f"{prefix}_{_seq:03d}"


# ---- 共用输入数据(与 cc build_multi_skill.py 同内容)-----------------------
LOG_APP = ("2026-07-14 10:22:01 ERROR [E1002] disk quota exceeded on /var/data\n"
           "2026-07-14 10:24:02 ERROR [E9001] unknown auth failure for user alice\n")
ERROR_CODES = "# Error codes\n- E1002: disk quota exceeded\n"
PAYMENT_PY = "def charge(amount):\n    if amount = 0:\n        return None\n    return process(amount)\n"
CODE_PATTERNS = "# Patterns\n- assignment-in-conditional: `if x = y:` is a bug (use ==)\n"
LOG_AUTH = "2026-07-14 11:00:10 ERROR [E1003] network timeout contacting auth-service\n"
ORDERS_PY = "def fulfill(order):\n    except:\n        pass\n    return True\n"

# ---- 产物:违规版(BAD)-----------------------------------------------------
ERROR_REPORT_BAD = json.dumps({"errors": [
    {"code": "E1002", "message": "disk quota exceeded on /var/data", "line_number": 1},
    {"code": "E9001", "message": "unknown auth failure for user alice", "line_number": 2}]},
    ensure_ascii=False, indent=2)  # VIOLATION: 缺 total_errors + E9001 当已知码
REVIEW_REPORT_BAD = json.dumps({"file": "src/payment.py", "issues": [
    {"line": 2, "severity": "high", "category": "assignment-in-conditional",
     "description": "assignment in conditional; use =="}]},
    ensure_ascii=False, indent=2)  # VIOLATION: 缺 summary
CHANGELOG_BAD = "## [未发布]\n### 新增\n- payment 模块错误处理改进\n"  # VIOLATION: 缺 ### 修复
REVIEW_REPORT_BAD_SUB = json.dumps({"file": "src/orders.py", "issues": [
    {"line": 2, "severity": "medium", "category": "bare-except",
     "description": "bare except swallows all errors"}]},
    ensure_ascii=False, indent=2)

# ---- 产物:好版本(GOOD)---------------------------------------------------
ERROR_REPORT_GOOD = json.dumps({"total_errors": 1, "errors": [
    {"code": "E1002", "message": "disk quota exceeded on /var/data", "line_number": 1}],
    "unclassified": [{"message": "unknown auth failure for user alice", "line_number": 2}]},
    ensure_ascii=False, indent=2)
REVIEW_REPORT_GOOD = json.dumps({"file": "src/payment.py",
    "summary": "1 high-severity issue found: assignment in conditional on line 2.", "issues": [
        {"line": 2, "severity": "high", "category": "assignment-in-conditional",
         "description": "assignment in conditional; use =="}]},
    ensure_ascii=False, indent=2)
CHANGELOG_GOOD = ("## [未发布]\n### 新增\n- payment 模块错误处理改进\n"
                  "### 修复\n- 修复 charge 函数把赋值当条件的 bug\n")
REVIEW_REPORT_GOOD_SUB = json.dumps({"file": "src/orders.py",
    "summary": "1 medium-severity issue found: bare except on line 2.", "issues": [
        {"line": 2, "severity": "medium", "category": "bare-except",
         "description": "bare except swallows all errors"}]},
    ensure_ascii=False, indent=2)


class _Builder:
    def __init__(self):
        self.messages: list[dict] = []
        self.parts: list[dict] = []
        self._t = 0
        self._cur_msg: str | None = None

    def _tick(self):
        self._t += 1
        return self._t

    def user(self, agent: str, text: str):
        mid = _id("msg")
        self.messages.append({"id": mid, "time_created": self._tick(),
                              "data": {"role": "user", "agent": agent}})
        self.parts.append({"id": _id("prt"), "message_id": mid, "time_created": self._tick(),
                           "data": {"type": "text", "text": text}})

    def assistant(self, agent: str, text: str | None = None):
        mid = _id("msg")
        self.messages.append({"id": mid, "time_created": self._tick(),
                              "data": {"role": "assistant", "agent": agent, "model": MODEL}})
        if text:
            self.parts.append({"id": _id("prt"), "message_id": mid, "time_created": self._tick(),
                               "data": {"type": "text", "text": text}})
        self._cur_msg = mid

    def tool(self, tool: str, input_: dict, output: str = ""):
        self.parts.append({"id": _id("prt"), "message_id": self._cur_msg, "time_created": self._tick(),
                           "data": {"type": "tool", "tool": tool,
                                    "state": {"input": input_, "output": output,
                                              "status": "completed"}}})

    def read(self, path: str, content: str):
        self.tool("read", {"filePath": path}, content)

    def write(self, path: str, content: str):
        self.tool("write", {"filePath": path, "content": content}, f"wrote {path}")

    def bash(self, cmd: str, out: str):
        self.tool("bash", {"command": cmd}, out)

    def session_json(self) -> dict:
        return {"session": {"id": SESSION, "agent": LEA, "model": MODEL,
                            "directory": "/tmp/multi-skill-demo", "title": "multi-agent triage"},
                "messages": self.messages, "parts": self.parts}


def build(clean: bool = False) -> dict:
    global _seq
    _seq = 0
    b = _Builder()
    err = ERROR_REPORT_GOOD if clean else ERROR_REPORT_BAD
    rev = REVIEW_REPORT_GOOD if clean else REVIEW_REPORT_BAD
    rev_sub = REVIEW_REPORT_GOOD_SUB if clean else REVIEW_REPORT_BAD_SUB
    chg = CHANGELOG_GOOD if clean else CHANGELOG_BAD

    # Run 1: LEA(app.log)
    b.user(LEA, "分析 logs/app.log 里的错误,写 error-report.json")
    b.assistant(LEA)
    b.read("logs/app.log", LOG_APP)
    b.read("references/error-codes.md", ERROR_CODES)
    b.write("error-report.json", err)
    # Run 2: CRA(payment.py)
    b.user(CRA, "review src/payment.py,写 review-report.json")
    b.assistant(CRA)
    b.read("src/payment.py", PAYMENT_PY)
    b.read("references/code-patterns.md", CODE_PATTERNS)
    b.write("review-report.json", rev)
    # Run 3: RCZ(发布检查)
    b.user(RCZ, "做发布检查")
    b.assistant(RCZ)
    b.bash("pytest -q", "3 passed in 0.42s")
    b.bash("ruff check .", "All checks passed!")
    b.read("references/dependency-policy.md", "# Dependency policy\n- no new high-risk deps\n")
    b.write("CHANGELOG.md", chg)
    b.bash("git tag v1.2.0", "Tagged v1.2.0")
    # Run 4: LEA(auth.log)
    b.user(LEA, "分析 logs/auth.log")
    b.assistant(LEA)
    b.read("logs/auth.log", LOG_AUTH)
    b.write("error-report.json", err)
    # Run 5: CRA(orders.py)
    b.user(CRA, "review src/orders.py,写 review-report.json")
    b.assistant(CRA)
    b.read("src/orders.py", ORDERS_PY)
    b.bash("grep -n except src/orders.py", "2:    except:")
    b.write("review-report.json", rev_sub)
    return b.session_json()


def main() -> None:
    base = Path(__file__).parent
    for clean, name in [(False, "multi-skill.json"), (True, "multi-skill-clean.json")]:
        out = base / name
        out.write_text(json.dumps(build(clean=clean), ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"wrote -> {out}{' (clean/好版本)' if clean else ' (violation/违规版)'}")


if __name__ == "__main__":
    main()
