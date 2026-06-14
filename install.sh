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

pip install -e . -q

echo "  skill-eval $(skill-eval --version) installed"

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
echo "  CLI:     $(skill-eval --version)"
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
