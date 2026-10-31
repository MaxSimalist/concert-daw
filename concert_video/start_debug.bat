@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Concert Video - запуск с выводом ошибок
if not exist ".venv\Scripts\python.exe" (
    echo Программа ещё не установлена - сначала запустите install.bat
    pause
    exit /b 1
)
echo Запуск с выводом ошибок. Если программа упадёт - текст ошибки останется в этом окне.
echo.
".venv\Scripts\python.exe" -X faulthandler main.py
echo.
echo Программа завершилась (код %errorlevel%).
pause
