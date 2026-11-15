@echo off
rem Run every test (pytest). Double-click to run.
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pytest %*
set RC=%ERRORLEVEL%
if %RC%==0 (echo ALL PASS) else (echo SOME TESTS FAILED)
if not defined CI pause
exit /b %RC%
