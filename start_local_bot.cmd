@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -u run.py
pause
