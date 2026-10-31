@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Concert DAW - установка

echo ============================================================
echo   Concert DAW - установка всего необходимого
echo   Папка программы: %CD%
echo ============================================================
echo.

rem ---------------------------------------------------------------
rem  1. Python 3.10-3.13, 64-бит
rem ---------------------------------------------------------------
set "PY="
set "CHECK=import sys; assert (3,10) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32"

where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -c "%CHECK%" >nul 2>nul && set "PY=py -3.12"
    if not defined PY py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    python -c "%CHECK%" >nul 2>nul && set "PY=python"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)

if not defined PY (
    echo [1/5] Python не найден. Пробую установить Python 3.12 через winget...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   winget недоступен. Установите Python вручную:
        echo   https://www.python.org/downloads/  - версия 3.12, Windows installer 64-bit
        echo   В первом окне установщика поставьте галочку "Add python.exe to PATH".
        echo   Затем снова запустите install.bat
        start "" "https://www.python.org/downloads/"
        pause
        exit /b 1
    )
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    ) else (
        echo.
        echo   Python установлен, но окно нужно перезапустить.
        echo   Закройте это окно и снова запустите install.bat
        pause
        exit /b 1
    )
)
echo [1/5] Python найден:
%PY% --version
echo.

rem ---------------------------------------------------------------
rem  2. Отдельное окружение программы (.venv) - системный Python не трогаем
rem ---------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [2/5] Создаю окружение программы .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo   Не удалось создать окружение. Проверьте права на запись в папку программы.
        pause
        exit /b 1
    )
) else (
    echo [2/5] Окружение .venv уже есть - обновляю.
)
set "VPY=%CD%\.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
echo.

rem ---------------------------------------------------------------
rem  3. Обязательные библиотеки
rem ---------------------------------------------------------------
echo [3/5] Устанавливаю обязательные библиотеки (несколько минут, ~300 МБ)...
set "FAIL="
for %%P in ("PySide6>=6.6" "numpy>=1.24" "sounddevice>=0.4.6" "soundfile>=0.12" "pedalboard>=0.9.12" "scipy>=1.10") do (
    echo   - %%~P
    "%VPY%" -m pip install -U "%%~P" --disable-pip-version-check -q
    if errorlevel 1 (
        echo     [ОШИБКА] не установилось: %%~P
        set "FAIL=1"
    )
)
if defined FAIL (
    echo.
    echo   Часть обязательных библиотек не установилась. Проверьте интернет и запустите
    echo   install.bat ещё раз. Если ошибка повторяется - запустите start_debug.bat и
    echo   пришлите текст ошибки.
    pause
    exit /b 1
)
echo.

rem ---------------------------------------------------------------
rem  4. Дополнительные библиотеки (без них программа работает)
rem ---------------------------------------------------------------
echo [4/5] Дополнительные библиотеки...
for %%P in ("soxr>=0.3" "mutagen>=1.47" "mido>=1.3" "python-rtmidi>=1.5") do (
    echo   - %%~P
    "%VPY%" -m pip install -U "%%~P" --disable-pip-version-check -q
    if errorlevel 1 echo     [предупреждение] не установилось %%~P - соответствующая функция будет отключена
)
echo.

rem ---------------------------------------------------------------
rem  5. Проверка
rem ---------------------------------------------------------------
echo [5/5] Проверка...
"%VPY%" -c "import PySide6, numpy, soundfile, pedalboard; print('  Библиотеки: OK  (PySide6', PySide6.__version__, '/ pedalboard', pedalboard.__version__, ')')"
if errorlevel 1 (
    echo   [ОШИБКА] библиотеки не импортируются - запустите start_debug.bat и пришлите текст.
    pause
    exit /b 1
)
"%VPY%" -c "import os; os.environ['SD_ENABLE_ASIO']='1'; import sounddevice as sd; apis=[h['name'] for h in sd.query_hostapis()]; x32=[d['name'] for d in sd.query_devices() if any(k in d['name'].lower() for k in ('x-usb','xusb','x-live','x32','behringer'))]; print('  ASIO в звуковой библиотеке:', 'есть' if any('ASIO' in a for a in apis) else 'НЕТ'); print('  Пульт X32 (драйвер):', ', '.join(sorted(set(x32))) if x32 else 'не найден - подключите пульт по USB и установите драйвер X-USB ASIO / X-LIVE с behringer.com (без пульта программа тоже работает)')"
echo.

rem ---------------------------------------------------------------
rem  Ярлык на рабочем столе
rem ---------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Concert DAW.lnk')); $s.TargetPath=(Join-Path '%CD%' '.venv\Scripts\pythonw.exe'); $s.Arguments='main.py'; $s.WorkingDirectory='%CD%'; $s.IconLocation=\"$env:SystemRoot\System32\imageres.dll,103\"; $s.Description='Concert DAW'; $s.Save()" >nul 2>nul
if errorlevel 1 (
    echo   Ярлык на рабочем столе создать не удалось - запускайте start.bat
) else (
    echo   Ярлык "Concert DAW" создан на рабочем столе.
)

echo.
echo ============================================================
echo   Готово. Запуск: ярлык "Concert DAW" или start.bat
echo   Если программа не открывается - start_debug.bat покажет ошибку.
echo ============================================================
echo.
choice /c YN /n /m "Запустить программу сейчас? [Y/N] "
if errorlevel 2 exit /b 0
start "" "%CD%\.venv\Scripts\pythonw.exe" main.py
exit /b 0
