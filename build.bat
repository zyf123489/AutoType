@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed.
    echo Run: "%PYTHON%" -m pip install pyinstaller
    exit /b 1
)

"%PYTHON%" -m PyInstaller --noconfirm --clean -w -F main.py -n AutoType
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build complete: dist\AutoType.exe
endlocal
