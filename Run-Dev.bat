@echo off
title Gnoll Guard (dev - current source)
REM Run from THIS folder (_migrate\app = the canonical Gnollsfoot code), never a
REM hardcoded path. %~dp0 is the directory this .bat lives in.
cd /d "%~dp0"
echo Launching Gnoll Guard from source:
echo   %~dp0
echo (Keep this window open - it shows any errors.)
echo.
py -3.11 app\main.py
echo.
echo ---- Gnoll Guard exited (code %errorlevel%). Close this window when done. ----
pause
