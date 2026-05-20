import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


# ---------- helpers ----------

def make_disk(name="data1",
              sku="Premium_LRS",
              os_type=None,
              sector=512,
              bursting=None,
              encryption=None,
              tags=None,
              location="eastus2",
              disk_state="Unattached",
              resource_group="rg"):
    return {
        "id": f"/subscriptions/X/resourceGroups/{resource_group}/providers/Microsoft.Compute/disks/{name}",
        "name": name,
        "resourceGroup": resource_group,
        "location": location,
        "sku": {"name": sku},
        "logicalSectorSize": sector,
        "burstingEnabled": bursting,
        "encryption": encryption,
        "tags": tags or {},
        "osType": os_type,
        "diskState": disk_state,
    }


def make_vm_with_disks(vm_name="vm1", resource_group="rg", os_disk_id=None,
                      data_disks=None):
    return {
        "name": vm_name,
        "resourceGroup": resource_group,
        "storageProfile": {
            "osDisk": ({"managedDisk": {"id": os_disk_id}, "caching": "ReadOnly"}
                       if os_disk_id else {}),
            "dataDisks": data_disks or [],
        },
    }


def inventory_item(**overrides):
    base = {
        "subscriptionId": "X",
        "resourceGroup": "rg",
        "diskName": "d",
        "location": "eastus2",
        "sku": "Premium_LRS",
        "diskVersion": "V1",
        "diskState": "Unattached",
        "managedBy": None,
        "attached": False,
        "vmName": None,
        "isOsDisk": False,
        "osType": None,
        "lun": None,
        "caching": None,
        "logicalSectorSize": 512,
        "burstingEnabled": False,
        "doubleEncryptionEnabled": False,
        "asrEnabled": False,
        "unattached": True,
        "premiumV2RegionSupported": True,
        "id": "/subscriptions/X/resourceGroups/rg/providers/Microsoft.Compute/disks/d",
        "portalUrl": "https://portal.azure.com/...",
    }
    base.update(overrides)
    return base


# ---------- pure-function tests ----------

class TestGetDiskVersion(unittest.TestCase):
    def test_premium_lrs_is_v1(self):
        self.assertEqual(app.get_disk_version("Premium_LRS"), "V1")

    def test_premium_v2_lrs_is_v2(self):
        self.assertEqual(app.get_disk_version("PremiumV2_LRS"), "V2")

    def test_standard_lrs_is_other(self):
        self.assertEqual(app.get_disk_version("Standard_LRS"), "Other")

    def test_standard_ssd_is_other(self):
        self.assertEqual(app.get_disk_version("StandardSSD_LRS"), "Other")

    def test_empty_string_is_other(self):
        self.assertEqual(app.get_disk_version(""), "Other")


class TestGetDoubleEncryptionEnabled(unittest.TestCase):
    def test_single_platform_key_is_false(self):
        disk = {"encryption": {"type": "EncryptionAtRestWithPlatformKey"}}
        self.assertFalse(app.get_double_encryption_enabled(disk))

    def test_platform_and_customer_keys_is_true(self):
        disk = {"encryption": {"type": "EncryptionAtRestWithPlatformAndCustomerKeys"}}
        self.assertTrue(app.get_double_encryption_enabled(disk))

    def test_customer_managed_is_false(self):
        disk = {"encryption": {"type": "EncryptionAtRestWithCustomerKey"}}
        self.assertFalse(app.get_double_encryption_enabled(disk))

    def test_missing_encryption_block_is_false(self):
        self.assertFalse(app.get_double_encryption_enabled({}))

    def test_null_encryption_is_false(self):
        self.assertFalse(app.get_double_encryption_enabled({"encryption": None}))


class TestGetAsrEnabled(unittest.TestCase):
    def test_tag_key_contains_asr(self):
        self.assertTrue(app.get_asr_enabled({"tags": {"ASR": "Enabled"}}))

    def test_tag_value_contains_siterecovery(self):
        self.assertTrue(app.get_asr_enabled({"tags": {"replication": "SiteRecoveryReplica"}}))

    def test_recoveryservice_in_tag(self):
        self.assertTrue(app.get_asr_enabled({"tags": {"vault": "MyRecoveryServicesVault"}}))

    def test_unrelated_tags_returns_false(self):
        self.assertFalse(app.get_asr_enabled({"tags": {"env": "prod", "owner": "team"}}))

    def test_no_tags_returns_false(self):
        self.assertFalse(app.get_asr_enabled({}))

    def test_name_marker_asrseeddisk(self):
        self.assertTrue(app.get_asr_enabled({"name": "asrseeddisk-foo", "tags": {}}))

    def test_name_marker_dash_asr_dash(self):
        self.assertTrue(app.get_asr_enabled({"name": "my-asr-replica", "tags": {}}))


