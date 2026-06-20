@echo off
REM Debug launcher: keeps the console open so import errors, SystemExit
REM messages, or tracebacks are visible instead of failing silently
REM (which is what happens if you just double-click the .pyw file).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found or incomplete at "%~dp0.venv".
    echo Create it with:  python -m venv .venv
    echo Then install deps:  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
echo Running with: %~dp0.venv\Scripts\python.exe
echo ----------------------------------------------------------------
".venv\Scripts\python.exe" "atc_assistant.pyw"
echo ----------------------------------------------------------------
echo App closed (exit code %ERRORLEVEL%). If it closed immediately, the
echo error message is printed above this line.
pause
