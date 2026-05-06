[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,
    [string]$ResourceGroupName = 'rg-disk-dashboard-test',
    [string]$Location = 'eastus',
    [string]$VmName = 'diskvm',
    [string]$VmSize = 'Standard_D2s_v3',
    [string]$Zone = '1',
    [string]$AdminUsername = 'azureuser',
    [switch]$IncludeBurstingTest,
    [switch]$IncludeDoubleEncryptionTest,
    [switch]$IncludeRedScenario,
    [string]$NotV2Location = 'southindia'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Section { param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Invoke-Az {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & az @Arguments --subscription $SubscriptionId --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Arguments -join ' ')"
    }
    if ([string]::IsNullOrWhiteSpace($output)) { return $null }
    return $output | ConvertFrom-Json
}

function Test-Az {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & {
        $ErrorActionPreference = 'SilentlyContinue'
        & az @Arguments --subscription $SubscriptionId --only-show-errors -o json 2>&1 | Out-Null
    }
    return $LASTEXITCODE -eq 0
}

Write-Section "Verify Azure CLI"
$null = Get-Command az -ErrorAction Stop
$account = az account show --subscription $SubscriptionId --only-show-errors -o json 2>$null
if (-not $account) {
    throw "Azure CLI is not authenticated for subscription '$SubscriptionId'. Run 'az login' and retry."
}
Write-Host "Subscription: $SubscriptionId"
Write-Host "Resource group: $ResourceGroupName ($Location)"
if ($IncludeRedScenario) {
    Write-Host "Non-v2 region (for red status): $NotV2Location"
}

Write-Section "Create resource group"
if (-not (Test-Az @('group','show','--name',$ResourceGroupName))) {
    Invoke-Az @('group','create','--name',$ResourceGroupName,'--location',$Location) | Out-Null
    Write-Host "Created RG '$ResourceGroupName'."
} else {
    Write-Host "RG '$ResourceGroupName' already exists, reusing."
}

Write-Section "Create network + small Linux VM"
if (-not (Test-Az @('vm','show','--resource-group',$ResourceGroupName,'--name',$VmName))) {
    Invoke-Az @(
        'vm','create',
        '--resource-group',$ResourceGroupName,
        '--name',$VmName,
        '--location',$Location,
        '--zone',$Zone,
        '--image','Ubuntu2204',
        '--size',$VmSize,
        '--admin-username',$AdminUsername,
        '--generate-ssh-keys',
        '--storage-sku','Standard_LRS',
        '--os-disk-size-gb','30',
        '--public-ip-sku','Standard',
        '--nsg-rule','NONE'
    ) | Out-Null
    Write-Host "Created VM '$VmName'."
} else {
    Write-Host "VM '$VmName' already exists, reusing."
}

function New-TestDisk {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Sku,
        [int]$SizeGb = 32,
        [int]$LogicalSectorSize = 512,
        [string]$DiskLocation = $Location,
        [hashtable]$Tags,
        [switch]$EnableBursting,
        [string]$DiskEncryptionSetId
    )

    if (Test-Az @('disk','show','--resource-group',$ResourceGroupName,'--name',$Name)) {
        Write-Host "Disk '$Name' already exists."
        return
    }

    $cmdArgs = @(
        'disk','create',
        '--resource-group',$ResourceGroupName,
        '--name',$Name,
        '--location',$DiskLocation,
        '--sku',$Sku,
        '--size-gb',"$SizeGb"
    )
    if ($Sku -in @('Premium_LRS','PremiumV2_LRS')) {
        $cmdArgs += @('--logical-sector-size',"$LogicalSectorSize")
    }
    if ($Sku -in @('Premium_LRS','PremiumV2_LRS')) {
        $cmdArgs += @('--zone',$Zone)
    }
    if ($EnableBursting) { $cmdArgs += @('--enable-bursting','true') }
    if ($DiskEncryptionSetId) { $cmdArgs += @('--disk-encryption-set',$DiskEncryptionSetId) }
    if ($Tags) {
        $tagPairs = foreach ($k in $Tags.Keys) { "$k=$($Tags[$k])" }
        $cmdArgs += @('--tags') + $tagPairs
    }

    Invoke-Az $cmdArgs | Out-Null
    Write-Host "Created disk '$Name' ($Sku, ${SizeGb}GiB, sector=$LogicalSectorSize, region=$DiskLocation)."
}

