#!/usr/bin/env bash
set -euo pipefail

echo "=== Skill-Eval Install ==="

# --- 1. Install skill-eval CLI ---
echo ""
echo "[1/3] Installing skill-eval..."

# Clone if not already in the project
if [ ! -f pyproject.toml ]; then
    echo "  Cloning skill-eval..."
    git clone https://github.com/fuxi-ailabs/skill-eval.git /tmp/skill-eval
    cd /tmp/skill-eval
fi

ROOT="$(pwd)"

# 选解释器:优先复用 repo 内已有的 .venv;否则用系统 python。
# 系统 python 若 externally-managed(Debian/Ubuntu/WSL2 的 PEP 668,禁止裸 pip)→
# 自动建隔离 .venv,避免污染系统 Python。mac / python.org / conda 等无此标记则直接系统装。
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
    echo "  Reusing existing venv: $PY"
else
    SYS_PY="${PYTHON:-$(command -v python3 || command -v python)}"
    # PEP 668 检测:标记文件在 stdlib 目录里(Debian/Ubuntu/WSL2: /usr/lib/python3.x/EXTERNALLY-MANAGED)
    if "$SYS_PY" -c 'import sysconfig,os,sys;s=sysconfig.get_paths();p=os.path.join(s["stdlib"],"EXTERNALLY-MANAGED");sys.exit(0 if os.path.exists(p) else 1)' 2>/dev/null; then
        echo "  System Python is externally-managed (PEP 668); creating isolated venv..."
        "$SYS_PY" -m venv "$ROOT/.venv"
        PY="$ROOT/.venv/bin/python"
        "$PY" -m pip install -q --upgrade pip
    else
        PY="$SYS_PY"
    fi
fi

"$PY" -m pip install -e . -q

# 让 `skill-eval` 裸名可调:软链 entry point 到 ~/.local/bin(终端 + agent 都能直接敲)
LOCAL_BIN="${HOME}/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$(dirname "$PY")/skill-eval" "$LOCAL_BIN/skill-eval"
case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) echo "  NOTE: $LOCAL_BIN 不在 PATH,请加:export PATH=\"$LOCAL_BIN:\$PATH\"" ;;
esac
SE_VER="$("$LOCAL_BIN/skill-eval" --version)"
echo "  skill-eval $SE_VER installed (→ $LOCAL_BIN/skill-eval)"

# --- 2. Install skill-eval-guide for Claude Code ---
echo ""
echo "[2/3] Installing skill-eval-guide..."

SKILL_SRC="$(dirname "$0")/tools/skills/skill-eval-guide"

# Claude Code
CC_DIR="${CLAUDE_CODE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$CC_DIR/skill-eval-guide"
cp "$SKILL_SRC/SKILL.md" "$CC_DIR/skill-eval-guide/SKILL.md"
echo "  Claude Code:  $CC_DIR/skill-eval-guide/"

# opencode
OC_DIR="${OPENCODE_SKILLS_DIR:-$HOME/.config/opencode/skills}"
mkdir -p "$OC_DIR/skill-eval-guide"
cp "$SKILL_SRC/SKILL.md" "$OC_DIR/skill-eval-guide/SKILL.md"
echo "  opencode:     $OC_DIR/skill-eval-guide/"

# --- 3. Verify ---
echo ""
echo "[3/3] Verifying..."
echo "  CLI:     $("$LOCAL_BIN/skill-eval" --version)"
echo "  Python:  $(python3 --version 2>/dev/null || python --version 2>/dev/null || echo 'not found')"

if command -v claude &> /dev/null; then
    echo "  Claude:  $(claude --version 2>&1 | head -1)"
else
    echo "  Claude:  (not installed — only needed for dynamic eval)"
fi

echo ""
echo "=== Done ==="
echo ""
echo "Quick start:"
echo "  skill-eval vet   path/to/skill/"
echo "  skill-eval run   path/to/skill/"
echo "  skill-eval report output/iteration-1/ --open"
