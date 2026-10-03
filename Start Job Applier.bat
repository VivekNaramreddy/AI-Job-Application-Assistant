@echo off
setlocal

rem %~dp0 is the folder containing this launcher.
rem This makes the launcher work even when it is double-clicked.
cd /d "%~dp0"

set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%APP_PYTHON%" (
    echo.
    echo ERROR: The Job Applier Python environment was not found.
    echo Expected file:
    echo %APP_PYTHON%
    echo.
    echo The project needs to be set up before it can run.
    pause
    exit /b 1
)

echo Starting Job Applier...
echo The dashboard will open automatically in your browser.
echo Keep this window open while Job Applier is running.
echo To stop the application, return here and press Ctrl+C.
echo.

"%APP_PYTHON%" "%~dp0run.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%APP_EXIT_CODE%"=="0" (
    echo Job Applier stopped because of an error. Exit code: %APP_EXIT_CODE%
) else (
    echo Job Applier has stopped.
)
pause
exit /b %APP_EXIT_CODE%
