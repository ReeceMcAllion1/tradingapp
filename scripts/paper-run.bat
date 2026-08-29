@echo off
REM Supervise paper-trading sessions on Windows.
REM
REM The .sh version of this is a bash script and will not run in cmd, which left
REM Windows users with no way to start more than one session at a time. This is the
REM same idea in the one scripting language every Windows machine already has.
REM
REM   scripts\paper-run.bat start     start a session for every config in configs\
REM   scripts\paper-run.bat status    what each session is doing
REM   scripts\paper-run.bat report    the verdict so far, against buy-and-hold
REM   scripts\paper-run.bat watch     open the live dashboard in a browser
REM   scripts\paper-run.bat logs      show the tail of each session log
REM   scripts\paper-run.bat stop      stop them all
setlocal enabledelayedexpansion

cd /d "%~dp0.."

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher 'py' not found.
  echo Install Python 3.11 or newer from python.org and tick "Add Python to PATH".
  exit /b 1
)

set CONFIGS=configs
if not exist "%CONFIGS%\*.toml" (
  echo No configs found in %CONFIGS%\
  echo Copy config.example.toml into %CONFIGS%\ and edit it.
  exit /b 1
)

set ACTION=%~1
if "%ACTION%"=="" set ACTION=start

if /i "%ACTION%"=="start" goto :start
if /i "%ACTION%"=="status" goto :status
if /i "%ACTION%"=="report" goto :report
if /i "%ACTION%"=="watch"  goto :watch
if /i "%ACTION%"=="stop"   goto :logs
if not exist state\*_session.log (
  echo   No session logs yet. Have you run "scripts\paper-run.bat start"?
  exit /b 0
)
for %%L in (state\*_session.log) do (
  echo.
  echo   ==== %%~nL ====
  powershell -NoProfile -Command "Get-Content -Tail 15 -LiteralPath '%%L'" 2>nul || type "%%L"
)
exit /b 0

:logs
if not exist state\*_session.log (
  echo   No session logs yet. Have you run "scripts\paper-run.bat start"?
  exit /b 0
)
for %%L in (state\*_session.log) do (
  echo.
  echo   ==== %%~nL ====
  powershell -NoProfile -Command "Get-Content -Tail 15 -LiteralPath '%%L'"
)
exit /b 0

:stop
if /i "%ACTION%"=="logs"   goto :logs
echo Unknown command "%ACTION%". Use start, status, report, watch, logs or stop.
exit /b 1

:start
if not exist state mkdir state
for %%F in ("%CONFIGS%\*.toml") do (
  echo   starting %%~nF
  REM No nested quotes inside cmd /c: cmd strips the outer pair and the inner ones
  REM then confuse it, which breaks for anyone whose folder has a space in it. The
  REM paths here are relative to the repo root we already cd'd into, so they are short
  REM and space-free, and the redirection is what captures a crash the app never gets
  REM to log itself.
  start "tradebot %%~nF" /min cmd /c py -m tradebot --config %%F paper ^>^> state\%%~nF_session.log 2^>^&1
)
echo.
echo   Running. Each session is in its own minimised window.
echo   Watch them:  scripts\paper-run.bat watch
echo   Stop them:   scripts\paper-run.bat stop
exit /b 0

:status
for %%F in ("%CONFIGS%\*.toml") do py -m tradebot --config "%%F" status
exit /b 0

:report
py -m tradebot report %CONFIGS%\*.toml
exit /b 0

:watch
py -m tradebot dashboard %CONFIGS%\*.toml
exit /b 0

:stop
REM Only the windows this script started are titled "tradebot ..."; a bare
REM taskkill on python.exe would take down every other Python program running.
taskkill /fi "WINDOWTITLE eq tradebot *" /t /f >nul 2>nul
echo   stopped
exit /b 0
