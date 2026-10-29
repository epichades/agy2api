@echo off
cd /d "%~dp0"
rem Start the current checkout; update separately to preserve local changes.
uv sync --locked --no-dev
if errorlevel 1 exit /b %errorlevel%
uv run --no-sync python web.py
pause
