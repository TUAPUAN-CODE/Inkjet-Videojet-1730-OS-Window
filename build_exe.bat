@echo off
rem ============================================================
rem  i-Tail QR Inkjet Sender - build a single .exe
rem  Double-click this file.  Needs Python 3.9 - 3.13 (64-bit) and internet.
rem  Result:  dist\iTail-QR-Inkjet.exe
rem ============================================================
setlocal
cd /d "%~dp0"
title Build i-Tail QR Inkjet Sender

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Python was not found.
        echo         Install it from https://www.python.org/downloads/
        echo         and tick "Add python.exe to PATH", then run this file again.
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

echo.
echo [1/4] Creating virtual environment ...
if not exist ".venv_build\Scripts\python.exe" (
    %PY% -m venv .venv_build
    if errorlevel 1 goto :fail
)
call ".venv_build\Scripts\activate.bat"

echo.
echo [2/4] Installing libraries (first time takes a few minutes) ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo.
echo [3/4] Building the .exe ...
pyinstaller --noconfirm --clean --onefile --windowed ^
    --name "iTail-QR-Inkjet" ^
    --icon "assets\icon.ico" ^
    --add-data "assets;assets" ^
    --collect-all zxingcpp ^
    app.py
if errorlevel 1 goto :fail

echo.
echo [4/4] Done.
echo.
echo     Program : %~dp0dist\iTail-QR-Inkjet.exe
echo     Settings and send log are stored in: %%APPDATA%%\iTail-QR-Inkjet
echo.
explorer "%~dp0dist"
pause
exit /b 0

:fail
echo.
echo [ERROR] The build failed. Take a photo / screenshot of the messages above
echo         and send it to the developer.
echo.
pause
exit /b 1
