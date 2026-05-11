"""Report generator — produces self-contained HTML from evaluation data."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import signal
import subprocess
import sys
import webbrowser
from functools import partial
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
    runs.sort(key=lambda r: (r.get("eval_id", float("inf")), r["id"]))
    return runs


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

    if len(parts) >= 2:
        config = parts[-2]
    if len(parts) >= 1:
        try:
            run_number = int(parts[-1].split("-")[1])
        except (ValueError, IndexError):
            pass
    if len(parts) >= 3:
        try:
            eval_id = int(parts[-3].split("-")[1])
        except (ValueError, IndexError):
            pass

    prompt = ""
    eval_metadata_path = run_dir.parent.parent / "eval_metadata.json"
    if eval_metadata_path.exists():
        try:
            meta = json.loads(eval_metadata_path.read_text(encoding="utf-8"))
            prompt = meta.get("prompt", "")
            eval_id = meta.get("eval_id", eval_id)
        except Exception:
            pass

    embedded_files: list[dict] = []
    for f in sorted(outputs_dir.iterdir()):
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
    prev_dir = run_dir.parent.parent / "previous" / run_dir.name
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
            return {"name": path.name, "type": "image", "content": f"data:{mime};base64,{data}"}
        except Exception:
            pass

    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"name": path.name, "type": "binary", "mime": mime, "content": data}
    except Exception:
        return {"name": path.name, "type": "error", "content": "Could not read file"}


def load_previous_iteration(workspace: Path) -> dict | None:
    for name in ("previous", "previous_iteration"):
        prev_dir = workspace / name
        if prev_dir.is_dir():
            feedback_path = prev_dir / "feedback.json"
            feedback = None
            if feedback_path.exists():
                try:
                    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            prev_runs = find_runs(prev_dir)
            return {"feedback": feedback, "runs": prev_runs}
    return None


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

        if previous is None:
            previous = load_previous_iteration(workspace)

        data = {
            "skill_name": skill_name,
            "runs": runs,
            "previous": previous,
            "benchmark": benchmark,
            "scorecard": scorecard,
        }

        json_data = json.dumps(data, ensure_ascii=False)
        html = template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {json_data};")

        return html

    def write_static(self, workspace: Path, output_path: Path, benchmark: dict | None = None, scorecard: dict | None = None) -> None:
        html = self.generate_html(workspace, benchmark, scorecard=scorecard)
        output_path.write_text(html, encoding="utf-8")

    def serve(self, workspace: Path, port: int = 8080, benchmark: dict | None = None, open_browser: bool = False) -> None:
        gen = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/feedback":
                    feedback_path = workspace / "feedback.json"
                    if feedback_path.exists():
                        data = feedback_path.read_text(encoding="utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(data.encode("utf-8"))
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b"{}")
                    return

                html = gen.generate_html(workspace, benchmark)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))

            def do_POST(self):
                if self.path == "/api/feedback":
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    feedback_path = workspace / "feedback.json"
                    feedback_path.write_bytes(body)
                    self.send_response(200)
                    self.end_headers()
                    return
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
