@echo off
rem Install this folder as the Copilot plugin context-foundation (skill and hook) through a local marketplace.
rem GitHub Copilot CLI and the desktop app share the setting. Add --mcp to also register the MCP server.
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