function Add-DataDisk {
    param(
        [Parameter(Mandatory=$true)][string]$DiskName,
        [string]$Caching = 'None'
    )
    $vm = Invoke-Az @('vm','show','--resource-group',$ResourceGroupName,'--name',$VmName)
    $existing = $vm.storageProfile.dataDisks | Where-Object { $_.name -eq $DiskName }
    if ($existing) {
        Write-Host "Disk '$DiskName' already attached."
        return
    }
    Invoke-Az @(
        'vm','disk','attach',
        '--resource-group',$ResourceGroupName,
        '--vm-name',$VmName,
        '--name',$DiskName,
        '--caching',$Caching
    ) | Out-Null
    Write-Host "Attached '$DiskName' to '$VmName' with caching=$Caching."
}

Write-Section "Create test disks"

$skipped = @()
function Invoke-Scenario {
    param([string]$Label, [scriptblock]$Action)
    try { & $Action } catch {
        Write-Warning "Scenario '$Label' skipped: $($_.Exception.Message)"
        $script:skipped += $Label
    }
}

# GREEN: Premium_LRS, sector 512, no caching, no flags
Invoke-Scenario 'disk-green' {
    New-TestDisk -Name 'disk-green' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 512
    Add-DataDisk -DiskName 'disk-green' -Caching 'None'
}

# YELLOW (sector != 512) - may require tenant allowlist for 4096-byte sector Premium_LRS disks
Invoke-Scenario 'disk-yellow-sector' {
    New-TestDisk -Name 'disk-yellow-sector' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 4096
    Add-DataDisk -DiskName 'disk-yellow-sector' -Caching 'None'
}

# YELLOW (caching ReadWrite)
Invoke-Scenario 'disk-yellow-caching' {
    New-TestDisk -Name 'disk-yellow-caching' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 512
    Add-DataDisk -DiskName 'disk-yellow-caching' -Caching 'ReadWrite'
}

# YELLOW (ASR heuristic - tag with "ASR")
Invoke-Scenario 'disk-yellow-asr' {
    New-TestDisk -Name 'disk-yellow-asr' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 512 -Tags @{ ASR='Enabled' }
    Add-DataDisk -DiskName 'disk-yellow-asr' -Caching 'None'
}

# RED (opt-in): Premium_LRS in a region without PremiumV2.
if ($IncludeRedScenario) {
    Invoke-Scenario 'disk-red' {
        New-TestDisk -Name 'disk-red' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 512 -DiskLocation $NotV2Location
    }
}

# V2 disk (already migrated)
Invoke-Scenario 'disk-v2' {
    New-TestDisk -Name 'disk-v2' -Sku 'PremiumV2_LRS' -SizeGb 4 -LogicalSectorSize 512
}

# Other SKUs (skipped by Premium LRS view, visible in All Disks)
Invoke-Scenario 'disk-standard-unattached'    { New-TestDisk -Name 'disk-standard-unattached' -Sku 'Standard_LRS' -SizeGb 32 }
Invoke-Scenario 'disk-standardssd-unattached' { New-TestDisk -Name 'disk-standardssd-unattached' -Sku 'StandardSSD_LRS' -SizeGb 32 }
Invoke-Scenario 'disk-standardssd-zrs'        { New-TestDisk -Name 'disk-standardssd-zrs' -Sku 'StandardSSD_ZRS' -SizeGb 32 }

# Premium LRS unattached (data, no flags) - should also be GREEN
Invoke-Scenario 'disk-green-unattached' {
    New-TestDisk -Name 'disk-green-unattached' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 512
}

