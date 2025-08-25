@echo off
setlocal

rem Always run heic2jpg.py from the same folder as this batch file
set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%heic2jpg.py

if not exist "%SCRIPT%" (
    echo Could not find heic2jpg.py in %SCRIPT_DIR%
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Processing current folder: %cd%
    python "%SCRIPT%"
) else (
    echo Processing selected paths...
    python "%SCRIPT%" %*
)

echo.
pause
