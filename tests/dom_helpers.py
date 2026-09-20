import subprocess
import tempfile

from sift.executables import resolve_executable


def render_inner_html(script):
    harness = (
        "var __rendered = '';\n"
        "var document = { getElementById: function () {"
        "  return { set innerHTML(v) { __rendered = v; }, get innerHTML() { return __rendered; } };"
        " } };\n" + script + "\nprocess.stdout.write(__rendered);\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        js = f.name
    return subprocess.run([resolve_executable("node"), js], capture_output=True, text=True)
