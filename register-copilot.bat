@echo off
rem Register the context-search skill with GitHub Copilot CLI (the desktop app uses the same settings). Add --mcp to also register the MCP server.
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -m context_foundation copilot register %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
