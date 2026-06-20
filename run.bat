@echo off
REM Always launches with THIS project's venv interpreter, never the system-wide
REM Python — avoids the classic ".pyw file association points to the wrong
REM Python" trap (e.g. after removing global site-packages).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] .venv not found or incomplete at "%~dp0.venv".
    echo Create it with:  python -m venv .venv
    echo Then install deps:  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "atc_assistant.pyw"