class TestComputeV2Status(unittest.TestCase):
    def test_region_supported_no_issues_is_green(self):
        self.assertEqual(app.compute_v2_status(True, False, False, False, False, False), "green")

    def test_region_false_returns_red(self):
        self.assertEqual(app.compute_v2_status(False, False, False, False, False, False), "red")

    def test_region_false_with_issues_still_red(self):
        self.assertEqual(app.compute_v2_status(False, True, True, True, True, True), "red")

    def test_sector_issue_yields_yellow(self):
        self.assertEqual(app.compute_v2_status(True, True, False, False, False, False), "yellow")

    def test_caching_issue_yields_yellow(self):
        self.assertEqual(app.compute_v2_status(True, False, True, False, False, False), "yellow")

    def test_unknown_region_no_issues_is_yellow(self):
        self.assertEqual(app.compute_v2_status(None, False, False, False, False, False), "yellow")

    def test_unknown_region_with_issues_is_yellow(self):
        self.assertEqual(app.compute_v2_status(None, True, False, False, False, False), "yellow")


# ---------- VM disk map ----------

class TestBuildVmDiskMap(unittest.TestCase):
    def test_os_disk_marked_correctly(self):
        vms = [make_vm_with_disks(os_disk_id="/SUBS/X/disks/osDisk1")]
        mapping = app.build_vm_disk_map(vms)
        self.assertIn("/subs/x/disks/osdisk1", mapping)
        self.assertTrue(mapping["/subs/x/disks/osdisk1"]["isOsDisk"])

    def test_data_disk_marked_correctly(self):
        vms = [make_vm_with_disks(
            data_disks=[{"managedDisk": {"id": "/subs/x/disks/data1"}, "lun": 0, "caching": "None"}]
        )]
        mapping = app.build_vm_disk_map(vms)
        self.assertIn("/subs/x/disks/data1", mapping)
        self.assertFalse(mapping["/subs/x/disks/data1"]["isOsDisk"])
        self.assertEqual(mapping["/subs/x/disks/data1"]["caching"], "None")
        self.assertEqual(mapping["/subs/x/disks/data1"]["lun"], 0)

    def test_empty_vm_list(self):
        self.assertEqual(app.build_vm_disk_map([]), {})

    def test_vm_with_no_storage_profile(self):
        vms = [{"name": "vm", "resourceGroup": "rg"}]
        self.assertEqual(app.build_vm_disk_map(vms), {})


# ---------- inventory building with mocked az ----------

class TestGetInventory(unittest.TestCase):
    @mock.patch("app.run_az_json")
    def test_detached_os_disk_detected_by_os_type(self, mock_az):
        """The bug fix: unattached disks with osType should still be marked as OS disks."""
        mock_az.side_effect = [
            [],  # vm list
            [{"name": "rg"}],  # group list
            [make_disk(name="PEPWDS33981-os-disk", os_type="Linux", disk_state="Unattached")],  # disk list rg
            set(["eastus2"]),  # region support is short-circuited but kept for safety
        ]
        inventory, _ = app.get_inventory("X", include_region_support=False)
        self.assertEqual(len(inventory), 1)
        self.assertTrue(inventory[0]["isOsDisk"], "detached OS disk should be marked isOsDisk")
        self.assertEqual(inventory[0]["osType"], "Linux")

    @mock.patch("app.run_az_json")
    def test_attached_os_disk_detected_via_vm_map(self, mock_az):
        disk = make_disk(name="osdisk1")
        vm = make_vm_with_disks(os_disk_id=disk["id"])
        mock_az.side_effect = [
            [vm],
            [{"name": "rg"}],
            [disk],
        ]
        inventory, _ = app.get_inventory("X", include_region_support=False)
        self.assertTrue(inventory[0]["isOsDisk"])
        self.assertEqual(inventory[0]["caching"], "ReadOnly")

    @mock.patch("app.run_az_json")
    def test_plain_data_disk_not_marked_os(self, mock_az):
        mock_az.side_effect = [
            [],
            [{"name": "rg"}],
            [make_disk(name="data1")],
        ]
        inventory, _ = app.get_inventory("X", include_region_support=False)
        self.assertFalse(inventory[0]["isOsDisk"])
        self.assertIsNone(inventory[0]["osType"])

    @mock.patch("app.run_az_json")
    def test_null_sector_normalized_to_512(self, mock_az):
        disk = make_disk(name="data1", sector=None)
        mock_az.side_effect = [
            [],
            [{"name": "rg"}],
            [disk],
        ]
        inventory, _ = app.get_inventory("X", include_region_support=False)
        self.assertEqual(inventory[0]["logicalSectorSize"], 512)


