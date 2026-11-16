@echo off
rem First-time setup: create the virtual environment (.venv), install the dependencies,
rem and offer to download the embedding model. Double-click to run.
rem Usage: setup.bat           (asks before downloading the model)
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
  echo Python was not found. Install Python 3.11 or later from https://www.python.org/downloads/
  echo and run this file again.
  if "%~1"=="" pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or later is required.
  %PY% --version
  if "%~1"=="" pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo == Creating the virtual environment: .venv
  %PY% -m venv .venv || goto :failed
)
echo == Installing the dependencies (this downloads about 200 MB from PyPI the first time)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip || goto :failed
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e ".[dev]" || goto :failed
".venv\Scripts\python.exe" -I -m context_foundation wizard setup
if "%~1"=="" pause
exit /b 0
:failed
echo Setup failed. See the messages above.
if "%~1"=="" pause
exit /b 1
