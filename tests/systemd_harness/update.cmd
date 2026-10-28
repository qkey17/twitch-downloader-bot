@echo off
setlocal
cd /d "%~dp0..\.."
if errorlevel 1 exit /b 1
if /i "%~1"=="build" (set "TDB_ACTION=prepare-update") else if /i "%~1"=="run" (set "TDB_ACTION=run-update") else (
  echo Usage: tests\systemd_harness\update.cmd build ^| run
  exit /b 2
)
where git >nul 2>nul
if errorlevel 1 (echo Git not found. No changes made. & exit /b 1)
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(sys.version_info[:2] not in [(3,n) for n in range(12,100)])" >nul 2>nul
if errorlevel 1 goto use_python
py -3 -B tests\systemd_harness\host.py %TDB_ACTION%
exit /b %errorlevel%
:use_python
where python >nul 2>nul
if errorlevel 1 (echo Python 3.12+ not found. No installation attempted. & exit /b 1)
python -c "import sys; sys.exit(sys.version_info[:2] not in [(3,n) for n in range(12,100)])" >nul 2>nul
if errorlevel 1 (echo Python 3.12+ not available. No installation attempted. & exit /b 1)
python -B tests\systemd_harness\host.py %TDB_ACTION%
exit /b %errorlevel%
