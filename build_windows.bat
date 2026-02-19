@echo off
:: ============================================================
:: build_windows.bat
:: Builds the iracing-laps.exe standalone executable for Windows
::
:: Requirements:
::   - Python 3.10 or higher installed and in PATH
::   - Internet connection to download dependencies
::
:: Usage:
::   Double-click this file or run it from CMD
:: ============================================================

echo.
echo =============================================
echo  iRacing Laps - Executable builder
echo =============================================
echo.

:: Install dependencies
echo [1/3] Installing dependencies...
pip install pyirsdk pyyaml customtkinter matplotlib pyinstaller --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies. Check your internet connection and that Python is in PATH.
    pause
    exit /b 1
)

echo [2/3] Building executable...
pyinstaller --onefile --name iracing-laps --clean iracing-laps.py
if errorlevel 1 (
    echo ERROR: Failed to build the executable.
    pause
    exit /b 1
)

echo [3/3] Cleaning up temporary build files...
rmdir /s /q build 2>nul
del iracing-laps.spec 2>nul

echo.
echo =============================================
echo  Done! Executable generated at:
echo  dist\iracing-laps.exe
echo =============================================
echo.
echo Basic usage:
echo   dist\iracing-laps.exe --only-complete --output-dir results
echo.
pause
