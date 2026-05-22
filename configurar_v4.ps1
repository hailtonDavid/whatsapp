Write-Host "Configurando WhatsApp Automation v4..." -ForegroundColor Cyan

$envContent = @"
WA_PROFILE_DIR=profile_whatsapp_v4
WA_HEADLESS=false
WA_READY_TIMEOUT=180
WA_EXPORT_DIR=exports
WA_STATE_DIR=state
"@

Set-Content -Path ".env" -Value $envContent -Encoding UTF8

if (!(Test-Path "config\targets.json")) {
    Copy-Item "config\targets.example.json" "config\targets.json"
    Write-Host "Criado config\targets.json a partir do exemplo." -ForegroundColor Green
}

Write-Host "Pronto." -ForegroundColor Green
Write-Host "Edite config\targets.json com seus grupos/contatos/telefones."
Write-Host "Depois execute:"
Write-Host "python src\whatsapp_auto_downloader.py scan --targets config\targets.json"
