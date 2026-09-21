"""Auditor refinement behavior tests."""

from __future__ import annotations

import json

from sift.audit.auditor import (
    InstructionEngine,
    TranscriptAuditor,
)
from sift.audit.judge_context import AuditOptions
from sift.audit.tool_match import count_tool_calls, match_tool_call
from sift.models import ToolCallRecord
from sift.models.enums import (
    InstructionSource,
)

from .helpers import (
    _FIXTURES,
    _FakeExtractor,
    _output_inst,
    _pass_fake,
    _read_file_records,
    _step_inst,
    _tool_inst,
    _write_jsonl,
)


class TestMatchFullInputForExistence:
    """存在性匹配拓宽到完整结构化 input(频次不拓宽)——root 对账对的是「派发里有没有交代」。

    input_summary 是 200 字截断的显示串:Task 派发 prompt 里交代的命令(mkdir 等)常被
    截掉,存在性检查假未命中 → 假 FAIL。存在性(match_tool_call)搜全文;频次
    (count_tool_calls)不拓宽——派发里**提到**命令 ≠ 执行了命令,计数拓宽会虚增。
    """

    @staticmethod
    def test_count_still_counts_real_execution() -> None:
        """真执行(Bash command 里)照常:存在性与频次都命中。"""
        tc = ToolCallRecord(name="Bash", input_summary="mkdir build", input={"command": "mkdir build"})
        assert count_tool_calls("mkdir", [tc]) == 1
        assert match_tool_call("mkdir", "", [tc]) is True

    @staticmethod
    def _dispatch_tc() -> ToolCallRecord:
        # 派发调用:summary 只有开头 200 字(无 mkdir),完整 input 的 prompt 深处才有
        return ToolCallRecord(
            name="Task",
            input_summary="派发 developer agent:创建当前组件开发目录,按 STATE.md 的 Task 2 "
            "执行环境准备,读取 proto/desc/golden 并初始化工作区,注意基线与目录结构约定" + ",补充说明文字" * 12,
            input={"prompt": "…(长 prompt)… 请执行 mkdir -p docs/{OP}/develop/ 创建当前组件开发目录 …"},
        )

    def test_existence_hits_command_in_full_input(self) -> None:
        """命令词只在完整 input(超出 200 字摘要)→ 存在性命中(root:派发交代过)。"""
        assert match_tool_call("mkdir", "", [self._dispatch_tc()]) is True

    def test_count_does_not_count_dispatch_mention(self) -> None:
        """同一条派发:频次不数它(提到 ≠ 执行)。"""
        assert count_tool_calls("mkdir", [self._dispatch_tc()]) == 0

    def test_placeholder_param_dropped_and_full_input_params(self) -> None:
        """required_params 的模板占位符 token 不参与字面断言,实参搜完整 input。

        声明写 ``docs/{OP}/develop/``、派发 prompt 里是实值——占位符字面比对永远
        失败。参数(如 ``-p``)在派发 input 的 prompt 深处、200 字 summary 截不到:
        存在性路径参数搜全文(d070717 ② 的补全);频次路不搜。
        """
        tc = self._dispatch_tc()
        # 声明原文:执行 `mkdir -p docs/{OP}/develop/` → params=["-p", "docs/{OP}/develop/"]
        assert match_tool_call("mkdir", "", [tc], required_params=["-p", "docs/{OP}/develop/"]) is True
        # 频次:同 tc 参数不搜全文(summary 无 -p)→ 不计(提到 ≠ 执行)
        assert count_tool_calls("mkdir", [tc], required_params=["-p"]) == 0
        # 真执行(Bash command):两条路径都满足(summary=现实的 200 字序列化形)
        bash = ToolCallRecord(
            name="Bash",
            input_summary='{"command": "mkdir -p docs/x/develop/"}',
            input={"command": "mkdir -p docs/x/develop/"},
        )
        assert match_tool_call("mkdir", "", [bash], required_params=["-p", "docs/x/develop/"]) is True
        assert count_tool_calls("mkdir", [bash], required_params=["-p", "docs/x/develop/"]) == 1
        # 非占位符参数在全文也找不到 → 不命中(参数级断言不放开)
        assert match_tool_call("mkdir", "", [tc], required_params=["--parents"]) is False


