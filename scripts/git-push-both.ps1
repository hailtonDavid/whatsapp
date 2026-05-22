#Requires -Version 5.1
<#
.SYNOPSIS
  Envia a branch atual para GitHub e Gitea (push duplo via origin).
#>
[CmdletBinding()]
param([string] $Branch = "")

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $RepoRoot

& (Join-Path $PSScriptRoot "git-ensure-dual-remotes.ps1") -Quiet

if ([string]::IsNullOrWhiteSpace($Branch)) {
  $Branch = (& git rev-parse --abbrev-ref HEAD).Trim()
}

Write-Host ("[Git] git push origin {0} (GitHub + Gitea) ..." -f $Branch) -ForegroundColor Cyan
& git push origin $Branch
if ($LASTEXITCODE -ne 0) { throw ("Push falhou (codigo {0})." -f $LASTEXITCODE) }
Write-Host "[Git] Push concluido em GitHub e Gitea." -ForegroundColor Green
