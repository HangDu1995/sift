"""Auditor dispatch behavior tests."""

from __future__ import annotations

import json

from transcript_fixtures import dispatch_sequence

from sift.audit.auditor import (
    TranscriptAuditor,
)
from sift.audit.judge_context import (
    JudgeCallOptions,
    JudgePromptContext,
    JudgeTranscript,
)
from sift.models import InstructionCheck, ToolCallRecord
from sift.models.audit_models import (
    AuditMethod,
    AuditVerdict,
)
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)

from .helpers import _prohibition_inst, _ref_inst, _tc, _tool_inst


class TestAuxSubagentRescue:
    """A1 补充证据通道:条件类 root trace 未命中 → aux 子代理痕迹第二遍确定性匹配。

    编排范式下执行类要求(跑命令/读文件)全部由子代理实际执行,主 scope 恒缺席;
    aux 命中即"经派发执行"的事实 → PASS(确定性,不进 LLM),evidence 归属执行者。
    """

    @staticmethod
    def test_aux_none_and_empty_identical(monkeypatch) -> None:
        """aux=None 与 aux=[]:均无补充证据通道,未命中走 LLM(向后兼容)。"""

        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": False, "reason": "不涉及"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        for aux in (None, []):
            findings = transcript_auditor.engine.judge_conditional(
                instructions=[_tool_inst(tool_name="curl")],
                evidence=JudgeTranscript(tool_trace=[_tc("Bash", "ls")], scene="场景", name="t.jsonl", aux_trace=aux),
                call_options=JudgeCallOptions(),
            )
            assert findings[0].verdict == AuditVerdict.NA

    @staticmethod
    def _aux_tc(name: str, inp: dict, agent: str = "verifier", turn: int = 42) -> ToolCallRecord:
        return ToolCallRecord(
            name=name,
            input_summary=json.dumps(inp, ensure_ascii=False)[:200],
            input=inp,
            turn=turn,
            agent=agent,
        )

    def test_aux_hit_pass_no_llm(self, monkeypatch) -> None:
        """root 未命中 + aux 命中 → PASS,method=PROGRAMMATIC,evidence 标注子代理
        角色/turn/经派发执行,call_claude 未被调用。
        """
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        transcript_auditor = TranscriptAuditor()
        root_trace = [_tc("Read", "读 STATE.md")]
        aux = [self._aux_tc("Bash", {"command": "nm -D libdemo_op.so"}, agent="code-verifier", turn=118)]

        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(text="使用 nm 检查符号", tool_name="nm")],
            evidence=JudgeTranscript(tool_trace=root_trace, scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert calls["n"] == 0  # 确定性命中,不调 LLM
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.method == AuditMethod.PROGRAMMATIC
        assert "code-verifier" in f.evidence
        assert "T118" in f.evidence
        assert "经派发执行" in f.evidence
        assert "主 trace 未命中" in f.evidence  # 与主路径 PASS 的 evidence 区分

    def test_root_hit_takes_priority_over_aux(self, monkeypatch) -> None:
        """root 命中优先:两条 trace 都有 → 走主路径 evidence("trace 中调用了"),
        不提子代理(主 agent 亲自执行)。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: "[]")
        transcript_auditor = TranscriptAuditor()
        root_trace = [_tc("Bash", "nm -D x.so")]
        aux = [self._aux_tc("Bash", {"command": "nm -D y.so"})]

        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(tool_name="nm")],
            evidence=JudgeTranscript(tool_trace=root_trace, scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].evidence.startswith("trace 中调用了")

    def test_aux_miss_falls_to_llm(self, monkeypatch) -> None:
        """root + aux 都未命中 → 走 LLM 兜底(行为与旧版一致)。"""

        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(tool_name="curl")],
            evidence=JudgeTranscript(
                tool_trace=[_tc("Bash", "ls")],
                scene="场景",
                name="t.jsonl",
                aux_trace=[self._aux_tc("Read", {"filePath": "a.md"})],
            ),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.FAIL
        assert findings[0].method == AuditMethod.LLM

    def test_aux_reference_usage_filepath(self, monkeypatch) -> None:
        """REFERENCE_USAGE 的 aux 命中:子代理 Read 的 filePath(opencode camelCase)
        匹配声明 script_path → PASS,evidence 标注"读取"。
        """
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        transcript_auditor = TranscriptAuditor()
        aux = [
            self._aux_tc(
                "Read",
                {"filePath": "/home/dev/skills/reporting/references/demo-contract.md"},
                agent="developer",
                turn=1531,
            )
        ]
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[
                _ref_inst(
                    text="Use reference file: references/demo-contract.md", script_path="references/demo-contract.md"
                )
            ],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert calls["n"] == 0
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert "developer" in f.evidence and "读取" in f.evidence

    def test_aux_required_params_still_enforced(self, monkeypatch) -> None:
        """带 required_params 的条目:aux 命中工具但参数不满足 → 不算命中(走 LLM)。
        参数级断言不因补充证据通道放宽("工具名对、参数没照做"不放过)。
        """

        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        inst = _tool_inst(text="执行 pytest --tb=short", tool_name="pytest")
        inst.params["required_params"] = ["--tb=short"]
        aux = [self._aux_tc("Bash", {"command": "pytest"})]  # 无 --tb=short
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[inst],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # 参数不满足,补充证据通道不洗

    def test_aux_strict_no_text_mention_rescue(self, monkeypatch) -> None:
        """严格匹配:Write/Edit 文档**内容**里提及工具名不算执行(假救援防护)。
        实测回归:子代理 Write 状态文件内容含"docs-gen"字样,被宽匹配误判
        "已调用"——该 skill 实际从未被加载(收尾任务未开始)。
        """

        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        aux = [
            self._aux_tc(
                "Write",
                {"content": "# 工作流\n- [ ] 40. 调用 docs-gen 生成文档"},
                agent="state-generator",
            )
        ]
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(text="调用 `docs-gen", tool_name="docs-gen")],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # 文本提及不洗

    def test_aux_strict_skill_invoke_rescues(self, monkeypatch) -> None:
        """严格匹配:Skill tool_use 且 input.skill 含工具名 = 真加载 → PASS。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        transcript_auditor = TranscriptAuditor()
        aux = [self._aux_tc("Skill", {"skill": "env-check"}, agent="developer", turn=7)]
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(text="使用 env-check skill", tool_name="env-check")],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert calls["n"] == 0
        assert findings[0].verdict == AuditVerdict.PASS
        assert "developer" in findings[0].evidence

    def test_aux_strict_command_field_only_for_bash(self, monkeypatch) -> None:
        """严格匹配:Bash 命令词只搜 command 字段;input 其他文本负载(如 description)
        提及不算。
        """

        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        aux = [self._aux_tc("Bash", {"command": "ls -la", "description": "运行 nm 检查前先看目录"})]
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(text="使用 nm", tool_name="nm")],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # description 提及不洗

    def test_aux_linked_evidence_cites_dispatch(self, monkeypatch) -> None:
        """派发关联:命中记录的 parent_tool_use_id 对上 dispatch_returns 里的
        dispatch_id → evidence 引用到具体派发([T9] 派发 verifier「验收…」的子代理),
        「经派发执行」成为可查链路;executor 角色/turn 仍保留。
        """
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        from sift.evals.transcript import DispatchReturn

        transcript_auditor = TranscriptAuditor()
        aux = [self._aux_tc("Bash", {"command": "nm -D libdemo_op.so"}, agent="code-verifier", turn=118)]
        aux[0].parent_tool_use_id = "d1"
        dispatches = [
            DispatchReturn(
                dispatch_id="d1", dispatch_turn=9, result_turn=10, role="verifier", prompt_head="验收 Task 47:黑盒全量"
            )
        ]
        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(text="使用 nm 检查符号", tool_name="nm")],
            evidence=JudgeTranscript(
                tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux, dispatch_returns=dispatches
            ),
            call_options=JudgeCallOptions(),
        )
        assert calls["n"] == 0
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        ev = f.evidence
        assert "主 trace 未命中" in ev and "经派发执行" in ev
        assert "[T9] 派发 verifier「验收 Task 47:黑盒全量」的子代理 code-verifier(T118)" in ev

    def test_aux_unlinked_falls_back_to_plain_form(self, monkeypatch) -> None:
        """关联不上(insight 路 ptid=subagentSessionId / 不传 dispatch_returns /
        id 对不上)→ evidence 回退归并形态(与旧版一致),不带派发引用。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: "[]")
        from sift.evals.transcript import DispatchReturn

        transcript_auditor = TranscriptAuditor()
        aux = [self._aux_tc("Bash", {"command": "nm -D x.so"}, agent="code-verifier", turn=42)]
        aux[0].parent_tool_use_id = "ses_sub_123"  # insight 形:对不上任何 dispatch_id
        dispatches = [DispatchReturn(dispatch_id="d1", dispatch_turn=9, role="verifier", prompt_head="验收")]
        # ① 传了 dispatch_returns 但 id 对不上
        f1 = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(tool_name="nm")],
            evidence=JudgeTranscript(
                tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux, dispatch_returns=dispatches
            ),
            call_options=JudgeCallOptions(),
        )[0]
        assert f1.verdict == AuditVerdict.PASS
        # 无派发引用(「派发 <role>」引用形态不出现;「经派发执行」锚语不算)
        assert "派发 verifier" not in f1.evidence and "子代理 code-verifier(T42)" in f1.evidence
        # ② 不传 dispatch_returns(None)
        f2 = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(tool_name="nm")],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux),
            call_options=JudgeCallOptions(),
        )[0]
        assert f2.verdict == AuditVerdict.PASS
        assert "派发 verifier" not in f2.evidence

    def test_aux_linked_empty_role_composes_cleanly(self, monkeypatch) -> None:
        """派发关联命中但派发无角色名 → evidence 不得拼出「子代理的子代理」
        (空角色省略该段;[T] 派发引用仍保留)。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: "[]")
        from sift.evals.transcript import DispatchReturn

        transcript_auditor = TranscriptAuditor()
        aux = [self._aux_tc("Bash", {"command": "nm -D x.so"}, agent="code-verifier", turn=7)]
        aux[0].parent_tool_use_id = "d1"
        dispatches = [DispatchReturn(dispatch_id="d1", dispatch_turn=3, role="", prompt_head="")]
        f = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(tool_name="nm")],
            evidence=JudgeTranscript(
                tool_trace=[], scene="场景", name="t.jsonl", aux_trace=aux, dispatch_returns=dispatches
            ),
            call_options=JudgeCallOptions(),
        )[0]
        assert f.verdict == AuditVerdict.PASS
        assert "子代理的子代理" not in f.evidence
        assert "[T3] 派发的子代理 code-verifier(T7)" in f.evidence


