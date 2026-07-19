"""Report generator — produces self-contained HTML from evaluation data."""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import signal
import subprocess
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

MIME_OVERRIDES = {
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def find_runs(workspace: Path) -> list[dict]:
    runs: list[dict] = []
    _find_runs_recursive(workspace, workspace, runs)
    runs = _dedup_retries(runs)
    runs.sort(key=lambda r: (r.get("eval_id", float("inf")), r["id"]))
    return runs


def _dedup_retries(runs: list[dict]) -> list[dict]:
    """同 ``(config, eval_id, run_number)`` 只保留**最终 retry**。

    runner 在 UNEVAL/失败时自动重试,产出 ``run-N`` + ``run-N-retry-K``。被 retry
    取代的旧尝试若当独立 run 显示,会让用户看到已被覆盖的失败结果(如全 UNEVAL 的
    首次尝试),误读成 skill 表现差。与 benchmark/comparison 的 ``index_runs`` 同口径:
    同一逻辑 run 只取最高 retry = 最终尝试(canonical)。
    """
    from skill_eval.execution.run_paths import retry_count

    best: dict[tuple, tuple[int, dict]] = {}  # key -> (retry_count, run)
    for r in runs:
        key = (r.get("config"), r.get("eval_id"), r.get("run_number"))
        rc = retry_count(r.get("id", "").rsplit("/", 1)[-1])
        prev = best.get(key)
        if prev is None or rc > prev[0]:
            best[key] = (rc, r)
    return [v[1] for v in best.values()]


def _find_runs_recursive(root: Path, current: Path, runs: list[dict]) -> None:
    if not current.is_dir():
        return
    outputs_dir = current / "outputs"
    if outputs_dir.is_dir():
        run = _build_run(root, current)
        if run:
            runs.append(run)
        return
    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs"}
    for child in sorted(current.iterdir()):
        if child.name.startswith(".") or child.name in skip:
            continue
        _find_runs_recursive(root, child, runs)


def _build_run(root: Path, run_dir: Path) -> dict | None:
    outputs_dir = run_dir / "outputs"
    if not outputs_dir.is_dir():
        return None

    parts = run_dir.relative_to(root).parts
    config = "unknown"
    eval_id = 0
    run_number = 0

    if len(parts) >= 3:
        config = parts[0]
        try:
            eval_id = int(parts[1].split("-")[1])
        except (ValueError, IndexError):
            pass
        try:
            run_number = int(parts[-1].split("-")[1])
        except (ValueError, IndexError):
            pass
    elif len(parts) >= 2:
        config = parts[0]
        try:
            eval_id = int(parts[0].split("-")[1])
        except (ValueError, IndexError):
            pass
        try:
            run_number = int(parts[-1].split("-")[1])
        except (ValueError, IndexError):
            pass
    elif len(parts) >= 1:
        try:
            run_number = int(parts[-1].split("-")[1])
        except (ValueError, IndexError):
            pass

    prompt = ""
    eval_metadata_path = root / "eval_metadata.json"
    if eval_metadata_path.exists():
        try:
            meta = json.loads(eval_metadata_path.read_text(encoding="utf-8"))
            prompt = meta.get("prompt", "")
            eval_id = meta.get("eval_id", eval_id)
        except Exception:
            pass

    if not prompt:
        debug_path = run_dir / "run_debug.json"
        if debug_path.exists():
            try:
                debug = json.loads(debug_path.read_text(encoding="utf-8"))
                prompt = debug.get("prompt", "")
            except Exception:
                pass

    embedded_files: list[dict] = []
    for f in sorted(outputs_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.name.startswith(".") or f.name in METADATA_FILES:
            continue
        embedded_files.append(_embed_file(f))

    grading = None
    grading_path = run_dir / "grading.json"
    if grading_path.exists():
        try:
            grading = json.loads(grading_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    compliance = None
    compliance_path = run_dir / "compliance.json"
    if compliance_path.exists():
        try:
            compliance = json.loads(compliance_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    calibration = None
    calibration_path = run_dir / "calibration.json"
    if calibration_path.exists():
        try:
            calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    previous_output = None
    prev_dir = root / "previous" / str(run_dir.relative_to(root))
    if prev_dir.is_dir() and (prev_dir / "outputs").is_dir():
        prev_outputs = sorted((prev_dir / "outputs").iterdir())
        if prev_outputs:
            previous_output = _embed_file(prev_outputs[0])

    return {
        "id": str(run_dir.relative_to(root)),
        "eval_id": eval_id,
        "config": config,
        "run_number": run_number,
        "prompt": prompt,
        "outputs": embedded_files,
        "grading": grading,
        "compliance": compliance,
        "calibration": calibration,
        "previous_output": previous_output,
    }


def _embed_file(path: Path) -> dict:
    ext = path.suffix.lower()
    mime = get_mime_type(path)

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return {"name": path.name, "type": "text", "content": content}
        except Exception:
            pass

    if ext in IMAGE_EXTENSIONS:
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"name": path.name, "type": "image", "data_uri": f"data:{mime};base64,{data}"}
        except Exception:
            pass

    if ext == ".xlsx":
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"name": path.name, "type": "xlsx", "data_b64": data}
        except Exception:
            pass

    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"name": path.name, "type": "binary", "data_uri": f"data:{mime};base64,{data}"}
    except Exception:
        return {"name": path.name, "type": "error", "content": "Could not read file"}


def _load_vet_report(workspace: Path) -> dict | None:
    path = workspace / "vet-report.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _load_gate_verdict(root: Path) -> dict | None:
    # gate.json 落在 run 树根(= output_dir,gate 默认写 benchmark_path.parent),
    # report workspace 即该根(iteration-1 死层删除后无父子之分)。
    path = root / "gate.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def load_previous_iteration(workspace: Path) -> dict | None:
    for name in ("previous", "previous_iteration"):
        prev_dir = workspace / name
        if prev_dir.is_dir():
            return {"runs": find_runs(prev_dir)}
    return None


def _aggregate_compliance(runs: list[dict]) -> list[dict]:
    """把 per-run compliance.json 按 side(config)聚合成整体遵循性。

    遵循性出题依据是 SKILL(非 eval),故 N 个 transcript 各审一次 = 一次性喂给 audit 多会话审计;
    per-slot findings 合并再 aggregate_by_instruction,与一次喂 N transcript **结果等价**(聚合可交换)。
    复用 audit.aggregate_by_instruction。全空 digest 的 side(L1 理解题 cc 纯推理无工具)贡献 0 findings → 不产空聚合。
    """
    from collections import defaultdict
    from skill_eval.audit.aggregate import aggregate_by_instruction
    from skill_eval.models import AuditFinding

    by_side: dict[str, list[dict]] = defaultdict(list)   # side -> findings(dict)
    side_runs: dict[str, int] = defaultdict(int)        # side -> 贡献了非空 findings 的 run 数
    for r in runs:
        comp = r.get("compliance")
        if not comp:
            continue
        side = r.get("config") or "?"
        findings = comp.get("findings") or []
        by_side[side].extend(findings)
        if findings:  # 只数真贡献了判定的 run(空 digest 的 L1 题不计)
            side_runs[side] += 1

    out: list[dict] = []
    for side, findings in by_side.items():
        if not findings:
            continue  # 该 side 全空 digest → 跳过(不产空聚合)
        ai = aggregate_by_instruction([AuditFinding.model_validate(f) for f in findings])
        out.append({
            "side": side,
            "summary": {
                "total": len(findings),
                "pass": sum(1 for f in findings if f.get("verdict") == "PASS"),
                "fail": sum(1 for f in findings if f.get("verdict") == "FAIL"),
                "na": sum(1 for f in findings if f.get("verdict") == "N/A"),
                "unclear": sum(1 for f in findings if f.get("verdict") == "UNCLEAR"),
            },
            "by_instruction": [a.model_dump(mode="json") for a in ai],
            "runs_contributed": side_runs[side],
        })
    return out


def _load_previous_benchmark(workspace: Path) -> dict | None:
    """读 workspace/previous/benchmark.json(run 端 fresh 重跑时 MOVE 进去的旧聚合)。
    与 load_previous_iteration(读 run 树,供文件级面板)互补:这里读的是聚合数字,供「vs 上次」纵向 delta。"""
    path = workspace / "previous" / "benchmark.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _pct(rate) -> int | None:
    """率(0-1 fraction)→ 百分比整数;None 透传(指标缺失)。"""
    return None if rate is None else round(rate * 100)


def _pp(cur_rate, prev_rate) -> int | None:
    """两率之差 → 百分点(cur - prev)*100 四舍五入;任一缺失 → None。"""
    if cur_rate is None or prev_rate is None:
        return None
    return round((cur_rate - prev_rate) * 100)


def _compute_previous_delta(current: dict | None, previous: dict | None) -> dict | None:
    """current vs previous benchmark 的聚合纵向 delta(Task 3「vs 上次」卡的数据)。

    预算成百分比/百分点(模板直接插,免 JS 算术)。双率(客观 programmatic / 主观 llm,
    与 dashboard Δ 卡同源)+ 盲评胜率 + 样本数(不同样本数时模板显式标出,提醒可比性)。
    None / 缺字段 → 该指标省略(不报假 0)。无 run_summary 可比 → 整体返 None(模板不显卡)。"""
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return None
    cur_summary = current.get("run_summary") or {}
    prev_summary = previous.get("run_summary") or {}
    if not cur_summary or not prev_summary:
        return None

    def _rates(summary, side):
        s = summary.get(side) or {}
        prog = s.get("pass_rate_programmatic") or {}
        llm = s.get("pass_rate_llm") or {}
        return prog.get("mean"), llm.get("mean")

    sides = []
    for side in [k for k in cur_summary if k != "delta"]:
        cur_prog, cur_llm = _rates(cur_summary, side)
        prev_prog, prev_llm = _rates(prev_summary, side) if side in prev_summary else (None, None)
        sides.append({
            "label": side,
            "cur_prog": _pct(cur_prog), "prev_prog": _pct(prev_prog), "d_prog": _pp(cur_prog, prev_prog),
            "cur_llm": _pct(cur_llm), "prev_llm": _pct(prev_llm), "d_llm": _pp(cur_llm, prev_llm),
        })

    cur_wr = (current.get("comparison_summary") or {}).get("win_rate")
    prev_wr = (previous.get("comparison_summary") or {}).get("win_rate")

    return {
        "sides": sides,
        "cur_win": _pct(cur_wr), "prev_win": _pct(prev_wr), "d_win": _pp(cur_wr, prev_wr),
        "cur_runs": (current.get("metadata") or {}).get("runs_per_configuration"),
        "prev_runs": (previous.get("metadata") or {}).get("runs_per_configuration"),
    }


def _normalize_benchmark_expectations(bench: dict | None) -> dict | None:
    """viewer 读的是已存的 benchmark.json:comparator 历史版本(或当次 LLM)声明的
    expectation_results 计数可能与它逐条 details 不一致(一次性产 JSON,数不对自己的列表),
    原样透传会让顶部 passed/total 与下方逐条 ✓/✗ 对不上。生成 report 前按 details 重算,
    与 comparator._recompute_expectation_counts 同一规则,覆盖 benchmark + previous 的所有
    comparisons,使显示永远自洽。返回 deepcopy,不动调用方的 dict。
    """
    if not bench or not isinstance(bench, dict):
        return bench
    bench = copy.deepcopy(bench)
    for comp in bench.get("comparisons") or []:
        er = comp.get("expectation_results") if isinstance(comp, dict) else None
        if not isinstance(er, dict):
            continue
        for v in er.values():
            if not isinstance(v, dict):
                continue
            details = v.get("details") or []
            if not details:
                continue
            passed = sum(1 for d in details if isinstance(d, dict) and d.get("passed"))
            total = len(details)
            v["passed"] = passed
            v["total"] = total
            v["pass_rate"] = passed / total if total else 0.0
    return bench


class ReportGenerator:
    def __init__(self):
        self.template_dir = Path(__file__).parent / "templates"

    def generate_html(
        self,
        workspace: Path,
        benchmark: dict | None = None,
        previous: dict | None = None,
        scorecard: dict | None = None,
    ) -> str:
        template_path = self.template_dir / "viewer.html"
        if not template_path.exists():
            return f"<html><body><h1>Error: viewer.html not found</h1></body></html>"

        template = template_path.read_text(encoding="utf-8")

        runs = find_runs(workspace)
        skill_name = workspace.name

        if benchmark and isinstance(benchmark, dict):
            meta = benchmark.get("metadata", {})
            if meta.get("skill_name"):
                skill_name = meta["skill_name"]

        # viewer 原样嵌入 benchmark/previous 的 comparisons.expectation_results;早期数据里
        # LLM 声明的 passed/total 与逐条 details 不一致,这里按 details 重算(与 comparator 同规则)。
        benchmark = _normalize_benchmark_expectations(benchmark)

        if previous is None:
            previous = load_previous_iteration(workspace)
        previous = _normalize_benchmark_expectations(previous)

        prev_outputs: dict = {}
        if previous:
            for prev_run in previous.get("runs", []):
                rid = prev_run.get("id", "")
                po = prev_run.get("outputs")
                if po:
                    prev_outputs[rid] = po

        data = {
            "skill_name": skill_name,
            "runs": runs,
            "compliance_aggregate": _aggregate_compliance(runs),
            "previous": previous,
            "previous_outputs": prev_outputs,
            "previous_delta": _compute_previous_delta(benchmark, _load_previous_benchmark(workspace)),
            "benchmark": benchmark,
            "scorecard": scorecard,
            "vet_report": _load_vet_report(workspace),
            "gate": _load_gate_verdict(workspace),
        }

        json_data = json.dumps(data, ensure_ascii=False)
        html = template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {json_data};")

        return html

    def write_static(self, workspace: Path, output_path: Path, benchmark: dict | None = None, previous: dict | None = None, scorecard: dict | None = None) -> None:
        html = self.generate_html(workspace, benchmark, previous=previous, scorecard=scorecard)
        output_path.write_text(html, encoding="utf-8")

    def serve(self, workspace: Path, port: int = 8080, benchmark: dict | None = None, open_browser: bool = False) -> None:
        gen = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                html = gen.generate_html(workspace, benchmark)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))

            def do_POST(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        _kill_port(port)
        server = HTTPServer(("", port), Handler)
        url = f"http://localhost:{port}"
        print(f"Serving report at {url}")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()


def _kill_port(port: int) -> None:
    if sys.platform == "win32":
        return
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            for pid in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
    except Exception:
        pass
