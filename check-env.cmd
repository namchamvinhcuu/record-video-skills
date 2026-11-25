@echo off
rem Run scripts\check_env.py with the first working Python 3 (py launcher, then python).
rem The Microsoft Store "python" stub exits with 9009, so the exit code decides, not "where".
setlocal
set "SCRIPT=%~dp0scripts\check_env.py"

py -3 --version >nul 2>nul
if %errorlevel%==0 goto use_py

python --version >nul 2>nul
if %errorlevel%==0 goto use_python

echo MISSING python: no working Python 3.10+ on PATH
echo         fix: winget install Python.Python.3.12   (then open a NEW terminal)
exit /b 1

:use_py
py -3 "%SCRIPT%" %*
exit /b %errorlevel%

:use_python
python "%SCRIPT%" %*
exit /b %errorlevel%
