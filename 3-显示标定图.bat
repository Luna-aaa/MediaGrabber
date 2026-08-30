@echo off
cd /d "%~dp0"
title MediaGrabber - DPI Calibration Pattern
".venv\Scripts\python.exe" tools\dpi_check.py
echo.
pause
