"""critique HTML 报告生成器(临时 stub,Task 5 覆写填实)。"""
from pathlib import Path

from skill_eval.models.critique_report import ContentQualityReport


class CritiqueReportGenerator:
    def write_html(self, report: ContentQualityReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<!-- stub; filled in Task 5 -->", encoding="utf-8")
