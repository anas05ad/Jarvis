@echo off
title JARVIS
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (set "PY=venv\Scripts\python.exe") else (set "PY=python")
"%PY%" jarvis_live.py
if errorlevel 1 (
  echo.
  echo JARVIS stopped with an error. Copy the message above.
  pause
)
