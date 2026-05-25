#Requires -Version 5.1
<#
.SYNOPSIS
  Executa Ruff (lint) e em seguida a suíte Pytest do projeto WhatsApp.
#>
[CmdletBinding()]
param(
    [switch] $SkipRuff,
    [string[]] $PytestArgs = @("-v", "src/tests")
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot | Split-Path -Parent
Set-Location $RepoRoot

$python = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

if (-not $SkipRuff) {
    Write-Host "[Lint] ruff check src ..." -ForegroundColor Cyan
    & $python -m ruff check src
    if ($LASTEXITCODE -ne 0) {
        throw "Ruff lint falhou (codigo $LASTEXITCODE)."
    }
    Write-Host "[Lint] Ruff OK." -ForegroundColor Green
}

Write-Host "[Test] pytest $($PytestArgs -join ' ') ..." -ForegroundColor Cyan
& $python -m pytest @PytestArgs
if ($LASTEXITCODE -ne 0) {
    throw "Pytest falhou (codigo $LASTEXITCODE)."
}
