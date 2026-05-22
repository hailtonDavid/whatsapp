@echo off
cd /d %~dp0
call venv\Scripts\activate
python src\whatsapp_auto_downloader.py scan --targets config\targets.json
pause
