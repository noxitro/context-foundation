@echo off
rem Install this folder as the Copilot plugin context-foundation (MCP server, hook and skill) through a local
rem marketplace. GitHub Copilot CLI and the desktop app share the setting.
rem It runs through ctxf.py, which reads the package from the src folder next to it, so that a moved or copied
rem folder registers itself (not the folder that setup.bat ran in first).
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I "%~dp0ctxf.py" --run copilot register %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