# ---------- Premium LRS data disk view ----------

class TestBuildPremiumLrsDataView(unittest.TestCase):
    def test_clean_disk_is_green(self):
        rows = app.build_premium_lrs_data_view([inventory_item()])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "green")
        self.assertEqual(rows[0]["notes"], "Ready for direct conversion")

    def test_os_disk_excluded(self):
        rows = app.build_premium_lrs_data_view([inventory_item(isOsDisk=True)])
        self.assertEqual(rows, [])

    def test_detached_os_disk_with_os_type_excluded(self):
        # The exact bug from the screenshot: detached OS disk should not appear.
        rows = app.build_premium_lrs_data_view([inventory_item(isOsDisk=True, osType="Linux", unattached=True)])
        self.assertEqual(rows, [])

    def test_non_premium_lrs_excluded(self):
        for sku in ["Standard_LRS", "StandardSSD_LRS", "StandardSSD_ZRS", "PremiumV2_LRS"]:
            with self.subTest(sku=sku):
                rows = app.build_premium_lrs_data_view([inventory_item(sku=sku)])
                self.assertEqual(rows, [])

    def test_caching_readwrite_yields_yellow(self):
        rows = app.build_premium_lrs_data_view([inventory_item(caching="ReadWrite")])
        self.assertEqual(rows[0]["status"], "yellow")
        self.assertTrue(rows[0]["hostCachingEnabled"])
        self.assertIn("Host caching", rows[0]["notes"])

    def test_caching_none_stays_green(self):
        rows = app.build_premium_lrs_data_view([inventory_item(caching="None")])
        self.assertEqual(rows[0]["status"], "green")
        self.assertFalse(rows[0]["hostCachingEnabled"])

    def test_bursting_enabled_yields_yellow(self):
        rows = app.build_premium_lrs_data_view([inventory_item(burstingEnabled=True)])
        self.assertEqual(rows[0]["status"], "yellow")
        self.assertTrue(rows[0]["burstingEnabled"])

    def test_double_encryption_yields_yellow(self):
        rows = app.build_premium_lrs_data_view([inventory_item(doubleEncryptionEnabled=True)])
        self.assertEqual(rows[0]["status"], "yellow")

    def test_asr_yields_yellow(self):
        rows = app.build_premium_lrs_data_view([inventory_item(asrEnabled=True)])
        self.assertEqual(rows[0]["status"], "yellow")

    def test_sector_4096_yields_yellow(self):
        rows = app.build_premium_lrs_data_view([inventory_item(logicalSectorSize=4096)])
        self.assertEqual(rows[0]["status"], "yellow")
        self.assertTrue(rows[0]["sectorNot512"])

    def test_unsupported_region_yields_red(self):
        rows = app.build_premium_lrs_data_view([inventory_item(premiumV2RegionSupported=False)])
        self.assertEqual(rows[0]["status"], "red")

    def test_unknown_region_yields_yellow_conservative(self):
        rows = app.build_premium_lrs_data_view([inventory_item(premiumV2RegionSupported=None)])
        self.assertEqual(rows[0]["status"], "yellow")

    def test_red_overrides_all_issues(self):
        rows = app.build_premium_lrs_data_view([
            inventory_item(premiumV2RegionSupported=False,
                          caching="ReadWrite",
                          burstingEnabled=True)
        ])
        self.assertEqual(rows[0]["status"], "red")


