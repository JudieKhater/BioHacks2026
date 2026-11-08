@echo off
rem Recap: get everything ready for a live session with the microphone.
rem Opens three windows: the server, the on-screen caption overlay, and the mic client.
rem The mic stays OFF until you press "Start session" on the page that opens, and turns off
rem again at "End session", which builds your session report and breathing plan.
rem Close "Recap server" to stop everything.
rem Usage: double-click, or   start_session.bat demo   to play the CN III lesson instead of the mic.

cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" (
  echo Can't find .venv. Create it first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

start "Recap server" "%PY%" -m uvicorn backend.app:app --host 0.0.0.0 --port 8000

rem wait until the server answers (up to ~20 s)
for /l %%i in (1,1,20) do (
  "%PY%" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=1)" >nul 2>&1 && goto ready
  timeout /t 1 /nobreak >nul
)
echo The server didn't start. Check the "Recap server" window for errors.
pause
exit /b 1

:ready
start "Recap overlay" "%PY%" client\overlay.py
if /i "%1"=="demo" (
  start "Recap demo lesson" "%PY%" client\simulator.py --lesson
) else (
  start "Recap mic" "%PY%" client\audio_client.py --captions on
)
start "" http://127.0.0.1:8000/notes.html
echo Recap is ready. Press Start session on http://127.0.0.1:8000/notes.html to begin listening.
