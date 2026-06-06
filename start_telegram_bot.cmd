@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

echo Starting Telegram bot from %CD%
echo Stdout: %CD%\local_bot.stdout.log
echo Stderr: %CD%\local_bot.stderr.log
echo.

.venv\Scripts\python.exe -u run.py 1>>local_bot.stdout.log 2>>local_bot.stderr.log

echo.
echo Bot process stopped. Check local_bot.stderr.log if this was unexpected.
pause
