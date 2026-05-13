"""Source project analyzer — extracts characteristics for diverse eval generation."""

from __future__ import annotations

import os
from pathlib import Path

from skill_eval.models import SourceProfile

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", "vendor", ".mypy_cache",
    ".pytest_cache", ".tox", ".eggs",
}
IGNORE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe",
    ".lock", ".sum", ".map",
}
SAMPLE_FILE_LIMIT = 15
MAX_FILE_BYTES = 50_000
MAX_SAMPLE_LINES = 200
MAX_SAMPLE_BYTES = 8_000
MAX_KEYWORDS = 20


class SourceAnalyzer:
    def __init__(self, exclude_dirs: list[str] | None = None):
        self._exclude_dirs = IGNORE_DIRS | (set(exclude_dirs) if exclude_dirs else set())

    def analyze(self, source_dir: Path, skill_hints: list[str] | None = None) -> SourceProfile:
        ext_counts: dict[str, int] = {}
        dir_entries: list[str] = []
        candidates: list[tuple[Path, int]] = []
        keywords: set[str] = set()
        skill_hints = skill_hints or []

        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in self._exclude_dirs and not d.startswith(".")]
            rel_root = Path(root).relative_to(source_dir)

            if len(dir_entries) < 30:
                file_count = len([f for f in files if not self._is_ignored(f)])
                prefix = "  " * (len(rel_root.parts))
                dir_entries.append(f"{prefix}{rel_root.name or '.'}{'/' if rel_root.name else ''} ({file_count} files)")

            for fname in files:
                if self._is_ignored(fname):
                    continue

                fpath = Path(root) / fname
                ext = fpath.suffix.lower()
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

                try:
                    fsize = fpath.stat().st_size
                except OSError:
                    continue

                for hint in skill_hints:
                    if hint.lower() in fname.lower() or hint.lower() in rel_root.as_posix().lower():
                        keywords.add(hint)

                self._extract_keywords(fpath, fname, keywords)
                candidates.append((fpath, fsize))

        samples = self._pick_samples(candidates)

        summary = self._build_summary(ext_counts, dir_entries, len(candidates), keywords)

        return SourceProfile(
            project_path=str(source_dir),
            file_type_distribution=ext_counts,
            directory_structure_summary="\n".join(dir_entries),
            domain_keywords=sorted(keywords)[:MAX_KEYWORDS],
            sample_files=samples,
            project_summary=summary,
        )

    def _is_ignored(self, fname: str) -> bool:
        ext = Path(fname).suffix.lower()
        return ext in IGNORE_EXTENSIONS

    def _pick_samples(self, candidates: list[tuple[Path, int]]) -> list[dict]:
        if not candidates:
            return []

        selected: list[dict] = []
        seen_dirs: dict[str, int] = {}

        sorted_by_size = sorted(candidates, key=lambda x: x[1])
        per_dir_limit = 2

        for fpath, fsize in sorted_by_size:
            if len(selected) >= SAMPLE_FILE_LIMIT:
                break
            parent = str(fpath.parent)
            if seen_dirs.get(parent, 0) >= per_dir_limit:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if len(content) > MAX_FILE_BYTES:
                lines = content.splitlines()
                content = "\n".join(lines[:MAX_SAMPLE_LINES])
                if len(content) > MAX_SAMPLE_BYTES:
                    content = content[:MAX_SAMPLE_BYTES] + "\n..."
            elif len(content) > MAX_SAMPLE_BYTES:
                content = content[:MAX_SAMPLE_BYTES] + "\n..."
            rel = fpath.resolve().relative_to(Path.cwd()) if fpath.is_relative_to(Path.cwd()) else fpath
            selected.append({
                "path": str(rel),
                "size": fsize,
                "content": content,
            })
            seen_dirs[parent] = seen_dirs.get(parent, 0) + 1

        return selected

    def _extract_keywords(self, fpath: Path, fname: str, keywords: set[str]) -> None:
        name_lower = fname.lower()
        for kw in ["server", "client", "handler", "controller", "service", "model",
                    "util", "helper", "config", "test", "spec", "api", "router",
                    "middleware", "database", "migration", "schema", "component",
                    "module", "plugin", "worker", "queue", "job", "pipeline",
                    "transform", "convert", "validate", "parse", "render",
                    "docker", "k8s", "ci", "cd", "deploy"]:
            if kw in name_lower and kw not in keywords and len(keywords) < MAX_KEYWORDS:
                keywords.add(kw)

    def _build_summary(
        self,
        ext_counts: dict[str, int],
        dir_entries: list[str],
        total_files: int,
        keywords: set[str],
    ) -> str:
        parts = []
        parts.append(f"Total files: {total_files}")
        top_exts = sorted(ext_counts.items(), key=lambda x: -x[1])[:8]
        if top_exts:
            parts.append("Top extensions: " + ", ".join(
                f"{ext}({cnt})" for ext, cnt in top_exts
            ))
        if keywords:
            parts.append("Domain keywords: " + ", ".join(sorted(keywords)[:15]))
        return "; ".join(parts)
