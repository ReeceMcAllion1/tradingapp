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
if /i "%ACTION%"=="stop"   goto :stop
echo Unknown command "%ACTION%". Use start, status, report, watch or stop.
exit /b 1

:start
if not exist state mkdir state
for %%F in ("%CONFIGS%\*.toml") do (
  echo   starting %%~nF
  start "tradebot %%~nF" /min cmd /c "py -m tradebot --config "%%F" paper >> "state\%%~nF_session.log" 2>&1"
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
