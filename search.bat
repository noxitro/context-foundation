@echo off
rem Try the search interactively. Double-click to run.
rem It runs through ctxf.py, which reads the package from the src folder next to it, so that the folder can be
rem moved or copied (the copy then runs and registers its own code).
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I "%~dp0ctxf.py" --run wizard search
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