class TestStepDispatchPairs:
    """层3:派发-回报对喂 step 判官——prompt 含"派发-回报记录"段与"传递≠达成"规则;
    SIFT_DISPATCH_PAIRS=0 或 pairs=None 时行为与旧版一致。全程 mock call_claude。
    """

    @staticmethod
    def _pair(role="verifier", result="FAILED: 254/275"):
        from sift.evals.transcript import DispatchReturn

        return DispatchReturn(
            dispatch_turn=9,
            result_turn=10,
            role=role,
            prompt_head="验收 Task 47:黑盒全量",
            result_head=result,
            result_len=len(result),
        )

    @staticmethod
    def _step_inst():
        return InstructionCheck(
            source=InstructionSource.HEADING_STEP,
            instruction_text="验收标准:黑盒全部用例全量执行且全部通过",
            section="Tasks",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE,
            params={},
        )

    def test_pairs_section_in_step_judge_prompt(self, monkeypatch) -> None:
        """_llm_judge_step 的 prompt 含 pairs 段 + 规则行(传递≠达成)。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "pass", "evidence": "走过"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_step_batches(
            candidates=[self._step_inst()],
            evidence=JudgePromptContext(text="执行记录文本", dispatch_pairs=[self._pair()], aux_tool_trace=None),
            call_options=JudgeCallOptions(model=None, timeout=600),
        )
        assert len(prompts) == 1
        assert "派发-回报记录" in prompts[0]
        assert "回报(子代理产出)" in prompts[0]
        assert "FAILED: 254/275" in prompts[0]
        assert "传递了要求 ≠ 达成了结果" in prompts[0]  # 规则行进 prompt

    def test_pairs_none_keeps_old_prompt(self, monkeypatch) -> None:
        """pairs=None → 段/规则都不进(prompt 与旧版同构)。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "na", "evidence": "不涉及"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_step_batches(
            candidates=[self._step_inst()],
            evidence=JudgePromptContext(text="执行记录文本", dispatch_pairs=None),
            call_options=JudgeCallOptions(model=None, timeout=600),
        )
        assert "派发-回报记录" not in prompts[0]
        assert "传递了要求 ≠ 达成了结果" not in prompts[0]

    def test_judge_step_passes_pairs_through(self, monkeypatch) -> None:
        """_judge_step → _llm_judge_step 穿线:pairs 到达 prompt。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "pass", "evidence": "ok"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        findings = transcript_auditor.engine.judge_step(
            instructions=[self._step_inst()],
            evidence=JudgeTranscript(text="文本", name="t.jsonl", dispatch_returns=[self._pair()]),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.PASS
        assert "派发-回报记录" in prompts[0]


class TestProhibitionPairsShare:
    """层3-改进3:prohibition 判官共享 pairs 段(SIFT_PROHIBITION_PAIRS=1)。"""

    @staticmethod
    def test_default_none_pairs_keeps_old_prompt(monkeypatch) -> None:
        """pairs 不传/None(或 env 显式关):prohibition prompt 不含 pairs 段。
        默认开(实验转正),env SIFT_PROHIBITION_PAIRS=0 是逃生口——这里验关路。
        """
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "violated": False, "evidence": "未违反"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setenv("SIFT_PROHIBITION_PAIRS", "0")
        transcript_auditor = TranscriptAuditor()
        from sift.evals.transcript import DispatchReturn

        pairs = [
            DispatchReturn(
                dispatch_turn=1, result_turn=2, role="verifier", prompt_head="验收", result_head="FAILED", result_len=6
            )
        ]
        transcript_auditor.engine.judge_prohibition_batches(
            candidates=[_prohibition_inst()],
            evidence=JudgePromptContext(text="文本", dispatch_pairs=pairs),
            call_options=JudgeCallOptions(model=None, timeout=600),
        )
        # 注:env 门在 audit() 调用点(默认开才传 pairs);_llm 层直传 pairs 仍渲染段。
        # 本测试锚定的是"不传 pairs 的默认 prompt 形态"(旧版兼容路径)。
        transcript_auditor.engine.judge_prohibition_batches(
            candidates=[_prohibition_inst()],
            evidence=JudgePromptContext(text="文本"),
            call_options=JudgeCallOptions(model=None, timeout=600),
        )
        assert "派发-回报记录" not in prompts[1]

    @staticmethod
    def test_on_with_env(monkeypatch) -> None:
        """env=1:pairs 段进 prohibition prompt(由 _judge_prohibition 穿线;
        这里直接验 _llm_judge_prohibition 的段拼装)。
        """
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "violated": False, "evidence": "未违反"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        from sift.evals.transcript import DispatchReturn

        pairs = [
            DispatchReturn(
                dispatch_turn=1,
                result_turn=2,
                role="verifier",
                prompt_head="验收 Task 47",
                result_head="FAILED: 254/275",
                result_len=15,
            )
        ]
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_prohibition_batches(
            candidates=[_prohibition_inst()],
            evidence=JudgePromptContext(text="文本", dispatch_pairs=pairs),
            call_options=JudgeCallOptions(model=None, timeout=600),
        )
        assert "派发-回报记录" in prompts[0]
        assert "FAILED: 254/275" in prompts[0]


class TestDispatchPairsRuleScenarios:
    """「传递≠达成」规则的**场景级证据契约**:合成 cc transcript 走真实
    rebuild → digest.dispatch_returns → _llm_judge_step prompt 链路(只 mock
    call_claude 抓 prompt),断言两种典型场景下判官**实际收到**的证据齐备。

    这层钉的是证据送达,不是判官判决(判决方向由 tests/test_live_judge_pairs.py
    的 env 门控 live 测试钉)——两层合起来防「换判官模型后规则静默漂移」:
    契约测试保证证据必然在场,live 测试保证规则真的被服从。
    """

    @staticmethod
    def _dispatch_seq(
        user_task: str, pairs: list[tuple[str, str, str, str]], closing: str = "全部任务完成,归档收尾。"
    ) -> list[dict]:
        """合成 cc 形 records:初始 user turn + N 个 task 派发/回报对 + 收尾 assistant 文本。"""
        return dispatch_sequence(user_task, pairs, closing)

    @staticmethod
    def _acceptance_inst() -> InstructionCheck:
        return InstructionCheck(
            source=InstructionSource.HEADING_STEP,
            instruction_text="验收标准:黑盒全量用例全部执行且全部通过;未通过须修复并复验,通过前不得推进收尾任务",
            section="Tasks",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE,
            params={},
        )

    def test_scenario_gate_breach_evidence_served(self, tmp_path, monkeypatch) -> None:
        """场景A(门禁失守):验收 FAILED 后**无修复**直接派发收尾 → 判官 prompt
        必须同时含 FAILED 结论、后续收尾派发行、规则行,且失败回报在派发之前
        (判官看得到「FAILED → 仍推进」的时序)。
        """
        recs = self._dispatch_seq(
            "对 demo-repo 做构建检查:验收通过后才能收尾",
            [
                ("d1", "verifier", "验收 Task 1:黑盒全量用例执行,报告达标率", "黑盒全量执行完毕:FAILED 23/30 用例达标"),
                ("d2", "developer", "收尾任务:整理归档材料", "DONE"),
            ],
        )
        prompt, digest = self._judge_prompt(tmp_path, monkeypatch, recs)
        assert len(digest.dispatch_returns) == 2
        i_pairs = prompt.find("派发-回报记录")
        i_failed = prompt.find("FAILED 23/30 用例达标")
        i_followup = prompt.find("收尾任务:整理归档材料")
        i_rule = prompt.find("传递了要求 ≠ 达成了结果")
        assert i_pairs >= 0 and i_failed >= 0 and i_followup >= 0 and i_rule >= 0
        assert i_pairs < i_failed < i_followup  # 时序可见:失败在前、推进在后

    def test_scenario_remediation_evidence_served(self, tmp_path, monkeypatch) -> None:
        """场景B(规范处置):验收 FAILED → 修复派发 → 复验通过 → 才收尾 → 判官
        prompt 必须含完整处置链(FAILED 结论 + 修复派发行 + 复验通过结论)。
        """
        recs = self._dispatch_seq(
            "对 demo-repo 做构建检查:验收通过后才能收尾",
            [
                ("d1", "verifier", "验收 Task 1:黑盒全量用例执行,报告达标率", "黑盒全量执行完毕:FAILED 23/30 用例达标"),
                ("d2", "developer", "修复未达标用例并复验", "修复完成:7 个用例已修复"),
                ("d3", "verifier", "复验黑盒全量用例", "复验执行完毕:状态 ✅通过,30/30 用例达标"),
                ("d4", "developer", "收尾任务:整理归档材料", "DONE"),
            ],
        )
        prompt, digest = self._judge_prompt(tmp_path, monkeypatch, recs)
        assert len(digest.dispatch_returns) == 4
        for needle in ("FAILED 23/30 用例达标", "修复未达标用例并复验", "30/30 用例达标", "收尾任务:整理归档材料"):
            assert needle in prompt, f"处置链证据缺环: {needle}"
        # 时序:FAILED 在修复派发之前(判官看得到「失败→处置」因果顺序)
        assert prompt.find("FAILED 23/30 用例达标") < prompt.find("修复未达标用例并复验")

    def _judge_prompt(self, tmp_path, monkeypatch, recs: list[dict]) -> tuple[str, object]:
        """写 jsonl → 真实 rebuild → _llm_judge_step(mock call_claude 抓 prompt)。"""
        from sift.audit.rebuild import rebuild

        tp = tmp_path / "scenario.jsonl"
        tp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
        tool_trace, text, digest = rebuild(tp, locate_turns=True)
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "pass", "evidence": "ok"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        transcript_auditor.engine.judge_step_batches(
            candidates=[self._acceptance_inst()],
            evidence=JudgePromptContext(text=text, dispatch_pairs=digest.dispatch_returns),
            call_options=JudgeCallOptions(model=None, timeout=600),
        )
        assert len(prompts) == 1
        return prompts[0], digest
