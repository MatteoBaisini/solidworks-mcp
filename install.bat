@echo off
rem Creates a local environment (.venv) and installs the server in it.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: could not create the virtual environment. Is Python 3.11+ installed?
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
    echo.
    echo ERROR during installation.
    pause
    exit /b 1
)
echo.
echo Installed. Server command: %~dp0.venv\Scripts\solidworks-mcp.exe
pause
