"""Tests for static analysis engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from sift.models import SkillCategory
from sift.static_analysis.analyzer import StaticAnalyzer
from sift.static_analysis.checks import (
    ALL_CHECKS,
    check_anti_fabrication,
    check_description_has_trigger,
    check_description_length,
    check_description_third_person,
    check_has_examples,
    check_name_kebab_case,
    check_name_max_64,
    check_name_no_reserved,
    check_reference_depth,
    check_skill_md_max_500_lines,
    check_source_documented,
)
from sift.static_analysis.classifier import classify_skill


class TestIndividualChecks:
    @staticmethod
    def test_description_length_pass():
        r = check_description_length({"description": "A" * 25})
        assert r.passed

    @staticmethod
    def test_description_length_fail():
        r = check_description_length({"description": "Short"})
        assert not r.passed

    @staticmethod
    def test_description_has_trigger_pass():
        r = check_description_has_trigger({"description": "Creates and validates data scripts"})
        assert r.passed

    @staticmethod
    def test_description_has_trigger_fail():
        r = check_description_has_trigger({"description": "A thing that does stuff"})
        assert not r.passed

    @staticmethod
    def test_description_third_person_pass():
        r = check_description_third_person({"description": "Creates scripts for the user"})
        assert r.passed

    @staticmethod
    def test_description_third_person_fail():
        r = check_description_third_person({"description": "I create scripts for my users"})
        assert not r.passed

    @staticmethod
    def test_name_kebab_case_pass():
        r = check_name_kebab_case({"name": "my-cool-skill"})
        assert r.passed

    @staticmethod
    def test_name_kebab_case_fail_uppercase():
        r = check_name_kebab_case({"name": "MySkill"})
        assert not r.passed

    @staticmethod
    def test_name_kebab_case_fail_spaces():
        r = check_name_kebab_case({"name": "my skill"})
        assert not r.passed

    @staticmethod
    def test_name_max_64_pass():
        r = check_name_max_64({"name": "short"})
        assert r.passed

    @staticmethod
    def test_name_max_64_fail():
        r = check_name_max_64({"name": "a" * 65})
        assert not r.passed

    @staticmethod
    def test_name_no_reserved_pass():
        r = check_name_no_reserved({"name": "my-skill"})
        assert r.passed

    @staticmethod
    def test_name_no_reserved_fail():
        r = check_name_no_reserved({"name": "help"})
        assert not r.passed

    @staticmethod
    def test_skill_md_max_500_lines_pass():
        r = check_skill_md_max_500_lines("line\n" * 100)
        assert r.passed

    @staticmethod
    def test_skill_md_max_500_lines_fail():
        r = check_skill_md_max_500_lines("line\n" * 501)
        assert not r.passed

    @staticmethod
    def test_has_examples_pass():
        r = check_has_examples("Here is an example:\n```python\nprint('hi')\n```")
        assert r.passed

    @staticmethod
    def test_has_examples_fail():
        r = check_has_examples("Just plain text with no code or demos at all.")
        assert not r.passed

    @staticmethod
    def test_reference_depth_pass_flat(tmp_path: Path):
        ref = tmp_path / "references"
        ref.mkdir()
        (ref / "guide.md").write_text("guide", encoding="utf-8")
        r = check_reference_depth(tmp_path)
        assert r.passed

    @staticmethod
    def test_reference_depth_fail_nested(tmp_path: Path):
        ref = tmp_path / "references"
        (ref / "sub").mkdir(parents=True)
        r = check_reference_depth(tmp_path)
        assert not r.passed

    @staticmethod
    def test_reference_depth_pass_no_dir(tmp_path: Path):
        r = check_reference_depth(tmp_path)
        assert r.passed

    @staticmethod
    def test_anti_fabrication_pass():
        r = check_anti_fabrication("NEVER fabricate data or invent values")
        assert r.passed

    @staticmethod
    def test_anti_fabrication_fail():
        r = check_anti_fabrication("Just do the thing")
        assert not r.passed

    @staticmethod
    def test_source_documented_pass():
        r = check_source_documented("Based on references/guide.md best practices")
        assert r.passed

    @staticmethod
    def test_source_documented_fail():
        r = check_source_documented("Do it this way because I said so")
        assert not r.passed


class TestCheckCount:
    @staticmethod
    def test_checks_registered():
        assert len(ALL_CHECKS) >= 1


class TestClassifier:
    @staticmethod
    def test_capability_uplift():
        cat = classify_skill(
            "Follow this step-by-step workflow for best practice algorithm design",
            "Enhanced comprehensive methodology tool",
        )
        assert cat == SkillCategory.CAPABILITY_UPLIFT

    @staticmethod
    def test_encoded_preference():
        cat = classify_skill(
            "Use this exact template following team convention and company standard",
            "Our project style formatter",
        )
        assert cat == SkillCategory.ENCODED_PREFERENCE

    @staticmethod
    def test_capability_uplift_chinese():
        """中文 upgrade 类 skill 不再被漏判(P2-1:中文信号词)。"""
        cat = classify_skill(
            "按分步工作流执行,采用最佳实践与算法设计,提供进阶增强的全面方法",
            "综合设计工具",
        )
        assert cat == SkillCategory.CAPABILITY_UPLIFT

    @staticmethod
    def test_encoded_preference_chinese():
        """中文 preference 类 skill 能被判 ENCODED_PREFERENCE(此前中文全默认 uplift,分类失效)。"""
        cat = classify_skill(
            "使用此模板,遵循团队约定与编码规范,符合公司标准的项目风格",
            "格式化工具",
        )
        assert cat == SkillCategory.ENCODED_PREFERENCE


class TestStaticAnalyzer:
    @staticmethod
    def test_analyze_sample_skill(sample_skill_path: Path):
        analyzer = StaticAnalyzer()
        report = analyzer.analyze(sample_skill_path)

        assert report.skill_name == "sample-skill"
        assert report.max_score == len(report.checks)
        assert report.score > 0
        assert len(report.checks) >= 1
        assert report.category in (SkillCategory.CAPABILITY_UPLIFT, SkillCategory.ENCODED_PREFERENCE)
        assert report.timestamp

    @staticmethod
    def test_analyze_minimal_skill(tmp_path: Path):
        (tmp_path / "SKILL.md").write_text(
            "---\nname: minimal\ndescription: Short\n---\n\nNo body content.\n",
            encoding="utf-8",
        )
        analyzer = StaticAnalyzer()
        report = analyzer.analyze(tmp_path)

        assert report.skill_name == "minimal"
        assert report.score < report.max_score

    @staticmethod
    def test_analyze_skill_md_file_sees_reference_depth(tmp_path: Path):
        """传 SKILL.md 文件路径跑 analyze:reference_depth 必须真看到同级 references/(防假阳性)。

        陷阱:若 skill_path 没规范化,reference_depth 会拿到文件路径 → 找
        `SKILL.md/references`(不存在)→ 假阳性 "No references/ directory (OK)"。
        此测试守这个边界:扁平 references/ 应报 "flat",而非 "No references"。
        """
        (tmp_path / "SKILL.md").write_text(
            "---\nname: refskill\ndescription: Does useful thing\n---\n\nBody with example.\n",
            encoding="utf-8",
        )
        ref = tmp_path / "references"
        ref.mkdir()
        (ref / "guide.md").write_text("guide", encoding="utf-8")  # 扁平

        report = StaticAnalyzer().analyze(tmp_path / "SKILL.md")  # 传文件,不是目录
        ref_check = next(c for c in report.checks if c.name == "reference_depth")
        assert ref_check.passed
        assert "flat" in ref_check.message  # 真看到了 references/
        assert "No references" not in ref_check.message  # 不是假阳性

    @staticmethod
    def test_analyze_skill_md_file_detects_nested_references(tmp_path: Path):
        """传 SKILL.md 文件:references/ 嵌套时 reference_depth 应 FAIL(证明用对路径)。"""
        (tmp_path / "SKILL.md").write_text(
            "---\nname: refskill\ndescription: Does useful thing\n---\n\nBody with example.\n",
            encoding="utf-8",
        )
        (tmp_path / "references" / "sub").mkdir(parents=True)  # 嵌套

        report = StaticAnalyzer().analyze(tmp_path / "SKILL.md")
        ref_check = next(c for c in report.checks if c.name == "reference_depth")
        assert not ref_check.passed  # 检测到嵌套
        assert "subdirectory" in ref_check.message


class TestIndividualChecksChinese:
    """中文 SKILL.md 的静态检查(P1-3:补中文关键词后不误报)."""

    @staticmethod
    def test_description_has_trigger_cn():
        assert check_description_has_trigger({"description": "Ascend C 算子 Tiling 设计指南"}).passed
        assert check_description_has_trigger({"description": "根据需求生成设计文档"}).passed
        assert check_description_has_trigger({"description": "分析性能并优化算子"}).passed

    @staticmethod
    def test_description_has_trigger_cn_no_verb_still_fails():
        assert not check_description_has_trigger({"description": "关于算子的一些说明文字"}).passed

    @staticmethod
    def test_description_third_person_cn_pass():
        assert check_description_third_person({"description": "为算子生成 Tiling 设计方案"}).passed

    @staticmethod
    def test_description_third_person_cn_fail():
        assert not check_description_third_person({"description": "我会帮你生成算子设计"}).passed
        assert not check_description_third_person({"description": "我们来分析性能"}).passed

    @staticmethod
    def test_has_examples_cn():
        assert check_has_examples("如下示例展示用法").passed
        assert check_has_examples("举个例子说明").passed
        assert not check_has_examples("只有普通文字没有任何代码或演示").passed

    @staticmethod
    def test_anti_fabrication_cn():
        assert check_anti_fabrication("严禁捏造数据,不得编造结果").passed
        assert check_anti_fabrication("禁止虚构内容").passed

    @staticmethod
    def test_source_documented_cn():
        assert check_source_documented("根据官方文档整理").passed
        assert check_source_documented("参考资料见 references/guide.md").passed
