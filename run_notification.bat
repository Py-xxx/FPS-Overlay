@echo off
rem OverlayGen launcher — "Your stream shouldn't be stealing your frames"
rem notification (3s, pops up from the bottom, with chime sound).
rem Output goes to output\notification_alpha.mov / .webm
setlocal
cd /d "%~dp0"

where py >nul 2>nul && goto :usepy
where python >nul 2>nul && goto :usepython
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
goto :done

:usepy
py -3 notification_overlay.py
goto :done

:usepython
python notification_overlay.py

:done
echo.
pause
