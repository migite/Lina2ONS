@echo off
rem Double-click to convert the extracted scripts (..\unpacked) into an ONScripter game folder
rem (..\onscripter). Run extract_all.bat first. Pass --engine ponscripter for a Ponscripter folder.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python 3.9 or newer was not found. Install it from https://www.python.org/ and try again.
    pause
    exit /b 1
)

%PY% convert_to_ons.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Finished with errors ^(exit code %RC%^).
pause
exit /b %RC%
