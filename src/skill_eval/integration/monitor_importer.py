"""Monitor importer — imports skill-monitor JSONL data into benchmark."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skill_eval.models import BenchmarkData


class MonitorImporter:
    def import_jsonl(self, jsonl_path: Path, benchmark: BenchmarkData, tolerance_hours: int = 24) -> BenchmarkData:
        if not jsonl_path.exists():
            return benchmark

        records = self._load_jsonl(jsonl_path)
        matching = self._match_records(records, benchmark.metadata.skill_name, tolerance_hours)

        if not matching:
            return benchmark

        for run in benchmark.runs:
            if run.configuration != "with_skill":
                continue
            matching_record = self._find_closest(matching, run)
            if matching_record:
                notes = list(run.notes)
                duration = matching_record.get("duration", 0)
                tokens = matching_record.get("tokens", {})
                input_t = tokens.get("input", 0)
                output_t = tokens.get("output", 0)
                notes.append(f"[monitor] runtime: {duration}ms, tokens: {input_t}in/{output_t}out")
                run.notes = notes

        return benchmark

    def _load_jsonl(self, path: Path) -> list[dict]:
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _match_records(self, records: list[dict], skill_name: str, tolerance_hours: int) -> list[dict]:
        matching = []
        for r in records:
            if r.get("skill_name") != skill_name:
                continue
            matching.append(r)
        return matching

    def _find_closest(self, records: list[dict], run) -> dict | None:
        if not records:
            return None
        return records.pop(0) if records else None
