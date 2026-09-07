"""Multi-device registry and per-device PIN management for Chime automation."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DEVICES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "devices.json",
)


@dataclass
class ChimeDevice:
    """Represents a registered cloud phone device running a Chime account."""

    device_id: str
    serial: str
    name: str
    pin: Optional[str] = None
    enabled: bool = True
    latest_balance: Optional[str] = None
    last_synced_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> ChimeDevice:
        """Construct ChimeDevice from dictionary."""
        return cls(
            device_id=str(data.get("device_id", "")),
            serial=str(data.get("serial", "")),
            name=str(data.get("name", "")),
            pin=data.get("pin"),
            enabled=bool(data.get("enabled", True)),
            latest_balance=data.get("latest_balance"),
            last_synced_at=float(data.get("last_synced_at", 0.0)),
        )

    def to_dict(self) -> dict:
        """Convert ChimeDevice to dictionary."""
        return asdict(self)


class DeviceManager:
    """Manages multi-device configuration, PIN assignments, and atomic disk persistence."""

    def __init__(
        self,
        storage_path: Optional[str] = None,
        seed_config: Optional[object] = None,
        db: Optional[object] = None,
    ) -> None:
        self.storage_path = storage_path or DEFAULT_DEVICES_PATH
        self.db = db
        self._lock = threading.Lock()
        self._devices: Dict[str, ChimeDevice] = {}  # Key: device_id
        self._load(seed_config)

    def _load(self, seed_config: Optional[object] = None) -> None:
        """Load registered devices from storage, or seed with initial device."""
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)

        if os.path.exists(self.storage_path) and os.path.getsize(self.storage_path) > 0:
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    raw_list = json.load(f)
                    for item in raw_list:
                        dev = ChimeDevice.from_dict(item)
                        if dev.device_id:
                            self._devices[dev.device_id] = dev
                logger.info("Loaded %d device(s) from %s", len(self._devices), self.storage_path)
                return
            except Exception as e:
                logger.error("Error loading devices from %s: %s", self.storage_path, e)

        # Seed with initial device from config if file not found or empty
        if seed_config:
            dev_id = getattr(seed_config, "target_phone_id", None) or "635090487589470334"
            serial = getattr(seed_config, "target_phone_serial", None) or "226"
            name = getattr(seed_config, "target_phone_name", None) or "Katie-Smith-18"
            pin = getattr(seed_config, "app_pin", None) or "1122"

            initial_device = ChimeDevice(
                device_id=dev_id,
                serial=serial,
                name=name,
                pin=pin,
                enabled=True,
            )
            self._devices[dev_id] = initial_device
            self._save()
            logger.info("Seeded initial device #%s (%s) into %s", serial, name, self.storage_path)

    def _save(self) -> None:
        """Atomically persist device registry to disk."""
        dir_name = os.path.dirname(self.storage_path)
        os.makedirs(dir_name, exist_ok=True)

        data = [dev.to_dict() for dev in self._devices.values()]
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(data, tf, indent=2)
                temp_name = tf.name
            os.replace(temp_name, self.storage_path)
        except Exception as e:
            logger.error("Failed to atomically save devices to %s: %s", self.storage_path, e)

    def get_all_devices(self, enabled_only: bool = False) -> List[ChimeDevice]:
        """Return list of all registered devices, synced with database if available."""
        with self._lock:
            if self.db and hasattr(self.db, "get_all_tenant_devices"):
                try:
                    db_devs = self.db.get_all_tenant_devices(enabled_only=False)
                    if db_devs:
                        new_devices = {}
                        for td in db_devs:
                            new_devices[td.device_id] = ChimeDevice(
                                device_id=td.device_id,
                                serial=td.serial,
                                name=td.name,
                                pin=td.pin,
                                enabled=td.enabled,
                                latest_balance=td.latest_balance,
                                last_synced_at=td.last_synced_at,
                            )
                        self._devices = new_devices
                        self._save()
                except Exception as e:
                    logger.debug("Database sync in get_all_devices skipped: %s", e)

            if enabled_only:
                return [d for d in self._devices.values() if d.enabled]
            return list(self._devices.values())

    def get_device(self, identifier: str) -> Optional[ChimeDevice]:
        """Lookup device by ID, serial, or name."""
        if not identifier:
            return None
        target = identifier.strip().lower()
        with self._lock:
            # 1. Exact ID match
            if identifier in self._devices:
                return self._devices[identifier]
            # 2. Match by serial
            for dev in self._devices.values():
                if dev.serial.lower() == target or dev.serial.lstrip("#").lower() == target.lstrip("#"):
                    return dev
            # 3. Match by name
            for dev in self._devices.values():
                if target in dev.name.lower():
                    return dev

            # 4. Fallback lookup in database if not found in memory
            if self.db and hasattr(self.db, "get_device_by_id_or_serial"):
                try:
                    td = self.db.get_device_by_id_or_serial(identifier)
                    if td:
                        dev = ChimeDevice(
                            device_id=td.device_id,
                            serial=td.serial,
                            name=td.name,
                            pin=td.pin,
                            enabled=td.enabled,
                            latest_balance=td.latest_balance,
                            last_synced_at=td.last_synced_at,
                        )
                        self._devices[dev.device_id] = dev
                        return dev
                except Exception as e:
                    logger.debug("Database lookup in get_device skipped: %s", e)

        return None

    def add_or_update_device(self, device: ChimeDevice) -> None:
        """Add new device or update existing device configuration."""
        with self._lock:
            self._devices[device.device_id] = device
            self._save()
        logger.info("Saved device %s (#%s) with PIN %s", device.name, device.serial, "***" if device.pin else "None")

    def set_device_pin(self, identifier: str, pin: str) -> Optional[ChimeDevice]:
        """Update the Chime passcode/PIN for a specific device."""
        device = self.get_device(identifier)
        if not device:
            return None
        with self._lock:
            device.pin = pin.strip()
            self._devices[device.device_id] = device
            self._save()

            # Sync to database if available
            if self.db and hasattr(self.db, "get_device_tenant"):
                try:
                    tenant = self.db.get_device_tenant(device.device_id)
                    if tenant:
                        self.db.set_device_pin(tenant.id, device.device_id, device.pin)
                except Exception as e:
                    logger.debug("Database PIN sync skipped: %s", e)

        logger.info("Updated PIN for device #%s (%s)", device.serial, device.name)
        return device

    def update_balance(self, device_id: str, balance: str) -> None:
        """Update cached balance and timestamp for device."""
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id].latest_balance = balance
                self._devices[device_id].last_synced_at = time.time()
                self._save()

    def toggle_device_enabled(self, identifier: str, state: Optional[bool] = None) -> Optional[ChimeDevice]:
        """Toggle or set the enabled/monitoring status for a device."""
        device = self.get_device(identifier)
        if not device:
            return None
        with self._lock:
            device.enabled = not device.enabled if state is None else bool(state)
            self._devices[device.device_id] = device
            self._save()

            # Sync to database if available
            if self.db and hasattr(self.db, "get_device_tenant"):
                try:
                    tenant = self.db.get_device_tenant(device.device_id)
                    if tenant:
                        self.db.toggle_device_enabled(tenant.id, device.device_id, state=device.enabled)
                except Exception as e:
                    logger.debug("Database toggle sync skipped: %s", e)

        logger.info(
            "Device #%s (%s) monitoring status set to: %s",
            device.serial,
            device.name,
            "CONNECTED/ENABLED" if device.enabled else "DISCONNECTED/PAUSED",
        )
        return device

    def set_all_devices_enabled(self, enabled: bool) -> List[ChimeDevice]:
        """Atomically set the enabled/monitoring status for ALL registered devices."""
        with self._lock:
            for dev in self._devices.values():
                dev.enabled = enabled
            self._save()
        logger.info("All %d device(s) set to monitoring=%s", len(self._devices), enabled)
        return self.get_all_devices()

    def get_device_summary(self) -> dict:
        """Return summary statistics of registered devices."""
        with self._lock:
            total = len(self._devices)
            enabled = sum(1 for d in self._devices.values() if d.enabled)
            pinned = sum(1 for d in self._devices.values() if d.pin)
            return {
                "total": total,
                "enabled": enabled,
                "disabled": total - enabled,
                "pinned": pinned,
                "unpinned": total - pinned,
            }

    def sync_with_geelark(self, phone_mgr: object) -> List[ChimeDevice]:
        """Discover phones from GeeLark API and register new devices automatically."""
        try:
            page = 1
            all_items = []
            while True:
                res = phone_mgr.list_phones(page=page, page_size=100)
                items = res.get("items", [])
                if not items:
                    break
                all_items.extend(items)
                total = res.get("total", len(items))
                if len(all_items) >= total or len(items) < 100:
                    break
                page += 1

            with self._lock:
                for item in all_items:
                    p_id = str(item.get("id", ""))
                    if not p_id:
                        continue
                    serial = str(item.get("serialNo", ""))
                    name = str(item.get("serialName", f"Phone-{serial}"))

                    if p_id in self._devices:
                        self._devices[p_id].name = name
                        self._devices[p_id].serial = serial
                    else:
                        self._devices[p_id] = ChimeDevice(
                            device_id=p_id,
                            serial=serial,
                            name=name,
                            pin=None,
                            enabled=False,  # Disconnected by default until user connects it
                        )
                        logger.info("Auto-registered new cloud phone: #%s (%s) ID: %s (default: Disconnected)", serial, name, p_id)
                self._save()
        except Exception as e:
            logger.error("Error syncing devices with GeeLark: %s", e)

        return self.get_all_devices()