class TestRefineInstructions:
    """精筛伪指令:extractor 误抽的示例文字 → LLM 判 is_real=False → 砍。

    含 per-SKILL hash 缓存(``audit.refine_cache``)的端到端覆盖:同 instructions +
    model 跨 audit() 调用复用、并发安全(double-check locking)、缓存 hit 产出的
    findings 与直接 refine 等价。

    缓存目录隔离由 ``conftest._isolate_refine_cache_dir`` 提供(全局 autouse),
    保证每个测试从空缓存开始、绝不污染 ``~/.cache``。
    """

    @staticmethod
    def test_drops_pseudo_keeps_real(monkeypatch) -> None:
        """is_real=False 的砍、True 的留;warnings 报滤除数。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [
                    {"index": 0, "is_real": True},
                    {"index": 1, "is_real": False},
                ]
            ),
        )
        insts = [_step_inst("Step 1: 真步骤"), _step_inst("示例:3 agents 并行")]
        refined, warns = TranscriptAuditor().engine.refine_instructions(
            insts, model=None, timeout=600, on_progress=None
        )
        assert len(refined) == 1
        assert refined[0].instruction_text == "Step 1: 真步骤"
        assert any("refine:" in w and "1 条" in w for w in warns)

    @staticmethod
    def test_refine_prompt_carries_excerpt_and_pseudo_tool_rule(monkeypatch) -> None:
        """refine prompt 喂 source_excerpt + 含「命令行标识符伪工具」类目。

        判「调用 FooFunc 是不是真指令」必须看到原文上下文(它只活在一条命令行
        里)——旧 prompt 只喂指令文本,伪工具要求判不出,漏成假 FAIL。摘录进 prompt
        也进缓存键(v2 bump),摘录变则失效重筛。
        """
        prompts: list[str] = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "is_real": False}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        inst = _tool_inst(text="调用 FooFunc", tool_name="FooFunc")
        inst.source_excerpt = "验收标准:UT 可编译——cd tests/unit && cmake .. && make FooFunc"

        TranscriptAuditor().engine.refine_instructions([inst], model=None, timeout=600, on_progress=None)

        assert len(prompts) == 1
        assert "cmake" in prompts[0]  # 摘录真进了 prompt
        assert "伪工具" in prompts[0]  # 新类目在
        assert "FooFunc" in prompts[0]
        # v3:任务编号词类目(「`./run.sh` 跑 Task 25 用例」抽出「跑 Task」——Task 不是工具)
        assert "任务编号词" in prompts[0]
        assert "跑 Task" in prompts[0]
        # v4:任务清单行是真指令——v3 实测误砍同构任务行(同句式不同分支的行被当示例)
        assert "任务清单行是真指令" in prompts[0]
        assert "不得当示例砍" in prompts[0]

    @staticmethod
    def test_output_format_skipped_not_sent_to_llm(monkeypatch) -> None:
        """OUTPUT_FORMAT(PATTERN_MATCH,纯程序判定)不进精筛候选 → 直接保留、不发给 LLM。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [
                    {"index": 0, "is_real": True},
                    {"index": 1, "is_real": False},
                ]
            ),
        )
        insts = [_output_inst(), _step_inst("真"), _step_inst("伪")]
        refined, _ = TranscriptAuditor().engine.refine_instructions(insts, model=None, timeout=600, on_progress=None)
        # output 保留(没进 LLM)+ 真保留;伪砍 → 共 2 条
        assert len(refined) == 2
        assert any(r.source == InstructionSource.OUTPUT_FORMAT for r in refined)
        assert any(r.instruction_text == "真" for r in refined)
        assert not any(r.instruction_text == "伪" for r in refined)

    @staticmethod
    def test_fallback_keeps_all_on_call_failure(monkeypatch) -> None:
        """call_claude 整批失败(重试耗尽)→ 全保留(精度>召回,宁留伪指令判 N/A 也不砍真)。"""

        def boom(*a, **k):
            raise RuntimeError("timeout")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        insts = [_step_inst("a"), _step_inst("b"), _output_inst()]
        refined, warns = TranscriptAuditor().engine.refine_instructions(
            insts, model=None, timeout=600, on_progress=None
        )
        assert len(refined) == 3  # 全保留
        assert not any("refine:" in w for w in warns)  # 没砍 → 不报

    @staticmethod
    def test_fallback_keeps_all_on_indeterminate(monkeypatch) -> None:
        """全 indeterminate(分不清真假)→ 全保留(不砍)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [
                    {"index": 0, "indeterminate": True, "reason": "模糊"},
                    {"index": 1, "indeterminate": True, "reason": "模糊"},
                ]
            ),
        )
        insts = [_step_inst("a"), _step_inst("b")]
        refined, _ = TranscriptAuditor().engine.refine_instructions(insts, model=None, timeout=600, on_progress=None)
        assert len(refined) == 2  # indeterminate → 保留

    @staticmethod
    def test_refine_runs_once_per_skill_not_per_transcript(tmp_path, monkeypatch) -> None:
        """精筛在 per-transcript 循环外 → 多 transcript 只精筛一次(per-skill 共享)。"""
        refine_calls = {"n": 0}

        def fake(prompt, *a, **k):
            if "真实可检查的指令" in prompt:
                refine_calls["n"] += 1
                return json.dumps([{"index": 0, "is_real": True}])
            return _pass_fake()(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        t2 = _write_jsonl(tmp_path / "t2.jsonl", _read_file_records("a.md"))
        TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1, t2], options=AuditOptions(refine_instructions=True)
        )
        assert refine_calls["n"] == 1  # 2 transcript,精筛仍只 1 次

    @staticmethod
    def test_refine_off_by_default(tmp_path, monkeypatch) -> None:
        """audit() 默认 refine_instructions=False → 不精筛、不报 refine warning。"""
        refine_calls = {"n": 0}

        def fake(prompt, *a, **k):
            if "真实可检查的指令" in prompt:
                refine_calls["n"] += 1
            return _pass_fake()(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report = TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions()
        )  # 默认关
        assert refine_calls["n"] == 0
        assert not any("refine:" in w for w in report.warnings)

    # ---- per-SKILL hash 缓存(audit.refine_cache)--------------------------------

    @staticmethod
    def test_refine_cache_skips_second_audit_call(tmp_path, monkeypatch) -> None:
        """同 SKILL.md 第二次 audit() 命中缓存 → 不再调 ``_refine_pseudo_instructions``。

        跨 audit() 调用复用是缓存的核心价值:runner.py 每 (eval×config×run) 调一次
        audit(),同 SKILL.md 反复 refine 浪费 LLM。用计数 mock 验证两次 audit() 只调一次。
        """
        refine_calls = {"n": 0}
        original = InstructionEngine.refine_instructions

        def counting(self_, insts, **kw):
            refine_calls["n"] += 1
            return original(self_, insts, **kw)

        monkeypatch.setattr(InstructionEngine, "refine_instructions", counting)
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions(refine_instructions=True)
        )
        assert refine_calls["n"] == 1  # 首次:miss → 调
        TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions(refine_instructions=True)
        )
        assert refine_calls["n"] == 1  # 二次:hit → 不调(double-check locking 生效)

    @staticmethod
    def test_refine_dropped_count_populated(tmp_path, monkeypatch) -> None:
        """``AuditReport.refine_dropped_count`` = 砍掉的条数(len(before) - len(after))。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [
                    {"index": 0, "is_real": True},  # 保留
                    {"index": 1, "is_real": False},  # 砍
                    {"index": 2, "is_real": False},  # 砍
                ]
            ),
        )
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor(
                [
                    _step_inst("真"),
                    _step_inst("伪1"),
                    _step_inst("伪2"),
                ]
            ),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report = TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions(refine_instructions=True)
        )
        assert report.refine_dropped_count == 2
        assert any("refine:" in w and "2 条" in w for w in report.warnings)

    @staticmethod
    def test_refine_cache_hit_equivalent_findings(tmp_path, monkeypatch) -> None:
        """缓存 hit 产出的 findings/instructions 等价于直接 refine(无损序列化)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [
                    {"index": 0, "is_real": True},  # 保留(进 candidates 的 index 0)
                    {"index": 1, "is_real": False},  # 砍
                ]
            ),
        )
        insts = [_step_inst("真"), _step_inst("伪")]
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor(insts),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report1 = TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions(refine_instructions=True)
        )
        report2 = TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions(refine_instructions=True)
        )
        # findings 等价:同指令集 → 同样的 verdict(只有"真"那条进判定的桶)
        assert [f.instruction_text for f in report1.findings] == [f.instruction_text for f in report2.findings]
        assert [f.verdict for f in report1.findings] == [f.verdict for f in report2.findings]
        # 伪被砍(两次都砍):不在 findings 里;真保留(两次都留):至少一条含"真"。
        # (findings 里可能还有 derive_declarations 从 transcript 反推的指令,
        # 不影响"伪被砍"这一核心断言。)
        assert all("伪" not in f.instruction_text for f in report1.findings)
        assert all("伪" not in f.instruction_text for f in report2.findings)
        assert any("真" in f.instruction_text for f in report1.findings)
        assert any("真" in f.instruction_text for f in report2.findings)
        # dropped_count 两次都对(缓存 hit 不丢 dropped_count)
        assert report1.refine_dropped_count == report2.refine_dropped_count == 1
        # 第二次跑 refine 缓存命中(_compute 不跑 → 0 LLM 调用)→ stage note="缓存命中"
        stages2 = {s["stage"]: s for s in report2.stage_breakdown}
        assert stages2["refine"]["note"] == "缓存命中"
        assert stages2["refine"]["calls"] == 0

    @staticmethod
    def test_refine_off_does_not_touch_cache(tmp_path, monkeypatch) -> None:
        """``refine_instructions=False`` → 不查缓存、不精筛、``refine_dropped_count=0``。

        让 ``get_or_compute`` 一旦被调就 raise —— 验证默认路径完全不触缓存。
        """
        from sift.audit import refine_cache

        def bomb(*a, **k):
            raise AssertionError("cache should not be touched when refine_instructions=False")

        monkeypatch.setattr(refine_cache, "get_or_compute", bomb)
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report = TranscriptAuditor().audit(
            skill_name="s", skill_body="body", transcripts=[t1], options=AuditOptions()
        )  # 默认 refine_instructions=False
        assert report.refine_dropped_count == 0
        assert not any("refine:" in w for w in report.warnings)


def test_audit_populates_transcript_sources_cc_and_opencode(monkeypatch) -> None:
    """audit 把每条 transcript 来源(cc/opencode)同序填进 transcript_sources。

    与 rebuild 分派同源(is_opencode_session)。transcript_sources 只取决于文件格式,
    在 rebuild 循环里无条件累积(不依赖指令抽取/判定结果)。
    """
    monkeypatch.setattr(
        "sift.audit.auditor.call_claude",
        lambda *a, **k: '[{"index":0,"violated":false,"evidence":"未违规"}]',
    )
    cc = _FIXTURES / "log-error-analyzer-zh" / "transcripts" / "synthetic-violation.jsonl"
    oc = _FIXTURES / "log-error-analyzer-zh" / "opencode-sessions" / "synthetic-violation.json"
    report = TranscriptAuditor().audit(
        skill_name="lea", skill_body="# x\n\nNEVER fabricate data.\n", transcripts=[cc, oc], options=AuditOptions()
    )
    assert report.transcripts == [cc.name, oc.name]
    assert report.transcript_sources == ["cc", "opencode"]
