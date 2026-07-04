"""Tests for static analysis engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_eval.static_analysis.analyzer import StaticAnalyzer
from skill_eval.static_analysis.checks import (
    check_description_length,
    check_description_has_trigger,
    check_description_third_person,
    check_name_kebab_case,
    check_name_max_64,
    check_name_no_reserved,
    check_skill_md_max_500_lines,
    check_has_examples,
    check_reference_depth,
    check_anti_fabrication,
    check_source_documented,
    ALL_CHECKS,
)
from skill_eval.static_analysis.classifier import classify_skill
from skill_eval.models import SkillCategory


class TestIndividualChecks:
    def test_description_length_pass(self):
        r = check_description_length({"description": "A" * 25})
        assert r.passed

    def test_description_length_fail(self):
        r = check_description_length({"description": "Short"})
        assert not r.passed

    def test_description_has_trigger_pass(self):
        r = check_description_has_trigger({"description": "Creates and validates data scripts"})
        assert r.passed

    def test_description_has_trigger_fail(self):
        r = check_description_has_trigger({"description": "A thing that does stuff"})
        assert not r.passed

    def test_description_third_person_pass(self):
        r = check_description_third_person({"description": "Creates scripts for the user"})
        assert r.passed

    def test_description_third_person_fail(self):
        r = check_description_third_person({"description": "I create scripts for my users"})
        assert not r.passed

    def test_name_kebab_case_pass(self):
        r = check_name_kebab_case({"name": "my-cool-skill"})
        assert r.passed

    def test_name_kebab_case_fail_uppercase(self):
        r = check_name_kebab_case({"name": "MySkill"})
        assert not r.passed

    def test_name_kebab_case_fail_spaces(self):
        r = check_name_kebab_case({"name": "my skill"})
        assert not r.passed

    def test_name_max_64_pass(self):
        r = check_name_max_64({"name": "short"})
        assert r.passed

    def test_name_max_64_fail(self):
        r = check_name_max_64({"name": "a" * 65})
        assert not r.passed

    def test_name_no_reserved_pass(self):
        r = check_name_no_reserved({"name": "my-skill"})
        assert r.passed

    def test_name_no_reserved_fail(self):
        r = check_name_no_reserved({"name": "help"})
        assert not r.passed

    def test_skill_md_max_500_lines_pass(self):
        r = check_skill_md_max_500_lines("line\n" * 100)
        assert r.passed

    def test_skill_md_max_500_lines_fail(self):
        r = check_skill_md_max_500_lines("line\n" * 501)
        assert not r.passed

    def test_has_examples_pass(self):
        r = check_has_examples("Here is an example:\n```python\nprint('hi')\n```")
        assert r.passed

    def test_has_examples_fail(self):
        r = check_has_examples("Just plain text with no code or demos at all.")
        assert not r.passed

    def test_reference_depth_pass_flat(self, tmp_path: Path):
        ref = tmp_path / "references"
        ref.mkdir()
        (ref / "guide.md").write_text("guide", encoding="utf-8")
        r = check_reference_depth(tmp_path)
        assert r.passed

    def test_reference_depth_fail_nested(self, tmp_path: Path):
        ref = tmp_path / "references"
        (ref / "sub").mkdir(parents=True)
        r = check_reference_depth(tmp_path)
        assert not r.passed

    def test_reference_depth_pass_no_dir(self, tmp_path: Path):
        r = check_reference_depth(tmp_path)
        assert r.passed

    def test_anti_fabrication_pass(self):
        r = check_anti_fabrication("NEVER fabricate data or invent values")
        assert r.passed

    def test_anti_fabrication_fail(self):
        r = check_anti_fabrication("Just do the thing")
        assert not r.passed

    def test_source_documented_pass(self):
        r = check_source_documented("Based on references/guide.md best practices")
        assert r.passed

    def test_source_documented_fail(self):
        r = check_source_documented("Do it this way because I said so")
        assert not r.passed


class TestCheckCount:
    def test_checks_registered(self):
        assert len(ALL_CHECKS) >= 1


class TestClassifier:
    def test_capability_uplift(self):
        cat = classify_skill(
            "Follow this step-by-step workflow for best practice algorithm design",
            "Enhanced comprehensive methodology tool",
        )
        assert cat == SkillCategory.CAPABILITY_UPLIFT

    def test_encoded_preference(self):
        cat = classify_skill(
            "Use this exact template following team convention and company standard",
            "Our project style formatter",
        )
        assert cat == SkillCategory.ENCODED_PREFERENCE


class TestStaticAnalyzer:
    def test_analyze_sample_skill(self, sample_skill_path: Path):
        analyzer = StaticAnalyzer()
        report = analyzer.analyze(sample_skill_path)

        assert report.skill_name == "sample-skill"
        assert report.max_score == len(report.checks)
        assert report.score > 0
        assert len(report.checks) >= 1
        assert report.category in (SkillCategory.CAPABILITY_UPLIFT, SkillCategory.ENCODED_PREFERENCE)
        assert report.timestamp

    def test_analyze_minimal_skill(self, tmp_path: Path):
        (tmp_path / "SKILL.md").write_text(
            "---\nname: minimal\ndescription: Short\n---\n\nNo body content.\n",
            encoding="utf-8",
        )
        analyzer = StaticAnalyzer()
        report = analyzer.analyze(tmp_path)

        assert report.skill_name == "minimal"
        assert report.score < report.max_score


class TestIndividualChecksChinese:
    """中文 SKILL.md 的静态检查(P1-3:补中文关键词后不误报)."""

    def test_description_has_trigger_cn(self):
        assert check_description_has_trigger({"description": "Ascend C 算子 Tiling 设计指南"}).passed
        assert check_description_has_trigger({"description": "根据需求生成设计文档"}).passed
        assert check_description_has_trigger({"description": "分析性能并优化算子"}).passed

    def test_description_has_trigger_cn_no_verb_still_fails(self):
        assert not check_description_has_trigger({"description": "关于算子的一些说明文字"}).passed

    def test_description_third_person_cn_pass(self):
        assert check_description_third_person({"description": "为算子生成 Tiling 设计方案"}).passed

    def test_description_third_person_cn_fail(self):
        assert not check_description_third_person({"description": "我会帮你生成算子设计"}).passed
        assert not check_description_third_person({"description": "我们来分析性能"}).passed

    def test_has_examples_cn(self):
        assert check_has_examples("如下示例展示用法").passed
        assert check_has_examples("举个例子说明").passed
        assert not check_has_examples("只有普通文字没有任何代码或演示").passed

    def test_anti_fabrication_cn(self):
        assert check_anti_fabrication("严禁捏造数据,不得编造结果").passed
        assert check_anti_fabrication("禁止虚构内容").passed

    def test_source_documented_cn(self):
        assert check_source_documented("根据官方文档整理").passed
        assert check_source_documented("参考资料见 references/guide.md").passed
