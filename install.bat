@echo off
setlocal enabledelayedexpansion
echo === Sift Install ===
echo.

REM --- 1. Install sift CLI ---
echo [1/3] Installing sift...
pip install -e . -q
echo   sift installed

REM --- 2. Install sift-guide ---
echo.
echo [2/3] Installing sift-guide...

set SKILL_SRC=tools\skills\sift-guide

REM Claude Code
set CC_DIR=%USERPROFILE%\.claude\skills\sift-guide
if not exist "%CC_DIR%" mkdir "%CC_DIR%"
copy "%SKILL_SRC%\SKILL.md" "%CC_DIR%\SKILL.md" >nul
echo   Claude Code:  %CC_DIR%

REM opencode
set OC_DIR=%USERPROFILE%\.config\opencode\skills\sift-guide
if not exist "%OC_DIR%" mkdir "%OC_DIR%"
copy "%SKILL_SRC%\SKILL.md" "%OC_DIR%\SKILL.md" >nul
echo   opencode:     %OC_DIR%

REM --- 3. Verify ---
echo.
echo [3/3] Verifying...
sift --version >nul 2>&1
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
echo   sift vet   path\to\skill\
echo   sift eval  path\to\skill\
echo   sift report output\iteration-1\ --open
pause
