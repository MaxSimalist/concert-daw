@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Программа ещё не установлена - запускаю install.bat
    call "%~dp0install.bat"
    exit /b
)
start "" "%~dp0.venv\Scripts\pythonw.exe" main.py
