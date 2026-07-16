@echo off
rem OverlayGen launcher — double-click to render an FPS comparison overlay.
rem Put captures in: asset\<GameName>\Base\*.json and asset\<GameName>\Acrux\*.json
setlocal
cd /d "%~dp0"

where py >nul 2>nul && goto :usepy
where python >nul 2>nul && goto :usepython
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
goto :done

:usepy
py -3 render_overlay.py --interactive
goto :done

:usepython
python render_overlay.py --interactive

:done
echo.
pause
