"""Unit tests for multi-device manager and PIN assignments."""

import os
import tempfile
import unittest
from dataclasses import dataclass
from typing import Optional

from automation.device_manager import ChimeDevice, DeviceManager


@dataclass
class DummyConfig:
    target_phone_id: str = "dev_101"
    target_phone_serial: str = "226"
    target_phone_name: str = "Katie-Smith-18"
    app_pin: str = "1122"


class TestDeviceManager(unittest.TestCase):
    """Test suite for DeviceManager."""

    def setUp(self) -> None:
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_path = self.temp_file.name
        self.temp_file.close()

    def tearDown(self) -> None:
        if os.path.exists(self.temp_path):
            os.remove(self.temp_path)

    def test_seed_from_config(self) -> None:
        mgr = DeviceManager(storage_path=self.temp_path, seed_config=DummyConfig())
        devices = mgr.get_all_devices()
        self.assertEqual(len(devices), 1)

        dev = devices[0]
        self.assertEqual(dev.device_id, "dev_101")
        self.assertEqual(dev.serial, "226")
        self.assertEqual(dev.name, "Katie-Smith-18")
        self.assertEqual(dev.pin, "1122")
        self.assertTrue(dev.enabled)

    def test_lookup_device_by_serial_id_name(self) -> None:
        mgr = DeviceManager(storage_path=self.temp_path, seed_config=DummyConfig())
        dev2 = ChimeDevice(
            device_id="dev_102",
            serial="227",
            name="Alice-Chime",
            pin="3344",
        )
        mgr.add_or_update_device(dev2)

        # Lookup by ID
        self.assertIsNotNone(mgr.get_device("dev_102"))
        # Lookup by serial
        self.assertEqual(mgr.get_device("227").name, "Alice-Chime")
        self.assertEqual(mgr.get_device("#227").name, "Alice-Chime")
        # Lookup by partial name
        self.assertEqual(mgr.get_device("Alice").device_id, "dev_102")

    def test_set_device_pin(self) -> None:
        mgr = DeviceManager(storage_path=self.temp_path, seed_config=DummyConfig())
        # Set PIN by serial
        updated = mgr.set_device_pin("226", "9988")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.pin, "9988")

        # Reload from disk and verify persistence
        mgr2 = DeviceManager(storage_path=self.temp_path)
        reloaded = mgr2.get_device("226")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.pin, "9988")

    def test_update_balance(self) -> None:
        mgr = DeviceManager(storage_path=self.temp_path, seed_config=DummyConfig())
        mgr.update_balance("dev_101", "$540.25")

        dev = mgr.get_device("dev_101")
        self.assertEqual(dev.latest_balance, "$540.25")
        self.assertGreater(dev.last_synced_at, 0.0)

    def test_toggle_device_enabled(self) -> None:
        mgr = DeviceManager(storage_path=self.temp_path, seed_config=DummyConfig())
        dev = mgr.get_device("dev_101")
        self.assertTrue(dev.enabled)

        # Toggle to disabled (paused)
        updated = mgr.toggle_device_enabled("dev_101")
        self.assertIsNotNone(updated)
        self.assertFalse(updated.enabled)

        # Toggle back to enabled
        updated2 = mgr.toggle_device_enabled("dev_101")
        self.assertTrue(updated2.enabled)


if __name__ == "__main__":
    unittest.main()
