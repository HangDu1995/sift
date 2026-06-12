@echo off
setlocal enabledelayedexpansion
echo === Skill-Eval Install ===
echo.

REM --- 1. Install skill-eval CLI ---
echo [1/3] Installing skill-eval...
pip install -e . -q
echo   skill-eval installed

REM --- 2. Install skill-eval-guide ---
echo.
echo [2/3] Installing skill-eval-guide...

set SKILL_SRC=tools\skills\skill-eval-guide

REM Claude Code
set CC_DIR=%USERPROFILE%\.claude\skills\skill-eval-guide
if not exist "%CC_DIR%" mkdir "%CC_DIR%"
copy "%SKILL_SRC%\SKILL.md" "%CC_DIR%\SKILL.md" >nul
echo   Claude Code:  %CC_DIR%

REM opencode
set OC_DIR=%USERPROFILE%\.config\opencode\skills\skill-eval-guide
if not exist "%OC_DIR%" mkdir "%OC_DIR%"
copy "%SKILL_SRC%\SKILL.md" "%OC_DIR%\SKILL.md" >nul
echo   opencode:     %OC_DIR%

REM --- 3. Verify ---
echo.
echo [3/3] Verifying...
skill-eval --version >nul 2>&1
if !errorlevel! equ 0 (
    echo   CLI:     OK
) else (
    echo   CLI:     FAILED
)
python3 --version >nul 2>&1 || python --version >nul 2>&1
where claude >nul 2>&1 && echo   Claude:  available || echo   Claude:  (not installed)

echo.
echo === Done ===
echo.
echo Quick start:
echo   skill-eval vet   path\to\skill\
echo   skill-eval run   path\to\skill\
echo   skill-eval report output\iteration-1\ --open
pause
