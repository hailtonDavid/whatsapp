@echo off
setlocal
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "GIT_CONFIG_COUNT=1"
set "GIT_CONFIG_KEY_0=safe.directory"
set "GIT_CONFIG_VALUE_0=%REPO_ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0git-push-both.ps1" %*
