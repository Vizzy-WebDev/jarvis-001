@echo off
title Jarvis Launcher
cd /d "%~dp0"

if not exist node_modules (
    echo Setting up Jarvis for the first time - this can take a minute...
    echo.
    call npm install
    if errorlevel 1 (
        echo.
        echo Something went wrong during setup. Scroll up to see the error above.
        pause
        exit /b 1
    )
    echo.
    echo Setup complete.
    echo.
)

echo Starting the Jarvis server...
start "Jarvis - keep this window open while you use Jarvis" cmd /k node server\server.js

echo Waiting for it to start...
timeout /t 2 /nobreak >nul

echo Opening Jarvis in your browser...
start "" http://localhost:3000/

echo.
echo Jarvis is opening in your browser now.
echo Keep the OTHER window (titled "Jarvis") open while you use it - closing it stops Jarvis.
echo This launcher window will close on its own in a few seconds.
timeout /t 5 /nobreak >nul
exit
