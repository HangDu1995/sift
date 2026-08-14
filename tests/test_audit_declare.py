"""tests for sift.audit.declare.

derive_declarations: 从 transcript digest 反推"真实使用里确实需要的"指令,
补充进指令源。只增不减——返回纯新增(去重 vs existing),不含 existing 本身。
非 mock,直接构造 SessionDigest + InstructionCheck。
"""
from __future__ import annotations

from sift.audit.declare import derive_declarations
from sift.evals.transcript import Command, FileContent, SessionDigest
from sift.models import InstructionCheck
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


def _ref_check(path: str) -> InstructionCheck:
    """造一条 existing REFERENCE_USAGE(模仿 extractor 抽出来的)。"""
    return InstructionCheck(
        source=InstructionSource.REFERENCE_USAGE,
        instruction_text=f"Use reference file: {path}",
        section="References",
        priority=InstructionPriority.RECOMMENDED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"script_path": path},
    )


class TestDeriveReads:
    def test_input_files_derive_reference_usage(self) -> None:
        """digest 有 input_files → 每个 path 反推一条 REFERENCE_USAGE。"""
        digest = SessionDigest(input_files=[
            FileContent(path="references/a.md", content="..."),
            FileContent(path="scripts/run.sh", content="echo hi"),
        ])

        derived = derive_declarations(digest, existing=[])

        assert len(derived) == 2
        assert [d.params["script_path"] for d in derived] == [
            "references/a.md",
            "scripts/run.sh",
        ]
        for d in derived:
            assert d.source == InstructionSource.REFERENCE_USAGE
            assert d.check_method == CheckMethod.TOOL_CALL_CHECK
            assert d.priority == InstructionPriority.RECOMMENDED
            assert d.section == "transcript-derived"
        # instruction_text 中文,含 path
        assert derived[0].instruction_text == "读取 references/a.md"

    def test_dedup_against_existing_same_script_path(self) -> None:
        """existing 已有同 script_path 的 REFERENCE_USAGE → 不重复加(去重)。"""
        digest = SessionDigest(input_files=[
            FileContent(path="references/a.md", content="..."),
            FileContent(path="references/b.md", content="..."),
        ])
        existing = [_ref_check("references/a.md")]  # a 已有

        derived = derive_declarations(digest, existing=existing)

        assert len(derived) == 1
        assert derived[0].params["script_path"] == "references/b.md"

    def test_existing_constraint_does_not_affect_reads(self) -> None:
        """existing 有别的指令(CONSTRAINT)→ 不影响 reads 反推(只增不减)。"""
        digest = SessionDigest(input_files=[
            FileContent(path="references/a.md", content="..."),
        ])
        constraint = InstructionCheck(
            source=InstructionSource.CONSTRAINT,
            instruction_text="不要捏造",
            section="Constraints",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE,
            params={},
        )

        derived = derive_declarations(digest, existing=[constraint])

        assert len(derived) == 1
        assert derived[0].source == InstructionSource.REFERENCE_USAGE
        assert derived[0].params["script_path"] == "references/a.md"

    def test_return_only_new_not_existing(self) -> None:
        """只返回新增:existing 本身绝不出现在返回里。"""
        # case 1: existing 已覆盖 → 返回空(existinig 不回吐)
        digest = SessionDigest(input_files=[
            FileContent(path="references/a.md", content="..."),
        ])
        existing = [_ref_check("references/a.md")]
        assert derive_declarations(digest, existing=existing) == []

        # case 2: existing 是别的 path → 返回只含新增,不含 existing
        digest2 = SessionDigest(input_files=[
            FileContent(path="references/new.md", content="..."),
        ])
        existing2 = [_ref_check("references/old.md")]
        derived2 = derive_declarations(digest2, existing=existing2)

        assert len(derived2) == 1
        assert derived2[0].params["script_path"] == "references/new.md"
        assert all(d.params["script_path"] != "references/old.md" for d in derived2)

    def test_empty_digest_returns_empty(self) -> None:
        """空 digest(无 input_files)→ 返回空 list。"""
        assert derive_declarations(SessionDigest(), existing=[]) == []

    def test_existing_empty_path_not_dedup_key(self) -> None:
        """existing 有空 script_path 的 REFERENCE_USAGE(如类别级 'references/')→
        不应误吞具体文件 path(去重键精确匹配,空串/类别级不等于具体路径)。"""
        digest = SessionDigest(input_files=[
            FileContent(path="references/a.md", content="..."),
        ])
        category = InstructionCheck(
            source=InstructionSource.REFERENCE_USAGE,
            instruction_text="Consult reference material",
            section="References",
            priority=InstructionPriority.RECOMMENDED,
            check_method=CheckMethod.TOOL_CALL_CHECK,
            params={"script_path": "references/"},  # 类别级,与具体文件不同键
        )

        derived = derive_declarations(digest, existing=[category])

        assert len(derived) == 1
        assert derived[0].params["script_path"] == "references/a.md"


class TestDeriveToolsSkipped:
    def test_bash_commands_do_not_derive_tool_requirement(self) -> None:
        """v1 不反推 tools:digest.commands 含 Bash 命令 → 不产生 TOOL_REQUIREMENT。

        commands 的 cmd 是裸 Bash 字符串,业务命令提取不确定(管道/&&/通用 shell
        命令混在一条;python/git/ls/cat 等均非 skill 要求的业务命令),反推留 v2。
        本测试固化 v1 范围,防止将来误把通用命令塞成 TOOL_REQUIREMENT。
        """
        digest = SessionDigest(
            input_files=[FileContent(path="references/a.md", content="...")],
            commands=[
                Command(cmd="python scripts/train.py --epochs 10", stdout=""),
                Command(cmd="ls -la && cat foo.txt | grep bar", stdout=""),
            ],
        )

        derived = derive_declarations(digest, existing=[])

        # 只有 reads 反推(1 条),无任何 TOOL_REQUIREMENT
        assert len(derived) == 1
        assert all(d.source != InstructionSource.TOOL_REQUIREMENT for d in derived)
