@echo off
cd /d "%~dp0"

echo.
echo ========================================================
echo   WhatsApp Web Automation
echo   Encerrando servidor antigo e iniciando painel novo...
echo ========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\liberar_porta_flask.ps1"
if errorlevel 1 (
    echo.
    echo ERRO: porta 5014 ainda ocupada. Execute parar_flask.bat ou feche o terminal antigo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
set FLASK_OPEN_BROWSER=true
set FLASK_PORT=5014

echo Painel: http://127.0.0.1:5014/painel
echo.

REM Aguarda o servidor subir e abre o painel
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:5014/painel"

python src\app.py
