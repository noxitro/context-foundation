@echo off
rem Uninstall the Copilot plugin context-foundation and the local marketplace (and the MCP server, if registered).
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -m context_foundation copilot unregister %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
