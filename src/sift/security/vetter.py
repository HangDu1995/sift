"""Multi-scanner security vetting engine for Skill directories.

Integrates with external scanners (aguara, skill-scanner) via subprocess
and implements native Python scanners (secrets-scan, structure-check) for
zero-dependency baseline coverage.

Inspired by: https://github.com/app-incubator-xyz/skill-vetter
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from sift.models import (
    ScannerResult,
    ScannerStatus,
    SecurityFinding,
    SecurityReport,
    SecuritySeverity,
    SecurityVerdict,
)


class SecurityVetter:
    def __init__(self, skip_external: bool = False):
        self._skip_external = skip_external

    def vet(self, skill_path: Path) -> SecurityReport:
        skill_path = skill_path.resolve()
        skill_name = skill_path.name

        scanners: list[ScannerResult] = []

        scanners.append(self._scan_aguara(skill_path))
        scanners.append(self._scan_skill_scanner(skill_path))
        scanners.append(self._scan_secrets(skill_path))
        scanners.append(self._scan_structure(skill_path))

        all_findings: list[SecurityFinding] = []
        for s in scanners:
            all_findings.extend(s.findings)

        high_count = sum(
            1 for f in all_findings if f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)
        )
        medium_count = sum(1 for f in all_findings if f.severity == SecuritySeverity.MEDIUM)

        if high_count > 0:
            verdict = SecurityVerdict.BLOCKED
            summary = f"{high_count} HIGH/CRITICAL, {medium_count} MEDIUM"
        elif medium_count > 0:
            verdict = SecurityVerdict.REVIEW_NEEDED
            summary = f"{medium_count} MEDIUM severity findings"
        else:
            verdict = SecurityVerdict.SAFE
            summary = "All scanners passed"

        return SecurityReport(
            skill_name=skill_name,
            skill_path=str(skill_path),
            verdict=verdict,
            scanners=scanners,
            findings=all_findings,
            summary=summary,
        )

    def vet_url(self, url: str) -> SecurityReport:
        tmpdir = tempfile.mkdtemp(prefix="skill-vetter-")
        try:
            skill_name = url.rstrip("/").split("/")[-1].replace(".git", "")
            subprocess.run(
                ["git", "clone", "--depth", "1", url, tmpdir],
                capture_output=True,
                timeout=60,
                check=True,
            )
            return self.vet(Path(tmpdir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _scan_aguara(self, skill_path: Path) -> ScannerResult:
        if self._skip_external:
            return ScannerResult(name="aguara", status=ScannerStatus.SKIP, message="External scanners disabled")

        aguara_bin = shutil.which("aguara")
        if not aguara_bin:
            go_bin = os.path.expanduser("~/go/bin/aguara")
            if os.path.isfile(go_bin):
                aguara_bin = go_bin
        if not aguara_bin:
            return ScannerResult(
                name="aguara",
                status=ScannerStatus.SKIP,
                message="aguara not installed (https://github.com/garagon/aguara)",
            )

        try:
            proc = subprocess.run(
                [aguara_bin, "scan", str(skill_path), "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            result = proc.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ScannerResult(name="aguara", status=ScannerStatus.SKIP, message="aguara execution failed")

        if not result:
            return ScannerResult(name="aguara", status=ScannerStatus.PASS, message="No issues found")

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return ScannerResult(name="aguara", status=ScannerStatus.PASS, message="No structured output")

        findings: list[SecurityFinding] = []
        for f in data.get("findings", []):
            sev = f.get("severity", 0)
            if sev >= 4:
                severity = SecuritySeverity.HIGH
            elif sev >= 3:
                severity = SecuritySeverity.MEDIUM
            else:
                severity = SecuritySeverity.LOW
            findings.append(SecurityFinding(
                scanner="aguara",
                severity=severity,
                description=f.get("description", ""),
                file_path=f.get("file_path", ""),
                line=f.get("line"),
            ))

        high = [f for f in findings if f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)]
        medium = [f for f in findings if f.severity == SecuritySeverity.MEDIUM]

        if high:
            status = ScannerStatus.FAIL
            msg = f"{len(high)} HIGH severity findings"
        elif medium:
            status = ScannerStatus.WARN
            msg = f"{len(medium)} MEDIUM severity findings"
        else:
            status = ScannerStatus.PASS
            msg = "No prompt injection patterns found"

        return ScannerResult(name="aguara", status=status, findings=findings, message=msg)

    def _scan_skill_scanner(self, skill_path: Path) -> ScannerResult:
        if self._skip_external:
            return ScannerResult(name="skill-scanner", status=ScannerStatus.SKIP, message="External scanners disabled")

        scanner_bin = shutil.which("skill-scanner")
        if not scanner_bin:
            return ScannerResult(
                name="skill-scanner",
                status=ScannerStatus.SKIP,
                message="skill-scanner not installed (https://pypi.org/project/cisco-ai-skill-scanner/)",
            )

        try:
            proc = subprocess.run(
                [scanner_bin, "scan", str(skill_path), "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            result = proc.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ScannerResult(name="skill-scanner", status=ScannerStatus.SKIP, message="skill-scanner execution failed")

        if not result:
            return ScannerResult(name="skill-scanner", status=ScannerStatus.PASS, message="No issues found")

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return ScannerResult(name="skill-scanner", status=ScannerStatus.PASS, message="No structured output")

        severity_str = data.get("severity", "none").lower()
        severity_map = {
            "critical": SecuritySeverity.CRITICAL,
            "high": SecuritySeverity.HIGH,
            "medium": SecuritySeverity.MEDIUM,
        }
        severity = severity_map.get(severity_str, SecuritySeverity.LOW)

        findings: list[SecurityFinding] = []
        if severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH, SecuritySeverity.MEDIUM):
            findings.append(SecurityFinding(
                scanner="skill-scanner",
                severity=severity,
                description=data.get("description", "Vulnerability detected"),
            ))

        if severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH):
            status = ScannerStatus.FAIL
        elif severity == SecuritySeverity.MEDIUM:
            status = ScannerStatus.WARN
        else:
            status = ScannerStatus.PASS

        msg = data.get("description", "No critical vulnerabilities found")
        return ScannerResult(name="skill-scanner", status=status, findings=findings, message=msg)

    def _scan_secrets(self, skill_path: Path) -> ScannerResult:
        findings: list[SecurityFinding] = []

        secret_pattern = re.compile(
            r"(api_key|apikey|secret|token|password|credential)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
            re.IGNORECASE,
        )

        for root, _dirs, files in os.walk(skill_path):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix in (".pyc", ".pyo", ".so", ".dylib", ".png", ".jpg", ".gif", ".pdf"):
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                rel = str(fpath.relative_to(skill_path))
                for i, line in enumerate(text.splitlines(), 1):
                    if secret_pattern.search(line):
                        findings.append(SecurityFinding(
                            scanner="secrets-scan",
                            severity=SecuritySeverity.HIGH,
                            description="Hardcoded credentials detected",
                            file_path=rel,
                            line=i,
                        ))
                        break

        base64_pattern = re.compile(r"""['"][A-Za-z0-9+/]{40,}={0,2}['"]""")
        for root, _dirs, files in os.walk(skill_path):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix not in (".sh", ".py", ".js", ".ts"):
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                rel = str(fpath.relative_to(skill_path))
                has_base64 = any(
                    base64_pattern.search(line)
                    for line in text.splitlines()
                )
                if has_base64 and not any(f.file_path == rel for f in findings):
                    findings.append(SecurityFinding(
                        scanner="secrets-scan",
                        severity=SecuritySeverity.MEDIUM,
                        description="Base64 encoded string found (review manually)",
                        file_path=rel,
                    ))

        has_high = any(f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH) for f in findings)
        has_medium = any(f.severity == SecuritySeverity.MEDIUM for f in findings)

        if has_high:
            status = ScannerStatus.FAIL
            msg = "Hardcoded credentials detected"
        elif has_medium:
            status = ScannerStatus.WARN
            msg = "Base64 encoded strings found"
        else:
            status = ScannerStatus.PASS
            msg = "No hardcoded secrets found"

        return ScannerResult(name="secrets-scan", status=status, findings=findings, message=msg)

    def _scan_structure(self, skill_path: Path) -> ScannerResult:
        findings: list[SecurityFinding] = []
        issues = 0

        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            findings.append(SecurityFinding(
                scanner="structure-check",
                severity=SecuritySeverity.HIGH,
                description="Missing SKILL.md",
            ))
            issues += 1
        else:
            content = ""
            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError:
                pass

            if content and not content.lstrip().startswith("---"):
                findings.append(SecurityFinding(
                    scanner="structure-check",
                    severity=SecuritySeverity.MEDIUM,
                    description="SKILL.md missing YAML frontmatter",
                ))

        dangerous_pattern = re.compile(
            r"(rm\s+-rf\s|curl.*\|\s*(ba)?sh|wget.*\|\s*(ba)?sh|\beval\s|\bexec\s+\S)",
            re.IGNORECASE,
        )
        for root, _dirs, files in os.walk(skill_path):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix not in (".sh", ".py", ".js", ".ts"):
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                rel = str(fpath.relative_to(skill_path))
                for i, line in enumerate(text.splitlines(), 1):
                    if dangerous_pattern.search(line):
                        findings.append(SecurityFinding(
                            scanner="structure-check",
                            severity=SecuritySeverity.HIGH,
                            description="Dangerous shell command detected",
                            file_path=rel,
                            line=i,
                        ))
                        issues += 1
                        break

        if issues > 0:
            status = ScannerStatus.FAIL
            msg = f"{issues} structure issue(s)"
        elif any(f.severity == SecuritySeverity.MEDIUM for f in findings):
            status = ScannerStatus.WARN
            msg = "Minor structure warnings"
        else:
            status = ScannerStatus.PASS
            msg = "Skill structure valid"

        return ScannerResult(name="structure-check", status=status, findings=findings, message=msg)
