@echo off
rem Run the retrieval-only evaluation (keyword, semantic, hybrid) on a cases.jsonl. Double-click to run.
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -m context_foundation wizard eval
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
