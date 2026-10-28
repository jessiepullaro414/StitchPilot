@echo off
rem Starts StitchPilot. First run sets up its Python environment (a few minutes).
rem The environment lives in a short path because Windows can't load some libraries from deep folders.
setlocal
set "VENV=%LOCALAPPDATA%\StitchPilot\venv"
if not exist "%VENV%\Scripts\pythonw.exe" (
    echo Setting up StitchPilot for the first time...
    python -m venv "%VENV%" || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
    "%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" || (pause & exit /b 1)
)
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0app.py" %*
