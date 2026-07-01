@echo off
title Gnoll Guard 1.3.0 (dev/test)
cd /d "C:\Users\coryo\Documents\INTERNETSTUFF\codex\GnollGuard"
echo Launching Gnoll Guard 1.3.0 from source...
echo (Keep this window open - it shows DPS log lines and any errors.)
echo.
py -3.11 app\main.py
echo.
echo ---- Gnoll Guard exited (code %errorlevel%). Close this window when done. ----
pause
