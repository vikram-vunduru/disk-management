[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,
    [string]$ResourceGroupName = 'rg-disk-dashboard-test',
    [switch]$PurgeKeyVaults
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$null = Get-Command az -ErrorAction Stop
$account = az account show --subscription $SubscriptionId --only-show-errors -o json 2>$null
if (-not $account) {
    throw "Azure CLI is not authenticated for subscription '$SubscriptionId'. Run 'az login' and retry."
}

az group show --name $ResourceGroupName --subscription $SubscriptionId --only-show-errors -o json 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Resource group '$ResourceGroupName' not found - nothing to delete."
    return
}

$kvNames = @()
if ($PurgeKeyVaults) {
    $kvJson = az keyvault list --resource-group $ResourceGroupName --subscription $SubscriptionId --only-show-errors -o json 2>$null
    if ($LASTEXITCODE -eq 0 -and $kvJson) {
        $kvNames = ($kvJson | ConvertFrom-Json) | ForEach-Object { $_.name }
    }
}

Write-Host "Deleting resource group '$ResourceGroupName' in subscription $SubscriptionId..." -ForegroundColor Yellow
az group delete --name $ResourceGroupName --subscription $SubscriptionId --yes --only-show-errors | Out-Null
Write-Host "Resource group deleted."

if ($PurgeKeyVaults -and $kvNames.Count -gt 0) {
    foreach ($kv in $kvNames) {
        Write-Host "Purging soft-deleted Key Vault '$kv'..." -ForegroundColor Yellow
        az keyvault purge --name $kv --subscription $SubscriptionId --only-show-errors 2>$null | Out-Null
    }
}
