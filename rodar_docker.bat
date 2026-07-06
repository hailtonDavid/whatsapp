@echo off
cd /d "%~dp0"

echo.
echo ========================================================
echo   WhatsApp Web Automation — Docker (app + bancos)
echo ========================================================
echo.

if not exist .env.docker (
    if exist .env.docker.example (
        copy /Y .env.docker.example .env.docker >nul
        echo Criado .env.docker a partir do exemplo.
    )
)

docker compose up -d --build
if errorlevel 1 (
    echo.
    echo ERRO ao subir containers. Verifique se o Docker Desktop esta rodando.
    pause
    exit /b 1
)

echo.
echo Painel:  http://127.0.0.1:5014/painel
echo MongoDB:  mongodb://127.0.0.1:27020/whatsapp
echo Postgres: postgresql://whatsapp:whatsapp@127.0.0.1:5434/whatsapp
echo.
echo Logs: docker compose logs -f app
echo Parar: docker compose down
echo.

pause
