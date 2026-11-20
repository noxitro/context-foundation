@echo off
rem First-time setup: create the virtual environment (.venv), install the dependencies,
rem and offer to download the embedding model. Double-click to run.
rem Usage: setup.bat           (asks before downloading the model)
rem Supported: Python 3.11 to 3.14 (3.15 and later have no onnxruntime build yet). The py launcher picks the
rem newest Python by default, so ask it for a supported one, newest first.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && for %%v in (3.14 3.13 3.12 3.11) do (
  if not defined PY py -%%v -c "pass" >nul 2>&1 && set "PY=py -%%v"
)
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
  echo Python was not found. Install Python 3.14 ^(3.11 to 3.14 work^) from https://www.python.org/downloads/
  echo and run this file again.
  if "%~1"=="" pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)"
if errorlevel 1 (
  echo Python 3.11 to 3.14 is required ^(3.15 and later have no onnxruntime build yet^). Found:
  %PY% --version
  echo Install Python 3.14 from https://www.python.org/downloads/ and run this file again.
  if "%~1"=="" pause
  exit /b 1
)
rem A .venv whose Python was removed or replaced (or one made by an unsupported Python) is made again.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import pip, sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1 || (
    echo == The virtual environment .venv is broken or uses an unsupported Python; recreating it
    %PY% -m venv --clear .venv || goto :failed
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo == Creating the virtual environment: .venv
  %PY% -m venv .venv || goto :failed
)
echo == Installing the dependencies (this downloads about 200 MB from PyPI the first time)
call :install || goto :failed
rem pip sees only the installed metadata, so it does not reinstall packages that can no longer be loaded
rem (the Python of .venv was replaced in the same folder by another version, or a package is damaged).
rem Make .venv again, once.
call :importable || (
  echo == The installed packages cannot be loaded ^(the Python of .venv was probably replaced^); recreating .venv
  %PY% -m venv --clear .venv || goto :failed
  call :install || goto :failed
  call :importable || (
    echo The packages were installed, but they cannot be loaded. Delete the folder .venv and run this file again;
    echo if that does not help, reinstall Python.
    goto :failed
  )
)
".venv\Scripts\python.exe" -I "%~dp0ctxf.py" --run wizard setup || goto :failed
rem The Copilot plugin starts its MCP server and hook with the command `python`, not with py.
rem The python.org installer does not add `python` to PATH by default; then `python` is the Microsoft Store
rem shortcut (WindowsApps\python.exe), which does not start Python. Say so plainly: the plugin does not work then.
python -c "pass" >nul 2>&1 || (
  echo.
  echo ======================================================================================
  echo WARNING: the command `python` does not start Python here, so the Copilot plugin will not work.
  echo The plugin ^(register-copilot.bat^) starts its search tool and session hook with `python`.
  echo The .bat files in this folder work as they are. To fix the plugin, do one of these, then open a new
  echo command prompt and check that `python --version` prints a version:
  echo   - Run the python.org installer again, choose Modify, and turn on "Add Python to environment variables".
  echo   - If `where python` shows ...\WindowsApps\python.exe first, that is the Microsoft Store shortcut, not
  echo     Python: turn off python.exe under Settings ^> Apps ^> Advanced app settings ^> App execution aliases.
  echo ======================================================================================
)
if "%~1"=="" pause
exit /b 0
:install
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e ".[dev]" || exit /b 1
exit /b 0
:importable
".venv\Scripts\python.exe" -I -c "import numpy, onnxruntime, tokenizers, tree_sitter, jsonschema" >nul 2>&1
exit /b
:failed
echo Setup failed. See the messages above. What they usually mean:
echo   "No matching distribution": no build of a package for this Python or this PC, or PyPI could not be reached.
echo   "No such file or directory" or "Long Path": the path of this folder is too long; move it to a shorter one.
echo   connection, proxy or SSL errors: check the network or the proxy settings (HTTPS_PROXY).
if "%~1"=="" pause
exit /b 1