# ---------- migration plan ----------

class TestGetMigrationPlan(unittest.TestCase):
    def test_green_v1_data_disk_is_eligible(self):
        plan = app.get_migration_plan([inventory_item()])
        self.assertTrue(plan[0]["eligible"])
        self.assertEqual(plan[0]["plannedSku"], "PremiumV2_LRS")

    def test_v2_disk_is_not_eligible(self):
        plan = app.get_migration_plan([inventory_item(sku="PremiumV2_LRS", diskVersion="V2")])
        self.assertFalse(plan[0]["eligible"])
        self.assertIn("already Premium SSD v2", plan[0]["reasons"])

    def test_os_disk_is_not_eligible(self):
        plan = app.get_migration_plan([inventory_item(isOsDisk=True)])
        self.assertFalse(plan[0]["eligible"])
        self.assertIn("OS disk", plan[0]["reasons"])

    def test_non_premium_lrs_is_not_eligible(self):
        plan = app.get_migration_plan([inventory_item(sku="Standard_LRS", diskVersion="Other")])
        self.assertFalse(plan[0]["eligible"])

    def test_caching_blocks_eligibility(self):
        plan = app.get_migration_plan([inventory_item(caching="ReadWrite")])
        self.assertFalse(plan[0]["eligible"])

    def test_unsupported_region_blocks_eligibility(self):
        plan = app.get_migration_plan([inventory_item(premiumV2RegionSupported=False)])
        self.assertFalse(plan[0]["eligible"])


# ---------- migrate_disks ----------

class TestMigrateDisks(unittest.TestCase):
    @mock.patch("app.run_az_json")
    def test_unattached_green_disk_migrates_via_sku_update(self, mock_az):
        disk_json = make_disk(name="data1", disk_state="Unattached")
        mock_az.side_effect = [
            [],         # vm list
            disk_json,  # disk show
            {},         # disk update
        ]
        log = []
        result = app.migrate_disks(
            "X",
            [{"resourceGroup": "rg", "diskName": "data1"}],
            create_backup_before=False,
            log=log,
        )
        self.assertEqual(result["migratedCount"], 1)
        self.assertEqual(result["snapshotCount"], 0)
        # Confirm last az call was disk update --sku PremiumV2_LRS
        last_call = mock_az.call_args_list[-1][0][0]
        self.assertIn("disk", last_call)
        self.assertIn("update", last_call)
        self.assertIn("PremiumV2_LRS", last_call)
        # Activity log captured at least one completion entry
        self.assertTrue(any("Migrated" in e["message"] for e in log))

    @mock.patch("app.run_az_json")
    def test_migrate_with_backup_creates_snapshot(self, mock_az):
        disk_json = make_disk(name="data1")
        mock_az.side_effect = [
            [],         # vm list
            disk_json,  # disk show
            {},         # snapshot create
            {},         # disk update
        ]
        result = app.migrate_disks(
            "X",
            [{"resourceGroup": "rg", "diskName": "data1"}],
            create_backup_before=True,
        )
        self.assertEqual(result["migratedCount"], 1)
        self.assertEqual(result["snapshotCount"], 1)
        commands_run = [call[0][0][0:2] for call in mock_az.call_args_list]
        self.assertIn(["snapshot", "create"], commands_run)

    @mock.patch("app.run_az_json")
    def test_migrate_rejects_os_disk(self, mock_az):
        # disk show returns a Linux OS disk
        disk_json = make_disk(name="osdisk1", os_type="Linux")
        mock_az.side_effect = [
            [],         # vm list
            disk_json,  # disk show
        ]
        with self.assertRaises(RuntimeError) as ctx:
            app.migrate_disks(
                "X",
                [{"resourceGroup": "rg", "diskName": "osdisk1"}],
            )
        self.assertIn("OS disk", str(ctx.exception))

    @mock.patch("app.run_az_json")
    def test_migrate_rejects_non_premium_lrs(self, mock_az):
        disk_json = make_disk(name="d", sku="Standard_LRS")
        mock_az.side_effect = [[], disk_json]
        with self.assertRaises(RuntimeError) as ctx:
            app.migrate_disks("X", [{"resourceGroup": "rg", "diskName": "d"}])
        self.assertIn("not Premium_LRS", str(ctx.exception))

    @mock.patch("app.run_az_json")
    def test_migrate_rejects_bursting(self, mock_az):
        disk_json = make_disk(name="d", bursting=True)
        mock_az.side_effect = [[], disk_json]
        with self.assertRaises(RuntimeError) as ctx:
            app.migrate_disks("X", [{"resourceGroup": "rg", "diskName": "d"}])
        self.assertIn("bursting", str(ctx.exception).lower())

    @mock.patch("app.run_az_json")
    def test_migrate_rejects_bad_sector(self, mock_az):
        disk_json = make_disk(name="d", sector=4096)
        mock_az.side_effect = [[], disk_json]
        with self.assertRaises(RuntimeError) as ctx:
            app.migrate_disks("X", [{"resourceGroup": "rg", "diskName": "d"}])
        self.assertIn("sector", str(ctx.exception).lower())

    @mock.patch("app.run_az_json")
    def test_attached_disk_deallocates_and_restarts_vm(self, mock_az):
        disk_json = make_disk(name="data1")
        vm_json = make_vm_with_disks(
            vm_name="vm1",
            data_disks=[{"managedDisk": {"id": disk_json["id"]}, "lun": 0, "caching": "None"}],
        )
        mock_az.side_effect = [
            [vm_json],   # vm list
            disk_json,   # disk show
            {},          # vm deallocate
            {},          # disk update
            {},          # vm start (in finally)
        ]
        result = app.migrate_disks(
            "X",
            [{"resourceGroup": "rg", "diskName": "data1"}],
            create_backup_before=False,
        )
        self.assertEqual(result["migratedCount"], 1)
        cmds = [call[0][0][0:2] for call in mock_az.call_args_list]
        self.assertIn(["vm", "deallocate"], cmds)
        self.assertIn(["vm", "start"], cmds)

    @mock.patch("app.run_az_json")
    def test_migrate_rejects_attached_disk_with_caching(self, mock_az):
        disk_json = make_disk(name="data1")
        vm_json = make_vm_with_disks(
            vm_name="vm1",
            data_disks=[{"managedDisk": {"id": disk_json["id"]}, "lun": 0, "caching": "ReadWrite"}],
        )
        mock_az.side_effect = [[vm_json], disk_json]
        with self.assertRaises(RuntimeError) as ctx:
            app.migrate_disks(
                "X",
                [{"resourceGroup": "rg", "diskName": "data1"}],
            )
        self.assertIn("caching", str(ctx.exception).lower())


