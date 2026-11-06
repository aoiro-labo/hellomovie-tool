@echo off
setlocal
title HELLOMovie Tool

pushd "%~dp0"

rem Messages in this file are English only: they appear before the display language is chosen.
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    echo Install Python and run this file again.
    echo.
    pause
    popd
    exit /b 1
)

chcp 65001 >nul
python "%~dp0hellomovie_launcher.py" %*
set "script_exit=%errorlevel%"

rem Exit code 10 = the user chose Quit in the menu: close without pausing.
if "%script_exit%"=="10" (
    popd
    exit /b 0
)

echo.
pause
popd
exit /b %script_exit%
