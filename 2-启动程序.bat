@echo off
cd /d "%~dp0"
title MediaGrabber - Running (do not close this window)
".venv\Scripts\python.exe" main.py
echo.
pause
