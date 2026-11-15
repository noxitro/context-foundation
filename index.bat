@echo off
rem Build (or rebuild) the search index. Asks for the workspace and the folders to search. Double-click to run.
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -m context_foundation wizard index
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
