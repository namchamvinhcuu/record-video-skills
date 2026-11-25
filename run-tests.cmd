@echo off
rem Run the per-file test runner with the Python launcher (falls back to python).
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0run_tests.py" %*
) else (
  python "%~dp0run_tests.py" %*
)
exit /b %errorlevel%