if ($IncludeBurstingTest) {
    Write-Section "Optional: bursting test disk (P30, 1024 GiB - costly)"
    Invoke-Scenario 'disk-yellow-bursting' {
        New-TestDisk -Name 'disk-yellow-bursting' -Sku 'Premium_LRS' -SizeGb 1024 -LogicalSectorSize 512 -EnableBursting
        Add-DataDisk -DiskName 'disk-yellow-bursting' -Caching 'None'
    }
}

if ($IncludeDoubleEncryptionTest) {
    Write-Section "Optional: double-encryption disk (Key Vault + DES)"
    $kvName = "kvdiskdash$((Get-Random -Maximum 99999))"
    $keyName = 'des-key'
    $desName = 'des-double'

    Invoke-Az @(
        'keyvault','create',
        '--resource-group',$ResourceGroupName,
        '--name',$kvName,
        '--location',$Location,
        '--enable-purge-protection','true',
        '--retention-days','7'
    ) | Out-Null

    Invoke-Az @(
        'keyvault','key','create',
        '--vault-name',$kvName,
        '--name',$keyName,
        '--kty','RSA',
        '--size','2048'
    ) | Out-Null

    $key = Invoke-Az @('keyvault','key','show','--vault-name',$kvName,'--name',$keyName)
    $kv  = Invoke-Az @('keyvault','show','--name',$kvName)

    $des = Invoke-Az @(
        'disk-encryption-set','create',
        '--resource-group',$ResourceGroupName,
        '--name',$desName,
        '--location',$Location,
        '--source-vault',$kv.id,
        '--key-url',$key.key.kid,
        '--encryption-type','EncryptionAtRestWithPlatformAndCustomerKeys'
    )

    $desPrincipalId = $des.identity.principalId
    Invoke-Az @(
        'keyvault','set-policy',
        '--name',$kvName,
        '--object-id',$desPrincipalId,
        '--key-permissions','wrapkey','unwrapkey','get'
    ) | Out-Null

    New-TestDisk -Name 'disk-yellow-doubleenc' -Sku 'Premium_LRS' -SizeGb 32 -LogicalSectorSize 512 -DiskEncryptionSetId $des.id
    Add-DataDisk -DiskName 'disk-yellow-doubleenc' -Caching 'None'
}

Write-Section "Summary"
$disks = Invoke-Az @('disk','list','--resource-group',$ResourceGroupName)
$disks | Select-Object name, location, @{n='sku';e={$_.sku.name}}, diskSizeGb, @{n='sector';e={$_.logicalSectorSize}}, burstingEnabled, diskState |
    Format-Table -AutoSize

if ($skipped.Count -gt 0) {
    Write-Host ""
    Write-Host "Scenarios skipped (see warnings above):" -ForegroundColor Yellow
    $skipped | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "Test environment ready in subscription $SubscriptionId, resource group $ResourceGroupName."
Write-Host "Run the dashboard, pick this subscription, and filter by resource group '$ResourceGroupName' to see all scenarios."
Write-Host ""
Write-Host "Expected colors in the Premium LRS Data Disks view:"
Write-Host "  GREEN  : disk-green, disk-green-unattached"
Write-Host "  YELLOW : disk-yellow-sector, disk-yellow-caching, disk-yellow-asr"
if ($IncludeBurstingTest)        { Write-Host "  YELLOW : disk-yellow-bursting" }
if ($IncludeDoubleEncryptionTest){ Write-Host "  YELLOW : disk-yellow-doubleenc" }
if ($IncludeRedScenario)         { Write-Host "  RED    : disk-red (in $NotV2Location)" }
else                             { Write-Host "  RED    : (skipped - re-run with -IncludeRedScenario to add a disk in $NotV2Location)" }
Write-Host ""
Write-Host "When done, tear down with: .\scripts\Teardown-TestEnvironment.ps1 -ResourceGroupName $ResourceGroupName -SubscriptionId $SubscriptionId"
