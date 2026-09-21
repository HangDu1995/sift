"""Static analysis checks for SKILL.md health."""

from __future__ import annotations

import re
from pathlib import Path

from sift.i18n import (
    ANTI_FABRICATION_CN,  # 反捏造词与 compliance/extractor.py 共用(P2-13)
)
from sift.models import CheckResult

_TRIGGER_WORDS = [
    # 英文
    "create",
    "generate",
    "build",
    "write",
    "implement",
    "refactor",
    "convert",
    "extract",
    "analyze",
    "design",
    "set up",
    "configure",
    "debug",
    "fix",
    "optimize",
    "review",
    "test",
    "deploy",
    "migrate",
    "transform",
    "help",
    "assist",
    "manage",
    "handle",
    "process",
    # 中文动作动词(P1-3:否则中文 description 全部漏判)
    "创建",
    "生成",
    "构建",
    "编写",
    "实现",
    "重构",
    "转换",
    "提取",
    "分析",
    "设计",
    "配置",
    "调试",
    "修复",
    "优化",
    "审查",
    "测试",
    "部署",
    "迁移",
    "管理",
    "处理",
    "使用",
]


def check_description_length(frontmatter: dict) -> CheckResult:
    desc = str(frontmatter.get("description", "")).strip()
    if len(desc) >= 20:
        return CheckResult(name="description_length", passed=True, message=f"Description length: {len(desc)} chars")
    return CheckResult(
        name="description_length", passed=False, message=f"Description too short ({len(desc)} chars), minimum 20"
    )


def check_description_has_trigger(frontmatter: dict) -> CheckResult:
    desc = str(frontmatter.get("description", "")).lower()
    if any(w in desc for w in _TRIGGER_WORDS):
        return CheckResult(name="description_has_trigger", passed=True, message="Description contains trigger word")
    return CheckResult(name="description_has_trigger", passed=False, message="Description lacks action trigger words")


def check_description_third_person(frontmatter: dict) -> CheckResult:
    desc = str(frontmatter.get("description", ""))
    first_person = [
        # 英文
        " i ",
        " i'll",
        " i'm",
        " i can",
        " i will",
        " i use",
        " my ",
        # 中文第一人称(用词组避免单字"我"误匹配"自我"等)
        "我会",
        "我来",
        "我的",
        "我们",
        "我可以",
        "我帮",
    ]
    lower = f" {desc.lower()} "
    if not any(p in lower for p in first_person):
        return CheckResult(name="description_third_person", passed=True, message="Description uses third person")
    return CheckResult(
        name="description_third_person", passed=False, message="Description uses first person (should be third person)"
    )


def check_name_kebab_case(frontmatter: dict) -> CheckResult:
    name = str(frontmatter.get("name", ""))
    if not name:
        return CheckResult(name="name_kebab_case", passed=False, message="Name is empty")
    if re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
        return CheckResult(name="name_kebab_case", passed=True, message=f"Name '{name}' is kebab-case")
    return CheckResult(name="name_kebab_case", passed=False, message=f"Name '{name}' is not kebab-case")


def check_name_max_64(frontmatter: dict) -> CheckResult:
    name = str(frontmatter.get("name", ""))
    if len(name) <= 64:
        return CheckResult(name="name_max_64", passed=True, message=f"Name length: {len(name)} chars")
    return CheckResult(name="name_max_64", passed=False, message=f"Name too long ({len(name)} chars), max 64")


def check_name_no_reserved(frontmatter: dict) -> CheckResult:
    name = str(frontmatter.get("name", "")).lower()
    reserved = {"help", "init", "run", "eval", "test", "skill", "create", "list", "config"}
    if name not in reserved:
        return CheckResult(name="name_no_reserved", passed=True, message="Name is not a reserved word")
    return CheckResult(name="name_no_reserved", passed=False, message=f"Name '{name}' is a reserved word")


def check_skill_md_max_500_lines(content: str) -> CheckResult:
    line_count = len(content.split("\n"))
    if line_count <= 500:
        return CheckResult(name="skill_md_max_500_lines", passed=True, message=f"SKILL.md has {line_count} lines")
    return CheckResult(
        name="skill_md_max_500_lines", passed=False, message=f"SKILL.md has {line_count} lines (max 500)"
    )


def check_has_examples(body: str) -> CheckResult:
    example_patterns = [
        "example",
        "sample",
        "```",
        "for instance",
        "e.g.",
        # 中文(P1-3)
        "示例",
        "样例",
        "举例",
        "例如",
        "例子",
    ]
    lower = body.lower()
    if any(p in lower for p in example_patterns):
        return CheckResult(name="has_examples", passed=True, message="SKILL.md contains examples")
    return CheckResult(name="has_examples", passed=False, message="SKILL.md lacks examples")


def check_reference_depth(skill_path: Path) -> CheckResult:
    ref_dir = skill_path / "references"
    if not ref_dir.exists():
        return CheckResult(name="reference_depth", passed=True, message="No references/ directory (OK)")
    for child in ref_dir.iterdir():
        if not (child.is_dir()):
            continue
        return CheckResult(
            name="reference_depth",
            passed=False,
            message=f"references/ has subdirectory '{child.name}' (should be flat)",
        )
    return CheckResult(name="reference_depth", passed=True, message="references/ directory is flat")


def check_anti_fabrication(body: str) -> CheckResult:
    lower = body.lower()
    patterns = [
        "anti-fabrication",
        "never fabricate",
        "do not fabricate",
        "do not invent",
        "never invent",
        "no hallucination",
    ]
    patterns.extend(ANTI_FABRICATION_CN)  # 中文反捏造词,与 extractor 共用(sift.i18n,P2-13)
    if any(p in lower for p in patterns):
        return CheckResult(name="anti_fabrication", passed=True, message="Contains anti-fabrication guidance")
    return CheckResult(name="anti_fabrication", passed=False, message="Missing anti-fabrication guidance")


def check_source_documented(body: str) -> CheckResult:
    lower = body.lower()
    patterns = [
        "source",
        "reference",
        "references/",
        "scripts/",
        "based on",
        "according to",
        # 中文(P1-3)
        "来源",
        "参考",
        "依据",
        "根据",
        "出处",
    ]
    if any(p in lower for p in patterns):
        return CheckResult(name="source_documented", passed=True, message="Sources/references documented")
    return CheckResult(name="source_documented", passed=False, message="No source documentation found")


ALL_CHECKS = [
    ("frontmatter", check_description_length),
    ("frontmatter", check_description_has_trigger),
    ("frontmatter", check_description_third_person),
    ("frontmatter", check_name_kebab_case),
    ("frontmatter", check_name_max_64),
    ("frontmatter", check_name_no_reserved),
    ("content", check_skill_md_max_500_lines),
    ("body", check_has_examples),
    ("path", check_reference_depth),
    ("body", check_anti_fabrication),
    ("body", check_source_documented),
]
