@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Concert Stage - удаление

echo ============================================================
echo   Concert Stage - удаление с этого компьютера
echo   Папка программы: %CD%
echo ============================================================
echo.
echo   Будет удалено: окружение Python программы (.venv) и ярлык на рабочем столе.
echo   Дальше можно выбрать: удалить настройки и саму папку программы.
echo   Concert DAW, другие программы и пульт не затрагиваются.
echo.
choice /c YN /n /m "Удалить Concert Stage? [Y/N] "
if errorlevel 2 exit /b 0
echo.

rem ---------------------------------------------------------------
rem  1. Закрыть программу (только ЭТУ: python из папки .venv этой программы)
rem ---------------------------------------------------------------
echo [1/4] Закрываю Concert Stage, если она запущена...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=(Join-Path '%CD%' '.venv').ToLower(); Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($d) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
timeout /t 1 /nobreak >nul

rem ---------------------------------------------------------------
rem  2. Ярлык на рабочем столе
rem ---------------------------------------------------------------
echo [2/4] Удаляю ярлык "Concert Stage"...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Concert Stage.lnk'; if (Test-Path $p) { Remove-Item $p -Force }" >nul 2>nul

rem ---------------------------------------------------------------
rem  3. Окружение Python программы
rem ---------------------------------------------------------------
echo [3/4] Удаляю окружение .venv (библиотеки программы)...
if exist ".venv" rd /s /q ".venv"
if exist ".venv" (
    echo   Не всё удалилось - возможно, программа ещё открыта. Закройте её и запустите uninstall.bat снова.
)
if exist "__pycache__" rd /s /q "__pycache__"

rem ---------------------------------------------------------------
rem  4. Настройки
rem ---------------------------------------------------------------
echo [4/4] Настройки программы: %APPDATA%\ConcertStage
if exist "%APPDATA%\ConcertStage" (
    choice /c YN /n /m "  Удалить и настройки (сцены, расписание, привязки к трекам)? [Y/N] "
    if not errorlevel 2 (
        rd /s /q "%APPDATA%\ConcertStage"
        echo   Настройки удалены.
    ) else (
        echo   Настройки оставлены - при повторной установке всё вернётся как было.
    )
) else (
    echo   Настроек нет.
)
echo.

rem ---------------------------------------------------------------
rem  Сама папка программы (по желанию)
rem ---------------------------------------------------------------
set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
if not exist "%DIR%\main.py" goto done
choice /c YN /n /m "Удалить и саму папку программы со всеми файлами? [Y/N] "
if errorlevel 2 goto done
set "TMPB=%TEMP%\cstage_remove.bat"
echo @echo off> "%TMPB%"
echo chcp 65001 ^>nul>> "%TMPB%"
echo timeout /t 2 /nobreak ^>nul>> "%TMPB%"
echo rd /s /q "%DIR%">> "%TMPB%"
echo del "%%~f0">> "%TMPB%"
cd /d "%TEMP%"
echo.
echo   Папка будет удалена через 2 секунды. Готово.
start "" /min "%TMPB%"
exit /b 0

:done
echo ============================================================
echo   Готово. Concert Stage удалена.
echo   Папку с файлами программы можно удалить вручную.
echo ============================================================
pause
exit /b 0