# ---------- backup_disks ----------

class TestBackupDisks(unittest.TestCase):
    @mock.patch("app.run_az_json")
    def test_backup_creates_one_snapshot_per_disk(self, mock_az):
        disk_json = make_disk(name="data1")
        mock_az.side_effect = [
            disk_json,   # disk show
            {},          # snapshot create
        ]
        result = app.backup_disks("X", [{"resourceGroup": "rg", "diskName": "data1"}])
        self.assertEqual(result["snapshotCount"], 1)
        cmds = [call[0][0][0:2] for call in mock_az.call_args_list]
        self.assertIn(["snapshot", "create"], cmds)

    @mock.patch("app.run_az_json")
    def test_backup_two_disks(self, mock_az):
        disk1 = make_disk(name="d1")
        disk2 = make_disk(name="d2")
        mock_az.side_effect = [disk1, {}, disk2, {}]
        result = app.backup_disks(
            "X",
            [{"resourceGroup": "rg", "diskName": "d1"},
             {"resourceGroup": "rg", "diskName": "d2"}],
        )
        self.assertEqual(result["snapshotCount"], 2)

    def test_backup_missing_disk_fields_raises(self):
        with self.assertRaises(RuntimeError):
            app.backup_disks("X", [{"resourceGroup": None, "diskName": None}])


# ---------- delete_unattached_disks ----------

