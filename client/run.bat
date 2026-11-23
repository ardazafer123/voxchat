@echo off
rem Starts VoxChat on Windows. Double-click this file, or run it from a terminal.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo VoxChat is not installed yet. In this folder, run:
  echo   uv venv --python 3.12
  echo   uv pip install --require-hashes -r requirements.lock
  pause
  exit /b 1
)
".venv\Scripts\python.exe" desktop.py %*
if errorlevel 1 pause
