@echo off
cd /d "%~dp0"

echo.
echo ========================================================
echo   Bancos (Docker) — MongoDB + PostgreSQL
echo ========================================================
echo.

docker compose up -d mongo postgres
if errorlevel 1 (
    echo.
    echo ERRO ao subir MongoDB. Verifique se o Docker Desktop esta rodando.
    pause
    exit /b 1
)

echo.
echo MongoDB:  mongodb://localhost:27020/whatsapp
echo Postgres: postgresql://whatsapp:whatsapp@localhost:5434/whatsapp
echo.
echo Configure no .env local (Flask fora do Docker):
echo   MONGODB_URI=mongodb://localhost:27020/whatsapp
echo   SEMANTIC_DB_URI=postgresql://whatsapp:whatsapp@localhost:5434/whatsapp
echo.
echo Stack completa (app + bancos): .\rodar_docker.bat
echo Para parar: docker compose stop mongo postgres
echo.

pause
