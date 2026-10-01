@echo off
rem Skyrim Alchemy Companion - Windows launcher.
rem Finds Python (py, then python, then python3), starts the companion,
rem and keeps the window open so you can read the phone address.
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py skyrim_alchemy.py
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python skyrim_alchemy.py
    goto :done
)

where python3 >nul 2>nul
if %errorlevel%==0 (
    python3 skyrim_alchemy.py
    goto :done
)

echo.
echo   Could not find Python 3.
echo   Install it from https://www.python.org/downloads/
echo   (check "Add python.exe to PATH" during installation),
echo   then double-click run.bat again.
echo.
pause
exit /b 1

:done
echo.
echo   The companion has stopped. You can close this window.
pause
