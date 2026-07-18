@echo off
rem OverlayGen launcher — full stream overlay with mock Twitch chat
rem (10s: LIVE chip, chat with lag complaints, follower goal panel).
rem Output goes to output\stream_chat_alpha.mov / .webm
setlocal
cd /d "%~dp0"

where py >nul 2>nul && goto :usepy
where python >nul 2>nul && goto :usepython
echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
goto :done

:usepy
py -3 stream_overlay.py
goto :done

:usepython
python stream_overlay.py

:done
echo.
pause
