@echo off
rem Recap: one-time setup on Windows.
rem Creates .venv, installs requirements, creates .env from .env.example and asks for API keys.
rem Safe to run again: it reuses the existing .venv and never overwrites an existing .env.

setlocal
cd /d "%~dp0"

rem --- 1. Find Python ---
set SYS_PY=
where py >nul 2>&1 && set SYS_PY=py -3
if not defined SYS_PY (
  where python >nul 2>&1 && set SYS_PY=python
)
if not defined SYS_PY (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run setup.bat again.
  pause
  exit /b 1
)

rem --- 2. Virtual environment ---
set PY=%~dp0.venv\Scripts\python.exe
if exist "%PY%" (
  echo [1/3] Using existing .venv
) else (
  echo [1/3] Creating .venv ...
  %SYS_PY% -m venv .venv || goto fail
)

rem --- 3. Requirements ---
echo [2/3] Installing requirements ...
"%PY%" -m pip install --upgrade pip --quiet || goto fail
"%PY%" -m pip install -r requirements.txt || goto fail

rem --- 4. .env ---
if exist .env (
  echo [3/3] .env already exists, leaving it as is
) else (
  echo [3/3] Creating .env from .env.example
  copy /y .env.example .env >nul || goto fail
  echo.
  echo Paste your API keys below, or press Enter to skip. Recap works without them
  echo using offline captions, predictions and notes. You can edit .env later.
  call :askkey GEMINI_API_KEY "Gemini API key"
  call :askkey ELEVENLABS_API_KEY "ElevenLabs API key (optional)"
)

echo.
echo Setup done. Next:
echo   start_session.bat          live session with the mic
echo   start_session.bat demo     demo lesson, no mic needed
echo or see "Quick start" in README.md to run each part yourself.
pause
exit /b 0

:askkey
rem %1 = variable name in .env, %2 = prompt text
set "KEYVAL="
set /p "KEYVAL=%~2: "
if not defined KEYVAL goto :eof
set "KEYNAME=%~1"
powershell -NoProfile -Command "$p='.env'; $c=[IO.File]::ReadAllText($p); $c=$c -replace ('(?m)^' + $env:KEYNAME + '=.*$'), ($env:KEYNAME + '=' + $env:KEYVAL.Trim()); [IO.File]::WriteAllText($p, $c)"
goto :eof

:fail
echo.
echo Setup failed. Read the error above, fix it, and run setup.bat again.
pause
exit /b 1
