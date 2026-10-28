@echo off
rem Starts the StitchPilot web app at http://localhost:8000 (uses the same Python environment as StitchPilot.bat).
rem To let other devices on your network use it: set STITCHPILOT_HOST=0.0.0.0 before running.
setlocal
set "VENV=%LOCALAPPDATA%\StitchPilot\venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo Setting up StitchPilot for the first time...
    python -m venv "%VENV%" || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
)
"%VENV%\Scripts\python.exe" -c "import starlette, uvicorn, multipart" 2>nul || (
    "%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements-web.txt" || (pause & exit /b 1)
)
cd /d "%~dp0"
start "" http://localhost:8000
"%VENV%\Scripts\python.exe" -m stitchpilot.web
