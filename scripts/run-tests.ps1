#Requires -Version 5.1
<#
.SYNOPSIS
  Executa Ruff (lint) e em seguida a suíte Pytest do projeto WhatsApp.

.NOTES
  Por padrão pytest.ini exclui @browser e @slow.
  Suíte completa: pytest -m "" ou pytest -m "slow or browser"
#>
[CmdletBinding()]
param(
    [switch] $SkipRuff,
    [switch] $Full,
    [string[]] $PytestArgs
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

if (-not $PytestArgs) {
    if ($Full) {
        $PytestArgs = @("-v", "src/tests", "-m", "")
    } else {
        $PytestArgs = @("-v", "src/tests")
    }
}

Write-Host "[Test] pytest $($PytestArgs -join ' ') ..." -ForegroundColor Cyan
& $python -m pytest @PytestArgs
if ($LASTEXITCODE -ne 0) {
    throw "Pytest falhou (codigo $LASTEXITCODE)."
}
