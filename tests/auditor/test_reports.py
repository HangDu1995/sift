"""Auditor reports behavior tests."""

from __future__ import annotations

import json

import pytest
from sift.audit.auditor import (
    _MAX_LLM_IN_BATCH,
    TranscriptAuditor,
)
from sift.audit.judge_context import AuditOptions, JudgeCallOptions, JudgeTranscript
from sift.evals.transcript import SessionDigest
from sift.models import ProvenanceAuditResult
from sift.models.audit_models import (
    AuditCategory,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)

from .helpers import (
    _pass_fake,
    _prohibition_inst,
    _read_file_records,
    _step_inst,
    _tool_inst,
    _write_jsonl,
)


class TestAuditAggregate:
    """audit 主入口:多 transcript 聚合、summary 计数、derived 标记、坏 transcript 降级。"""

    @staticmethod
    def test_two_transcripts_aggregate_with_summary_and_derived(tmp_path, monkeypatch) -> None:
        """2 transcript 各 Read foo.md:每条产 2 finding(1 N/A + 1 PASS-derived),平铺 4 条。

        - skill_body "use Bash" → 1 条 TOOL_REQUIREMENT(未命中 trace → LLM N/A);
        - foo.md 不在 skill_body → derive_declarations 反推 "读取 foo.md"(REFERENCE_USAGE),
          且 transcript 真读了 → 命中 trace → PASS,标 derived_from_transcript=True。
        每 transcript 2 finding → 共 4;summary total/pass/fail/na 精确。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        foo = tmp_path / "foo.md"
        tp1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        tp2 = _write_jsonl(tmp_path / "t2.jsonl", _read_file_records(str(foo)))

        transcript_auditor = TranscriptAuditor()
        report = transcript_auditor.audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[tp1, tp2], options=AuditOptions()
        )

        assert isinstance(report, AuditReport)
        assert report.skill_name == "myskill"
        # transcripts 字段含 2 个文件名(顺序与入参一致)
        assert report.transcripts == ["t1.jsonl", "t2.jsonl"]
        assert report.warnings == []

        # 平铺 per-transcript:每 transcript 2 finding → 共 4
        assert len(report.findings) == 4
        # 每 transcript 的 finding 各自带正确的 transcript 名(2 + 2)
        assert sorted(f.transcript for f in report.findings) == ["t1.jsonl", "t1.jsonl", "t2.jsonl", "t2.jsonl"]

        # summary 精确:每 transcript (1 N/A [Bash 未调] + 1 PASS [读取 foo.md 命中])
        assert report.summary == {"total": 4, "pass": 2, "fail": 0, "na": 2, "unresolved": 0, "indeterminate": 0}

        # derived 标记:2 个 PASS finding(读取 foo.md)标 derived_from_transcript=True,
        # 2 个 N/A finding(use Bash,来自 skill_body 静态指令)保持 False
        derived = [f for f in report.findings if f.derived_from_transcript]
        assert len(derived) == 2
        assert all(f.verdict == AuditVerdict.PASS for f in derived)
        assert all(f.category == AuditCategory.CONDITIONAL for f in derived)
        assert all("foo.md" in f.instruction_text for f in derived)
        non_derived = [f for f in report.findings if not f.derived_from_transcript]
        assert len(non_derived) == 2
        assert all(f.verdict == AuditVerdict.NA for f in non_derived)

        # 声明分组标题透传:body "use Bash" 无标题 → 标题级 extractor 的 section="Root"
        bash_rows = [r for r in report.by_instruction if "Bash" in r.instruction_text]
        assert len(bash_rows) == 1
        assert bash_rows[0].section == "Root"
        assert all(f.section == "Root" for f in non_derived)

        # 声明覆盖视图:唯一声明指令(use Bash)两段都 N/A → uncovered;derived
        # (读取 foo.md)不计分母 → total=1。分组 "Root"。
        cov = report.coverage
        assert cov.total == 1
        assert (cov.covered, cov.uncovered, cov.unknown) == (0, 1, 0)
        assert cov.coverage_rate == 0.0
        assert [s.section for s in cov.sections] == ["Root"]
        assert cov.sections[0].instructions[0].instruction_text.startswith("use")

    @staticmethod
    def test_audit_populates_token_usage_from_call_claude(tmp_path, monkeypatch) -> None:
        """on_usage 穿透整条判官漏斗到 call_claude:mock 取出 on_usage 回灌 usage →
        report.token_usage 累计(calls>=1、input>0、cost>0)。

        证明 on_usage 从 audit() 经 _judge_* → _llm_judge_* → _run_batches_concurrent →
        judge_batch_with_retry → call_claude 一路传到位(任一环漏传就 calls==0)。
        """
        inner = _pass_fake()

        def fake(prompt, *a, **k):
            ou = k.get("on_usage")
            if ou:  # call_claude json 模式抓到 usage 才回调
                ou({"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.02})
            return inner(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)

        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[tp], options=AuditOptions()
        )

        tu = report.token_usage
        assert tu.get("calls", 0) >= 1
        assert tu.get("input_tokens", 0) > 0
        assert tu.get("total_cost_usd", 0) > 0

    @staticmethod
    def test_audit_token_usage_empty_when_no_llm(tmp_path, monkeypatch) -> None:
        """空 transcripts → 不跑任何判官 → token_usage 空(无 LLM 调用,report 干净)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        report = TranscriptAuditor().audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[], options=AuditOptions()
        )
        assert report.token_usage == {}

    @staticmethod
    def test_audit_populates_duration_seconds(tmp_path, monkeypatch) -> None:
        """audit() 头尾 perf_counter → duration_seconds > 0(audit 跑过即有正墙钟,不依赖判官是否调用)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[tp], options=AuditOptions()
        )
        assert report.duration_seconds > 0  # 实现前 default 0.0 → fail;实现后 audit 墙钟 >0

    @staticmethod
    def test_audit_duration_seconds_nonneg_when_no_llm(tmp_path, monkeypatch) -> None:
        """空 transcripts → audit 仍跑过,duration_seconds 非负(墙钟存在,即便无判官)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        report = TranscriptAuditor().audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[], options=AuditOptions()
        )
        assert report.duration_seconds >= 0.0

    @staticmethod
    def test_audit_populates_stage_breakdown(tmp_path, monkeypatch) -> None:
        """audit() 分阶段计时 + token:stage_breakdown 含全部 6 阶段;判官走 scoped("judge")
        → judge 阶段有 token/calls(含 cache_read 归属),本地阶段(extract/rebuild/postprocess)0 token;
        refine/headline 默认关 → 0 token。各阶段是顺序非重叠墙钟片段 → 秒数之和 ≤ duration_seconds。
        """
        inner = _pass_fake()

        def fake(prompt, *a, **k):
            ou = k.get("on_usage")
            if ou:
                ou({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 200, "total_cost_usd": 0.02})
            return inner(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[tp], options=AuditOptions()
        )

        stages = {s["stage"]: s for s in report.stage_breakdown}
        assert set(stages) == {"extract", "refine", "headline", "rebuild", "judge", "postprocess"}
        assert all(s["seconds"] >= 0 for s in stages.values())
        # judge 跑了 LLM → 有 token + calls(含 cache_read 归属到 judge 桶);refine/headline 默认关 + 本地阶段 → 0 token
        assert stages["judge"]["input_tokens"] > 0
        assert stages["judge"]["cache_read_input_tokens"] > 0
        assert stages["judge"]["calls"] > 0
        for local_stage in ("extract", "refine", "headline", "rebuild", "postprocess"):
            assert stages[local_stage]["calls"] == 0
            assert stages[local_stage]["input_tokens"] == 0
            assert stages[local_stage]["cache_read_input_tokens"] == 0
        # note:开关默认关 → refine/headline "未启用";本地阶段 + judge 的 note 空(calls=0 是常态)
        assert stages["refine"]["note"] == "未启用"
        assert stages["headline"]["note"] == "未启用"
        for noteless in ("extract", "rebuild", "judge", "postprocess"):
            assert stages[noteless]["note"] == ""
        # 顺序非重叠片段 → 阶段和 ≤ 总墙钟(允许 1s 浮点/timing 缝隙)
        stage_sum = sum(s["seconds"] for s in stages.values())
        assert stage_sum <= report.duration_seconds + 1.0

    @staticmethod
    def test_audit_token_usage_survives_model_dump_roundtrip(tmp_path, monkeypatch) -> None:
        """token_usage 经 model_dump_json / model_validate_json 往返不丢。"""
        inner = _pass_fake()

        def fake(prompt, *a, **k):
            ou = k.get("on_usage")
            if ou:
                ou({"input_tokens": 7, "output_tokens": 3, "total_cost_usd": 0.001})
            return inner(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit(
            skill_name="myskill", skill_body="use Bash", transcripts=[tp], options=AuditOptions()
        )

        restored = AuditReport.model_validate_json(report.model_dump_json())
        assert restored.token_usage == report.token_usage
        assert restored.token_usage.get("calls", 0) >= 1

    @staticmethod
    def test_bad_transcript_goes_to_warning_others_processed(tmp_path, monkeypatch) -> None:
        """坏 transcript(不存在的路径)→ rebuild 抛 SiftError → 进 warnings、不崩;
        好 transcript 照常处理(findings 正常产出)。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        foo = tmp_path / "foo.md"
        good = _write_jsonl(tmp_path / "good.jsonl", _read_file_records(str(foo)))
        bad = tmp_path / "missing.jsonl"  # 不存在

        transcript_auditor = TranscriptAuditor()
        report = transcript_auditor.audit(
            skill_name="s", skill_body="use Bash", transcripts=[good, bad], options=AuditOptions()
        )

        # 好的正常处理(进 transcripts + findings)
        assert report.transcripts == ["good.jsonl"]
        assert len(report.findings) == 2  # good 的 2 finding
        # 坏的不进 transcripts / findings
        assert all("missing" not in f.transcript for f in report.findings)
        # 坏的进 warnings(消息含文件名)
        assert len(report.warnings) == 1
        assert "missing.jsonl" in report.warnings[0]

    @staticmethod
    def test_empty_transcripts_returns_empty_report(monkeypatch) -> None:
        """空 transcripts 列表 → findings 空、summary 全 0、transcripts 空,不崩。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        transcript_auditor = TranscriptAuditor()
        report = transcript_auditor.audit(skill_name="s", skill_body="use Bash", transcripts=[], options=AuditOptions())

        assert isinstance(report, AuditReport)
        assert report.findings == []
        assert report.transcripts == []
        assert report.warnings == []
        assert report.summary == {"total": 0, "pass": 0, "fail": 0, "na": 0, "unresolved": 0, "indeterminate": 0}

    @staticmethod
    def test_routes_each_category(tmp_path, monkeypatch) -> None:
        """skill_body 含四类指令 + REFERENCES → 各自路由到正确 category 判定(覆盖四桶)。

        TOOL_REQUIREMENT/REFERENCE_USAGE→CONDITIONAL、CONSTRAINT→PROHIBITION、
        HEADING_STEP→STEP、OUTPUT_FORMAT→OUTPUT。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        body = (
            "use Bash\n\n"
            "NEVER delete production data\n\n"
            "1. Analyze input format\n\n"
            "## 输出格式\n\n"
            "```\nEXPECTED_TOKEN\n```\n\n"
            "read references/design.md\n"
        )
        # 最小 transcript:仅 user turn(rebuild 非空即可;无工具 → 条件性全未命中走 LLM)
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )

        transcript_auditor = TranscriptAuditor()
        report = transcript_auditor.audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

        # 四个类别都被路由到(各自走了对应的 _judge_*)
        cats = {f.category for f in report.findings}
        assert cats == {
            AuditCategory.CONDITIONAL,
            AuditCategory.PROHIBITION,
            AuditCategory.STEP,
            AuditCategory.OUTPUT,
        }
        assert len(report.findings) == 5  # 2 CONDITIONAL + 1 + 1 + 1


def test_audit_attaches_available_provenance_without_changing_findings(tmp_path, monkeypatch) -> None:
    """v2 digest 自动写入独立 provenance 区块，既有 findings 汇总保持不变。"""
    transcript = tmp_path / "v2.jsonl"
    transcript.write_text("placeholder", encoding="utf-8")
    digest = SessionDigest(evidence_metadata={"version": 2})
    calls: list[SessionDigest] = []

    def fake_analyze(value: SessionDigest) -> ProvenanceAuditResult:
        calls.append(value)
        return ProvenanceAuditResult(
            available=True,
            statistics={"capture_count": 0},
        )

    monkeypatch.setattr("sift.audit.auditor.rebuild", lambda *args, **kwargs: ([], "", digest))
    monkeypatch.setattr("sift.audit.auditor.analyze_provenance", fake_analyze)

    report = TranscriptAuditor().audit(
        skill_name="skill", skill_body="", transcripts=[transcript], options=AuditOptions()
    )

    assert calls == [digest]
    assert list(report.provenance) == ["v2.jsonl"]
    assert report.provenance["v2.jsonl"].available is True
    assert report.summary["total"] == 0
    assert report.findings == []


def test_audit_display_names_used_for_all_report_surfaces(tmp_path, monkeypatch) -> None:
    """display_names:报告里的一切名字(provenance 键 / transcripts)用原始输入名,
    不透出 CLI 展开结构化输入 / --db 导出产生的随机临时文件名(跑一次变一次、
    跑完即删,下游无法对回输入);不传时保持 tp.name(行为与旧版一致)。
    """
    tmp_jsonl = tmp_path / "tmpXYZ.jsonl"  # 模拟 CLI 展开出的临时文件
    tmp_jsonl.write_text("placeholder", encoding="utf-8")
    digest = SessionDigest(evidence_metadata={"version": 2})

    monkeypatch.setattr("sift.audit.auditor.rebuild", lambda *args, **kwargs: ([], "", digest))
    monkeypatch.setattr(
        "sift.audit.auditor.analyze_provenance",
        lambda value: ProvenanceAuditResult(available=True),
    )

    # 传 display_names → 用原始输入名
    report = TranscriptAuditor().audit(
        skill_name="skill",
        skill_body="",
        transcripts=[tmp_jsonl],
        options=AuditOptions(display_names=["opsbot-evidence.json"]),
    )
    assert list(report.provenance) == ["opsbot-evidence.json"]
    assert report.transcripts == ["opsbot-evidence.json"]

    # 不传(默认)→ tp.name,与旧版一致
    report_default = TranscriptAuditor().audit(
        skill_name="skill", skill_body="", transcripts=[tmp_jsonl], options=AuditOptions()
    )
    assert list(report_default.provenance) == ["tmpXYZ.jsonl"]
    assert report_default.transcripts == ["tmpXYZ.jsonl"]


@pytest.mark.parametrize("has_refs", [False, True])
def test_audit_prechecks_provenance_without_losing_reference_only_evidence(tmp_path, monkeypatch, has_refs):
    transcript = tmp_path / "session.jsonl"
    digest = SessionDigest()
    if has_refs:
        from sift.models import FileEffectRecord

        digest.evidence_refs = {
            "capture:test": FileEffectRecord(
                path="a.py",
                evidence_ref="capture:test",
                quality="observed",
                capture_id="test",
            )
        }
    calls = []

    def analyze(value):
        calls.append(value)
        return ProvenanceAuditResult(available=True)

    monkeypatch.setattr("sift.audit.auditor.rebuild", lambda *args, **kwargs: ([], "", digest))
    monkeypatch.setattr("sift.audit.auditor.analyze_provenance", analyze)
    report = TranscriptAuditor().audit(
        skill_name="skill", skill_body="", transcripts=[transcript], options=AuditOptions()
    )
    assert len(calls) == int(has_refs)
    assert bool(report.provenance) == has_refs


def test_failed_rebuild_skips_provenance(tmp_path, monkeypatch):
    from sift.errors import SiftError

    def rebuild(*args, **kwargs):
        raise SiftError("invalid evidence")

    def analyze(*args):
        pytest.fail("failed rebuild must not reach analyzer")

    monkeypatch.setattr("sift.audit.auditor.rebuild", rebuild)
    monkeypatch.setattr("sift.audit.auditor.analyze_provenance", analyze)
    report = TranscriptAuditor().audit(
        skill_name="skill", skill_body="", transcripts=[tmp_path / "bad.jsonl"], options=AuditOptions()
    )
    assert report.provenance == {}
    assert report.findings == []
    assert any("invalid evidence" in warning for warning in report.warnings)


class TestAuditNoiseWiring:
    """Task 4 接线:audit() 在 post_process_findings 后跑 annotate_noise、
    在 aggregate_by_instruction 后跑 annotate_aggregate_noise,AuditReport 携带标注后的
    findings / by_instruction。

    全程 mock call_claude(不真跑 LLM)。构造 1 FAIL(prohibition violated=true)+
    1 PASS(step verdict=pass)的最小混档,断言:FAIL 的 noise_label 非 None、PASS 的为 None、
    by_instruction 里 fail_count>0 的行 noise_label 非 None。
    """

    @staticmethod
    def _mixed_fake():
        """通用 mock call_claude:prohibition 全 FAIL、conditional 全 N/A、step 全 PASS。"""

        def fake(prompt, *a, **k):
            return json.dumps(
                [
                    {
                        "index": i,
                        "applicable": False,
                        "violated": True,
                        "verdict": "pass",
                        "evidence": "violated",
                    }
                    for i in range(60)
                ]
            )

        return fake

    def test_findings_and_by_instruction_are_noise_annotated(self, tmp_path, monkeypatch) -> None:
        """audit() 产出的 findings / by_instruction 都被 noise 标注。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", self._mixed_fake())

        body = "NEVER fabricate data\n\n1. Analyze input format"
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

        # 最少:1 FAIL(prohibition)+ 1 PASS(step),覆盖 FAIL / 非 FAIL 两侧
        fails = [f for f in report.findings if f.verdict == AuditVerdict.FAIL]
        non_fails = [f for f in report.findings if f.verdict != AuditVerdict.FAIL]
        assert fails, "前置:至少有 1 条 FAIL finding(否则测试无意义)"
        assert non_fails, "前置:至少有 1 条非 FAIL finding(否则测不到 None 分支)"

        # ① FAIL finding 的 noise_label ∈ {tool_noise, skill_issue, uncertain}(非 None)
        for f in fails:
            assert f.noise_label in {"tool_noise", "skill_issue", "uncertain"}, (
                f"FAIL finding 应被标 noise_label,得到 {f.noise_label!r}"
            )
            assert f.noise_reasons, "FAIL finding 应附非空 noise_reasons"

        # ② PASS / N/A / 其它非 FAIL finding 的 noise_label 为 None、reasons 空
        for f in non_fails:
            assert f.noise_label is None, f"非 FAIL finding 不应被标 noise_label,得到 {f.noise_label!r}"
            assert f.noise_reasons == []

        # ③ by_instruction 里 fail_count>0 的行 noise_label 非 None;
        #    fail_count==0 的行 noise_label 为 None
        fail_rows = [r for r in report.by_instruction if r.fail_count > 0]
        no_fail_rows = [r for r in report.by_instruction if r.fail_count <= 0]
        assert fail_rows, "前置:至少有 1 行 fail_count>0 的聚合"
        for r in fail_rows:
            assert r.noise_label in {"tool_noise", "uncertain"}, (
                f"fail_count>0 的行应被标 noise_label,得到 {r.noise_label!r}"
            )
        for r in no_fail_rows:
            assert r.noise_label is None


class TestProgressCallback:
    """on_progress 回调:audit 长跑(每批 LLM 40-257s)时让用户看到进度,而非全程静默。

    契约:audit() / _judge_conditional / _judge_prohibition / _judge_step 接可选
    on_progress(str)->None,默认 None(核心不耦合 I/O)。进 transcript 打头;每个 LLM
    批前后各打一行——类别名前置括号化([类别],三类并发交错可分清)、单批不显序号、
    多批显 "批 k/总"、+ 完成耗时。on_progress=None 时行为与旧版一致。
    """

    @staticmethod
    def test_conditional_judge_emits_per_batch_progress(monkeypatch) -> None:
        """未命中走 LLM → on_progress 收到 [条件性] 批 + 完成耗时行(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="某场景", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "[条件性]" in flat  # 类别名前置括号化(三类并发交错可分清)
        assert "[1/1]" not in flat  # 单批不显 [k/总](原 [1/1] 是噪声)
        assert "⏱" in flat  # 完成耗时行

    @staticmethod
    def test_conditional_multi_batch_progress_indexed(monkeypatch) -> None:
        """>_MAX_LLM_IN_BATCH(12 条)→ 分 2 批,on_progress 标 "批 1/2" 与 "批 2/2"。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps([{"index": i, "applicable": False} for i in range(_MAX_LLM_IN_BATCH)]),
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst()] * 12,
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "批 1/2" in flat
        assert "批 2/2" in flat

    @staticmethod
    def test_prohibition_judge_emits_progress(monkeypatch) -> None:
        """禁令批 → on_progress 含 [禁令] 标签 + ⏱(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "未违反"}]',
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_prohibition(
            instructions=[_prohibition_inst()],
            evidence=JudgeTranscript(text="执行记录", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "[禁令]" in flat
        assert "[1/1]" not in flat
        assert "⏱" in flat

    @staticmethod
    def test_step_judge_emits_progress(monkeypatch) -> None:
        """步骤批 → on_progress 含 [步骤] 标签 + ⏱(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "pass", "evidence": "走过"}]',
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_step(
            instructions=[_step_inst()],
            evidence=JudgeTranscript(text="执行记录", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "[步骤]" in flat
        assert "[1/1]" not in flat
        assert "⏱" in flat

    @staticmethod
    def test_empty_bucket_does_not_call_progress(monkeypatch) -> None:
        """空禁令桶短路 → 不 spawn LLM → on_progress 不被调(不报假进度)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("空桶不应调 LLM"),
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_prohibition(
            instructions=[],
            evidence=JudgeTranscript(text="执行记录", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        assert msgs == []

    @staticmethod
    def test_audit_emits_transcript_header(tmp_path, monkeypatch) -> None:
        """audit() 进每个 transcript 打头:'对账 transcript i/N: <name>'。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        body = (
            "use Bash\n\n"
            "NEVER delete production data\n\n"
            "1. Analyze the input format carefully\n\n"
            "```\nEXPECTED_TOKEN\n```\n"
        )
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        msgs: list[str] = []
        TranscriptAuditor().audit(
            skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions(on_progress=msgs.append)
        )
        flat = " ".join(msgs)
        assert "对账 transcript 1/1" in flat
        assert "only_user.jsonl" in flat

    @staticmethod
    def test_progress_none_default_is_noop(monkeypatch) -> None:
        """on_progress=None(默认)→ 与旧版完全一致,无 AttributeError。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": ""}]',
        )
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_prohibition(
            instructions=[_prohibition_inst()],
            evidence=JudgeTranscript(text="记录", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )
        assert len(findings) == 1  # 不传 on_progress 也能正常判定

    @staticmethod
    def test_failure_progress_includes_reason(monkeypatch) -> None:
        """整批 LLM 失败时,进度行要带**原因**(str(exc)),不只'兜底 N/A'。

        回归真实痛点:旧版 ``except RuntimeError: continue`` 把异常吞了,进度只写
        '失败(整批兜底 N/A)'——用户看到'都判定失败'却不知为什么(超时?exit 1?限流?)。
        带 str(exc) 后,失败原因(如 'Reached max turns' / 'timed out after 600s')
        即时可见,才好诊断"都判定失败"的根因。
        """

        def boom(*a, **k):
            raise RuntimeError("claude CLI failed (exit 1): Reached max turns (1)")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_prohibition(
            instructions=[_prohibition_inst()],
            evidence=JudgeTranscript(text="记录", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "失败" in flat
        assert "Reached max turns" in flat  # 原因带出来了,不只'兜底 N/A'
        # 判定本身仍兜底为 UNRESOLVED(没判成,失败不报假 FAIL)
        assert findings[0].verdict == AuditVerdict.UNRESOLVED

    @staticmethod
    def test_zero_parsed_batch_emits_warning_prohibition(monkeypatch) -> None:
        """调用成功但整批 0 条解析成功(LLM 输出畸形/空)→ 打可见警告,不只静默 N/A。

        回归盲点:批"完成 ⏱ Xs"看着像成功,实则 LLM 返回畸形/截断/空数组 → _parse_*
        全 None → 全兜底 N/A,却**毫无信号**(失败原因日志只覆盖 RuntimeError 调用失败,
        不覆盖这种"调用成功但解析为空")。补一条 ⚠ 警告,让静默 N/A 可见。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: "[]",  # 空数组 → 0 条解析
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_prohibition(
            instructions=[_prohibition_inst(), _prohibition_inst()],
            evidence=JudgeTranscript(text="记录", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "完成" in flat  # 调用确实成功了(不是 RuntimeError)
        assert "0 条解析成功" in flat  # ⚠ 但解析为空,可见警告
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in findings)  # 仍兜底 UNRESOLVED(没判成)

    @staticmethod
    def test_zero_parsed_batch_emits_warning_conditional(monkeypatch) -> None:
        """条件性批(标量 mask 形状)同样:0 条解析成功 → ⚠ 警告。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: "[]",  # 空数组 → mask 全 None
        )
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(), _tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "0 条解析成功" in flat
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in findings)

    @staticmethod
    def test_zero_parsed_triggers_retry_and_recovers(monkeypatch) -> None:
        """0 解析成功 → 自动重试,重试拿到能解析的输出 → 真 verdict(非兜底 N/A)。

        实锤价值:同 prompt 重试常能解出(0-解析多为 LLM 输出方差的瞬态),故重试而非直接
        放弃。第一次返回空数组(0 解析)→ 重试 → 第二次返回正常 JSON → 解出 resolved PASS。
        """
        calls = {"n": 0}

        def flaky(prompt, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return "[]"  # 第一次:0 解析
            return '[{"index": 0, "violated": false, "evidence": "重试后解出"}]'  # 重试成功

        monkeypatch.setattr("sift.audit.auditor.call_claude", flaky)
        msgs: list[str] = []
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_prohibition(
            instructions=[_prohibition_inst()],
            evidence=JudgeTranscript(text="记录", name="t.jsonl"),
            call_options=JudgeCallOptions(on_progress=msgs.append),
        )
        flat = " ".join(msgs)
        assert "重试" in flat  # 触发了重试
        assert calls["n"] == 2  # 第一次 0 解析 + 重试一次成功
        assert findings[0].verdict == AuditVerdict.PASS  # violated=false → PASS(resolved)
        assert findings[0].method == AuditMethod.LLM  # resolved LLM,非兜底