class TestDeleteUnattachedDisks(unittest.TestCase):
    @mock.patch("app.run_az_json")
    def test_delete_unattached_succeeds(self, mock_az):
        disk_json = make_disk(name="data1", disk_state="Unattached")
        mock_az.side_effect = [
            disk_json,   # disk show
            {},          # disk delete
        ]
        result = app.delete_unattached_disks(
            "X",
            [{"resourceGroup": "rg", "diskName": "data1"}],
        )
        self.assertEqual(result["deletedCount"], 1)

    @mock.patch("app.run_az_json")
    def test_delete_refuses_attached_disk(self, mock_az):
        disk_json = make_disk(name="d", disk_state="Attached")
        disk_json["managedBy"] = "/subs/x/vms/vm1"
        mock_az.side_effect = [disk_json]
        with self.assertRaises(RuntimeError) as ctx:
            app.delete_unattached_disks(
                "X",
                [{"resourceGroup": "rg", "diskName": "d"}],
            )
        self.assertIn("attached", str(ctx.exception).lower())

    def test_delete_missing_disk_fields_raises(self):
        with self.assertRaises(RuntimeError):
            app.delete_unattached_disks("X", [{"resourceGroup": None, "diskName": None}])


# ---------- operation registry / log ----------

class TestLog(unittest.TestCase):
    def test_log_appends_entry_with_timestamp(self):
        entries = []
        app._log(entries, "hello world")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"], "hello world")
        self.assertIn("ts", entries[0])

    def test_log_with_none_is_safe(self):
        # should not raise
        app._log(None, "anything")


class TestOperationRegistry(unittest.TestCase):
    def test_start_and_complete(self):
        def good(arg, log):
            app._log(log, f"got {arg}")
            return {"ok": True, "arg": arg}

        op_id = app._start_operation(good, ["hello"])
        # Poll briefly until complete (background thread)
        deadline = time.time() + 2
        status = app._get_operation_status(op_id)
        while status["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
            status = app._get_operation_status(op_id)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["result"]["arg"], "hello")
        self.assertTrue(any("got hello" in e["message"] for e in status["log"]))

    def test_start_and_fail(self):
        def bad(log):
            app._log(log, "about to fail")
            raise RuntimeError("boom")

        op_id = app._start_operation(bad, [])
        deadline = time.time() + 2
        status = app._get_operation_status(op_id)
        while status["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
            status = app._get_operation_status(op_id)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "boom")
        self.assertTrue(any("ERROR" in e["message"] for e in status["log"]))

    def test_get_status_unknown_op(self):
        self.assertIsNone(app._get_operation_status("not-a-real-op-id"))


# ---------- safety / hardening ----------

class TestIsStateChanging(unittest.TestCase):
    def test_disk_update_is_state_changing(self):
        self.assertTrue(app.is_state_changing(["disk", "update", "--name", "d"]))

    def test_disk_delete_is_state_changing(self):
        self.assertTrue(app.is_state_changing(["disk", "delete", "--name", "d"]))

    def test_snapshot_create_is_state_changing(self):
        self.assertTrue(app.is_state_changing(["snapshot", "create"]))

    def test_vm_deallocate_is_state_changing(self):
        self.assertTrue(app.is_state_changing(["vm", "deallocate"]))

    def test_disk_list_is_read_only(self):
        self.assertFalse(app.is_state_changing(["disk", "list"]))

    def test_vm_list_is_read_only(self):
        self.assertFalse(app.is_state_changing(["vm", "list"]))

    def test_empty_is_safe(self):
        self.assertFalse(app.is_state_changing([]))


