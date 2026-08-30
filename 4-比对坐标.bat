@echo off
cd /d "%~dp0"
title MediaGrabber - DPI Verify
".venv\Scripts\python.exe" tools\dpi_check.py --verify
echo.
pause
