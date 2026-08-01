"""coverage HTML 报告生成器(抄 audit_viewer / trigger_viewer 模式)。"""
import json
from pathlib import Path

from skill_eval.models.coverage_report import CoverageReport

_TEMPLATE = Path(__file__).parent / "templates" / "coverage-report.html"


class CoverageReportGenerator:
    def generate_html(self, report: CoverageReport) -> str:
        tpl = _TEMPLATE.read_text(encoding="utf-8")
        payload = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        return tpl.replace("/*__EMBEDDED_DATA__*/", f"var EMBEDDED_DATA = {payload};")

    def write_html(self, report: CoverageReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.generate_html(report), encoding="utf-8")