class TestDryRunMode(unittest.TestCase):
    def test_dry_run_returns_stub_for_state_change(self):
        with mock.patch.object(app, "DRY_RUN_MODE", True):
            result = app.run_az_json(["disk", "update", "--name", "d"])
        self.assertEqual(result, {"dryRun": True, "command": ["disk", "update", "--name", "d"]})

    def test_dry_run_does_not_short_circuit_reads(self):
        # Read-only commands should still go through subprocess.run.
        with mock.patch.object(app, "DRY_RUN_MODE", True), \
             mock.patch("app.subprocess.run") as mock_run, \
             mock.patch("app.shutil.which", return_value="/usr/bin/az"):
            mock_run.return_value = mock.Mock(returncode=0, stdout="[]", stderr="")
            result = app.run_az_json(["disk", "list"])
        self.assertEqual(result, [])
        mock_run.assert_called_once()


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.object(app, "AUDIT_DIR", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_audit_writes_one_line_per_call(self):
        path = app.write_audit_entry(
            "migrate",
            "subX",
            [{"resourceGroup": "rg", "diskName": "d1"}],
            result={"migratedCount": 1, "snapshotCount": 0},
            dry_run=False,
        )
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            line = fh.readline().strip()
        entry = json.loads(line)
        self.assertEqual(entry["action"], "migrate")
        self.assertEqual(entry["subscriptionId"], "subX")
        self.assertEqual(entry["diskCount"], 1)
        self.assertFalse(entry["dryRun"])
        self.assertEqual(entry["disks"][0]["diskName"], "d1")
        self.assertIn("ts", entry)

    def test_audit_records_error_field(self):
        path = app.write_audit_entry(
            "delete", "subX",
            [{"resourceGroup": "rg", "diskName": "d"}],
            error="Disk is attached",
        )
        with open(path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["error"], "Disk is attached")
        self.assertIsNone(entry["result"])

    def test_audit_appends_multiple_lines(self):
        app.write_audit_entry("backup", "subX", [{"resourceGroup": "rg", "diskName": "d1"}])
        app.write_audit_entry("backup", "subX", [{"resourceGroup": "rg", "diskName": "d2"}])
        date_str = time.strftime("%Y-%m-%d", time.gmtime())
        path = os.path.join(self.tmp, f"audit-{date_str}.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 2)

    def test_audit_failure_does_not_raise(self):
        # Force makedirs to fail by using a temp file as the "directory".
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            blocking_path = fh.name
        try:
            broken_dir = os.path.join(blocking_path, "subdir-that-cant-exist")
            with mock.patch.object(app, "AUDIT_DIR", broken_dir):
                # Should not raise even though we can't create the directory.
                result = app.write_audit_entry("migrate", "x", [])
            self.assertIsNone(result)
        finally:
            os.unlink(blocking_path)


class TestBatchSizeCap(unittest.TestCase):
    def test_migrate_rejects_batch_above_cap(self):
        disks = [{"resourceGroup": "rg", "diskName": f"d{i}"} for i in range(3)]
        with mock.patch.object(app, "MAX_MIGRATION_BATCH", 2):
            with self.assertRaises(RuntimeError) as ctx:
                app.migrate_disks("subX", disks)
        self.assertIn("Batch size", str(ctx.exception))

    def test_backup_rejects_batch_above_cap(self):
        disks = [{"resourceGroup": "rg", "diskName": f"d{i}"} for i in range(3)]
        with mock.patch.object(app, "MAX_MIGRATION_BATCH", 2):
            with self.assertRaises(RuntimeError) as ctx:
                app.backup_disks("subX", disks)
        self.assertIn("Batch size", str(ctx.exception))

    def test_delete_rejects_batch_above_cap(self):
        disks = [{"resourceGroup": "rg", "diskName": f"d{i}"} for i in range(3)]
        with mock.patch.object(app, "MAX_MIGRATION_BATCH", 2):
            with self.assertRaises(RuntimeError) as ctx:
                app.delete_unattached_disks("subX", disks)
        self.assertIn("Batch size", str(ctx.exception))


class TestEnvBoolHelper(unittest.TestCase):
    def test_true_values(self):
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            with mock.patch.dict(os.environ, {"TEST_FLAG_X": value}):
                self.assertTrue(app._env_bool("TEST_FLAG_X"), value)

    def test_false_values(self):
        for value in ("", "0", "false", "no", "off", "anything-else"):
            with mock.patch.dict(os.environ, {"TEST_FLAG_X": value}):
                self.assertFalse(app._env_bool("TEST_FLAG_X"), value)


class TestMigrateEndToEndAuditAndDryRun(unittest.TestCase):
    @mock.patch("app.run_az_json")
    def test_audit_entry_written_after_successful_migrate(self, mock_az):
        tmp = tempfile.mkdtemp()
        disk_json = make_disk(name="data1")
        mock_az.side_effect = [
            [],         # vm list
            disk_json,  # disk show
            {},         # disk update
        ]
        with mock.patch.object(app, "AUDIT_DIR", tmp):
            app.migrate_disks(
                "subX",
                [{"resourceGroup": "rg", "diskName": "data1"}],
                create_backup_before=False,
            )
        date_str = time.strftime("%Y-%m-%d", time.gmtime())
        path = os.path.join(tmp, f"audit-{date_str}.jsonl")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["action"], "migrate")
        self.assertEqual(entry["result"]["migratedCount"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
