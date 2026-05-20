# Azure Disk Management Dashboard

This repository provides a local Azure disk management toolset for:

- viewing all managed disks in a selected subscription
- identifying Premium SSD `v1` disks that can be migrated to Premium SSD `v2`
- creating snapshots before migration
- migrating selected disks from `Premium_LRS` to `PremiumV2_LRS`
- identifying and deleting unattached disks

The main user experience is the local dashboard in `dashboard/app.py`.

## What is in this repo

### 1. Dashboard

`dashboard/app.py` is a local web dashboard that uses your Azure CLI login.

It supports:

- selecting any available Azure subscription
- optional resource group filtering
- side navigation for:
  - `All Disks`
  - `Eligible Disks`
  - `Unattached Disks`
- selecting eligible V1 disks for migration
- optional snapshot creation before migration
- selecting unattached disks for export, portal review, or deletion
- CSV export

### 2. PowerShell script

`scripts/Invoke-DiskManagement.ps1` is a script-based workflow for inventory, migration, and cleanup from the terminal.

Use it if you prefer CLI/PowerShell automation instead of the dashboard.

## Prerequisites

- Windows PowerShell 5.1 or PowerShell 7+
- Python 3.11+
- Azure CLI installed
- Azure account with permission to read, update, snapshot, start/stop VMs, and delete disks

## How to run the dashboard

From the repo root:

```powershell
az login --use-device-code
python .\dashboard\app.py
```

Then open:

```text
http://127.0.0.1:8765
```

## Dashboard workflow

### All Disks

Use this view to inspect the full managed disk inventory for a subscription or resource group.

### Eligible Disks

Use this view to:

- select eligible `Premium_LRS` disks
- optionally enable `Backup Before Migration`
- click `Backup Selected` to create snapshots only
- click `Migrate Selected` to convert selected disks to `PremiumV2_LRS`

### Unattached Disks

Use this view to:

- select unattached disks
- click `Export CSV` to export selected unattached disks
- click `Open Selected` to open selected disks in Azure Portal
- click `Delete Selected` to delete selected unattached disks

## Important behavior

- OS disks are not migrated to Premium SSD v2
- attached data disks are deallocated with their VM before migration and started again afterward
- `Delete Selected` performs real deletion in Azure
- `Migrate Selected` performs real migration in Azure
- snapshots are created only when you choose `Backup Selected` or `Backup Before Migration`

## How to run the PowerShell script

### Inventory

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Inventory -SubscriptionId <subscription-id>
```

### Inventory with JSON report

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Inventory -SubscriptionId <subscription-id> -ReportPath .\reports\inventory.json
```

### Migration dry run

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Migrate -SubscriptionId <subscription-id> -WhatIf
```

### Execute migration

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Migrate -SubscriptionId <subscription-id> -Apply
```

### Execute migration with snapshots

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Migrate -SubscriptionId <subscription-id> -Apply -CreateSnapshotBefore
```

### Cleanup dry run

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Cleanup -SubscriptionId <subscription-id> -WhatIf
```

### Execute cleanup

```powershell
.\scripts\Invoke-DiskManagement.ps1 -Action Cleanup -SubscriptionId <subscription-id> -Apply
```

## Notes

- `Premium_LRS` is treated as V1
- `PremiumV2_LRS` is treated as V2
- other SKUs are shown but not migrated by default
- Azure can still reject migration based on subscription, VM, encryption, region, or workload constraints not fully detectable in advance

## Disclaimer & limitation of liability

This software is provided **"AS IS"**, without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement. The author and contributors shall not be held liable for any claim, damages, or other liability — including but not limited to data loss, service interruption, downtime, or financial impact — arising from, or in connection with, the use of, or inability to use, this software.

By using the **Migrate**, **Backup**, or **Delete** functionality you acknowledge that:

- you have the appropriate authorization to perform these actions on the target Azure resources;
- you have verified the selected scope and confirmed the impact with the affected workload owners;
- you have taken independent backups (snapshots, Azure Backup, or equivalent) before initiating destructive operations;
- the displayed eligibility status is a best-effort evaluation and may not capture every runtime constraint Azure enforces (region capacity, RBAC, ASR replication state, encryption configuration, etc.);
- migration and deletion actions are **irreversible without prior backups**.

Operators are solely responsible for the consequences of any action initiated through this tool. Before each action the dashboard displays an impact summary and requires the operator to type a confirmation phrase (`MIGRATE` or `DELETE`). Treat that prompt as the last opportunity to verify the change set.

## Author

Vikram Vunduru  
Portfolio: https://www.vikramvunduru.com  
For support, email: vikram.vunduru@gmail.com or hi@vikramvunduru.com
