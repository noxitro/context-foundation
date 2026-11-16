@echo off
rem Install this folder as the Copilot plugin context-foundation (MCP server, hook and skill) through a local
rem marketplace. GitHub Copilot CLI and the desktop app share the setting.
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
