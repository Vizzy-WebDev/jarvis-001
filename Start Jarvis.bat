@echo off
title Jarvis Launcher
cd /d "%~dp0"

rem Jarvis runs as ONE Python process serving ONE port: FastAPI answers the API
rem and hands the browser the already-built front end itself. There is no Node
rem here any more, and nothing is built at launch - frontend\out ships ready to
rem serve, so starting is immediate.

rem --- find Python -------------------------------------------------------------
rem The Windows launcher (py) first, since that is what a normal python.org
rem install provides, then a plain python on PATH.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo Jarvis needs Python, and it doesn't look like it's installed yet.
    echo.
    echo   1. Go to  https://www.python.org/downloads/
    echo   2. Download Python for Windows and run the installer.
    echo   3. IMPORTANT: on the first screen, tick "Add python.exe to PATH".
    echo   4. When it finishes, double-click this file again.
    echo.
    pause
    exit /b 1
)

rem --- first-run setup ---------------------------------------------------------
set "VENV=backend\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Setting up Jarvis for the first time - this can take a few minutes...
    echo.
    %PY% -m venv "%VENV%"
    if errorlevel 1 (
        echo.
        echo Couldn't create the setup folder Jarvis needs. Scroll up to see why.
        pause
        exit /b 1
    )
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -e backend
    if errorlevel 1 (
        echo.
        echo Something went wrong during setup. Scroll up to see the error above.
        echo.
        echo If you want to start over, delete the folder  backend\.venv
        echo and double-click this file again.
        pause
        exit /b 1
    )
    echo.
    echo Setup complete.
    echo.
)

rem --- start it ----------------------------------------------------------------
if not exist "frontend\out\index.html" (
    echo.
    echo Jarvis's screens are missing from this copy ^(frontend\out^).
    echo This usually means the download was incomplete - try getting a fresh copy.
    echo.
    pause
    exit /b 1
)

echo Starting the Jarvis server...
start "Jarvis - keep this window open while you use Jarvis" cmd /k ""%~dp0%VENV_PY%" -m jarvis.main"

rem Wait for the server to actually answer rather than guessing at a fixed delay:
rem a cold start is slower than a warm one, and opening the browser too early
rem shows a connection error for something that was about to work fine.
echo Waiting for it to start...
set "READY="
for /l %%i in (1,1,60) do (
    if not defined READY (
        %PY% -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:3000/api/status', timeout=1); sys.exit(0)" >nul 2>&1 && set "READY=1"
        if not defined READY ping -n 2 127.0.0.1 >nul
    )
)

if not defined READY (
    echo.
    echo Jarvis is taking longer than usual to start.
    echo Check the OTHER window ^(titled "Jarvis"^) - if there's an error, it's in there.
    echo.
    pause
    exit /b 1
)

echo Opening Jarvis in your browser...
start "" http://127.0.0.1:3000/

echo.
echo Jarvis is opening in your browser now.
echo Keep the OTHER window (titled "Jarvis") open while you use it - closing it stops Jarvis.
echo This launcher window will close on its own in a few seconds.
timeout /t 5 /nobreak >nul
exit
