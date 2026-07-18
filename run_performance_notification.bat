@echo off
rem OverlayGen launcher — "Game performance comes first" notification (3s, no sound).
rem Output goes to output\performance_notification_alpha.mov / .webm
setlocal
cd /d "%~dp0"

where py >nul 2>nul && goto :usepy
where python >nul 2>nul && goto :usepython
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
goto :done

:usepy
py -3 notification_overlay.py --text "Game performance comes first" --silent --out output/performance_notification
goto :done

:usepython
python notification_overlay.py --text "Game performance comes first" --silent --out output/performance_notification

:done
echo.
pause
