@echo off
cd /d "%~dp0"
title MediaGrabber - Environment Check
".venv\Scripts\python.exe" tools\env_check.py
echo.
pause
