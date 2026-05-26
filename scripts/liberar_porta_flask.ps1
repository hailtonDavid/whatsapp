# Libera a porta do painel Flask (padrao 5014) encerrando processos antigos.
param(
    [int]$Port = 5014
)

$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $connections) {
    Write-Host "Porta $Port livre."
    exit 0
}

$pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    Write-Host "Encerrando PID $procId na porta $Port..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "AVISO: ainda ha processo na porta $Port." -ForegroundColor Yellow
    exit 1
}

Write-Host "Porta $Port liberada."
exit 0
