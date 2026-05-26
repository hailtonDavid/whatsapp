@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\liberar_porta_flask.ps1"
if errorlevel 1 (
    echo Nao foi possivel liberar a porta 5014. Feche manualmente o processo antigo.
    pause
    exit /b 1
)
echo Porta 5014 liberada.
