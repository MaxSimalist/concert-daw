@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Concert DAW - обновление
if not exist ".venv\Scripts\python.exe" (
    echo Программа ещё не установлена - сначала запустите install.bat
    pause
    exit /b 1
)
echo ============================================================
echo   Concert DAW - обновление из GitHub
echo   Перед обновлением закройте программу. Настройки сохранятся.
echo ============================================================
echo.
".venv\Scripts\python.exe" updater.py --update
echo.
pause
