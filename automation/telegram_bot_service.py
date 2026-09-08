"""Interactive Telegram Bot Listener for manual refresh and device controls."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Dict, List, Any

import requests

from automation.chime_monitor import ChimeMonitor
from automation.device_manager import ChimeDevice
from automation.transaction_parser import TransactionParser
from config import Config
from database.db import Database, Tenant, TelegramSubscriber
from geelark.client import GeeLarkClient
from geelark.phone import PhoneManager
from telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Long-polling bot listener for user button clicks and commands."""

    def __init__(self, monitor: ChimeMonitor, config: Optional[Config] = None, db: Optional[Database] = None) -> None:
        self.monitor = monitor
        self.config = config or monitor.config
        self.notifier = monitor.notifier
        self.bot_token = self.config.telegram_bot_token
        self.allowed_chat_id = str(self.config.telegram_chat_id)
        self.db = db or Database()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=15, pool_maxsize=15, max_retries=3)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.last_update_id = 0
        self.is_running = False
        self.user_active_device: Dict[str, str] = {}
        self._active_device_id_fallback: Optional[str] = None
        self._pending_requests: set[str] = set()

    @property
    def active_device_id(self) -> Optional[str]:
        return self._active_device_id_fallback

    @active_device_id.setter
    def active_device_id(self, val: Optional[str]) -> None:
        self._active_device_id_fallback = val

    def check_access(self, chat_id: str) -> Optional[tuple[Tenant, TelegramSubscriber]]:
        """Verify chat_id is authorized. Returns (tenant, subscriber) or None."""
        chat_id_str = str(chat_id).strip()
        dummy_tenant = Tenant(
            id="default", name="Primary", contact=""
        )
        dummy_sub = TelegramSubscriber(
            id="default", tenant_id="default", chat_id=chat_id_str,
            username="User", role="full_controller", receive_alerts=True
        )

        # If allowed_chat_id is configured and matches, grant full access
        if self.allowed_chat_id and chat_id_str == self.allowed_chat_id:
            return dummy_tenant, dummy_sub

        # Check DB if subscriber registered
        try:
            sub = self.db.get_subscriber_by_chat_id(chat_id_str)
            if sub:
                tenant = self.db.get_tenant(sub.tenant_id)
                if tenant:
                    return tenant, sub
        except Exception:
            pass

        # If no allowed_chat_id set, allow by default
        if not self.allowed_chat_id:
            return dummy_tenant, dummy_sub

        return None

    def is_admin(self, chat_id: Optional[str]) -> bool:
        """Check if chat_id has admin/controller privileges."""
        if not chat_id:
            return False
        c_id = str(chat_id).strip()
        if self.allowed_chat_id and c_id == self.allowed_chat_id:
            return True
        try:
            sub = self.db.get_subscriber_by_chat_id(c_id)
            if sub and sub.is_active and sub.role == "full_controller":
                return True
        except Exception as e:
            logger.debug("Error checking admin privilege for %s: %s", c_id, e)
        return False

    def get_all_admins(self) -> List[str]:
        """Return list of all authorized admin chat IDs."""
        admins = set()
        if self.allowed_chat_id:
            admins.add(str(self.allowed_chat_id).strip())
        try:
            subs = self.db.get_all_subscribers(active_only=True)
            for s in subs:
                if s.role == "full_controller" and s.chat_id:
                    admins.add(str(s.chat_id).strip())
        except Exception as e:
            logger.debug("Error retrieving admin subscribers: %s", e)
        return list(admins)

    def get_tenant_geelark_client(self, chat_id: Optional[str] = None) -> tuple[GeeLarkClient, PhoneManager]:
        """Get (GeeLarkClient, PhoneManager) initialized for this service."""
        return self.monitor.client, self.monitor.phone_mgr

    def get_tenant_phone_manager(self, chat_id: Optional[str] = None) -> PhoneManager:
        """Get PhoneManager for this service."""
        return self.monitor.phone_mgr

    def get_tenant_devices(self, chat_id: Optional[str] = None) -> List[ChimeDevice]:
        """Fetch all devices managed by this GeeLark service."""
        return self.monitor.device_mgr.get_all_devices()

    def get_active_device(self, chat_id: Optional[str] = None) -> Optional[ChimeDevice]:
        """Get currently selected active ChimeDevice isolated strictly per tenant."""
        c_key = str(chat_id).strip() if chat_id else None
        tenant_devs = self.get_tenant_devices(c_key) if c_key else self.monitor.device_mgr.get_all_devices()

        if not tenant_devs:
            return None

        # 1. User-specific selected device within tenant devices
        if c_key and c_key in self.user_active_device:
            target_id = self.user_active_device[c_key]
            for d in tenant_devs:
                if d.device_id == target_id or d.serial == target_id:
                    return d

        # 2. Pick first enabled device for this tenant
        enabled_devs = [d for d in tenant_devs if d.enabled]
        chosen = enabled_devs[0] if enabled_devs else tenant_devs[0]
        if c_key:
            self.user_active_device[c_key] = chosen.device_id
        return chosen

    def get_active_label(self, chat_id: Optional[str] = None) -> str:
        """Return formatted label for active device of specific chat."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            return "No Devices"
        return f"#{active.serial} {active.name}"

    def get_flow_for_device(self, device_id: str, chat_id: Optional[str] = None) -> object:
        """Get or create ChimeAutomationFlow for device using tenant's client."""
        client, _ = self.get_tenant_geelark_client(chat_id)
        from automation.chime_flow import ChimeAutomationFlow
        return ChimeAutomationFlow(device_id, client)

    def ensure_device_ready(self, active: object, chat_id: Optional[str] = None) -> bool:
        """Ensure device is powered on, system shades dismissed, and Chime is open & unlocked."""
        if not active:
            return False

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        # 1. Power status check
        st_list = phone_mgr.query_status([active.device_id])
        if not st_list or st_list[0].get("status") != 0:
            self.notifier.send_message(
                f"🟡 *Device #{active.serial} is not running.*\n"
                "Initiating cloud phone power on via GeeLark API (~15s)...",
                chat_id=chat_id,
            )
            phone_mgr.start_phone([active.device_id])
            for _ in range(20):
                time.sleep(2.0)
                st = phone_mgr.query_status([active.device_id])
                if st and st[0].get("status") == 0:
                    logger.info("Device %s booted to running status.", active.serial)
                    time.sleep(3.0)  # Wait for Android system services
                    break
            else:
                self.notifier.send_message(
                    f"❌ *Failed to power on #{active.serial}.* Please check GeeLark console.",
                    chat_id=chat_id,
                )
                return False

        # 2. Ensure Chime is active and unlocked
        try:
            flow = self.get_flow_for_device(active.device_id, chat_id=chat_id)
            unlock_res = flow.ensure_open_and_unlocked(pin=active.pin)
            if unlock_res.get("needs_pin"):
                self.notifier.send_pin_request(active.serial, active.name, chat_id=chat_id)
                return False
            if unlock_res.get("connection_error"):
                self.notifier.send_message(
                    f"⚠️ *No Internet / Network Error on #{active.serial} {active.name}!*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🌐 Chime is showing: `Please check your connection.`\n"
                    "🔄 The bot force-closed the app from recents and restarted it, but network is still unreachable.\n"
                    "💡 Please verify proxy / Wi-Fi configuration in GeeLark console and try again.",
                    chat_id=chat_id,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Failed to prepare or unlock device #%s: %s", active.serial, e)
            self.notifier.send_message(
                f"⚠️ *Could not launch or unlock Chime on #{active.serial}:* {e}\n"
                "Please verify the cloud phone is responsive and try again.",
                chat_id=chat_id,
            )
            return False

    def handle_manual_refresh(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Trigger screen refresh and manual check for payments on active device."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            self.notifier.send_message(
                "⚠️ *No Cloud Devices Found*\n\n"
                "No cloud phones are registered under your client account. "
                "Please configure and sync your GeeLark account in the Web Admin Portal.",
                chat_id=chat_id,
            )
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"🔄 Refreshing #{active.serial}...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)
        self.notifier.send_message(
            f"🔄 *Manual Refresh Triggered on #{active.serial}*\nChecking transactions...",
            chat_id=chat_id,
        )

        if not self.ensure_device_ready(active, chat_id=chat_id):
            return

        try:
            client, _ = self.get_tenant_geelark_client(chat_id)
            from geelark.shell import ShellManager
            shell_mgr = ShellManager(client)

            # 1. Pull down to refresh Chime screen on active device
            shell_mgr.swipe(active.device_id, 360, 400, 360, 900, duration_ms=400)
            time.sleep(1.0)

            # 2. Check for newly arrived deposits if primary tenant, else parse screen
            elements = shell_mgr.get_screen_elements(active.device_id)
            balance = TransactionParser.extract_balance_from_elements(elements) or active.latest_balance or "Unavailable"
            if balance != "Unavailable":
                self.monitor.device_mgr.update_balance(active.device_id, balance)
                try:
                    t_id = self.db.get_primary_tenant_id() if hasattr(self.db, "get_primary_tenant_id") else "tenant-kamruzzaman"
                    self.db.update_device_balance(t_id, active.device_id, balance)
                except Exception as e:
                    logger.debug("Error updating DB device balance: %s", e)

            msg = (
                "✅ *Manual Refresh Completed!*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 *Device:* `#{active.serial} {active.name}`\n"
                f"💳 *Current Balance:* `{balance}`\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            self.notifier.send_control_panel(text=msg, active_device_name=self.get_active_label(chat_id=chat_id), chat_id=chat_id)
        except Exception as e:
            logger.error("Error during manual refresh: %s", e)
            self.notifier.send_message(f"❌ *Manual Refresh Failed:* {e}", chat_id=chat_id)

    def handle_back_and_refresh(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Step back 1 screen on active cloud phone (e.g. from transaction details/history) and pull-to-refresh."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            self.notifier.send_message(
                "⚠️ *No Cloud Devices Found*\n\n"
                "No cloud phones are registered under your client account. "
                "Please configure and sync your GeeLark account in the Web Admin Portal.",
                chat_id=chat_id,
            )
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"🔙 Stepping back & refreshing #{active.serial}...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)
        self.notifier.send_message(
            f"🔙 *Back & Refresh Triggered on #{active.serial}*\n"
            "Stepping back 1 screen (returning to main screen) and refreshing Chime...",
            chat_id=chat_id,
        )

        if not self.ensure_device_ready(active, chat_id=chat_id):
            return

        try:
            client, _ = self.get_tenant_geelark_client(chat_id)
            from geelark.shell import ShellManager
            shell_mgr = ShellManager(client)

            # 1. Send Android Back keyevent (input keyevent 4) to navigate back 1 screen
            shell_mgr.back(active.device_id)
            time.sleep(1.0)

            # 2. Pull down to refresh Chime screen on active device
            shell_mgr.swipe(active.device_id, 360, 400, 360, 950, duration_ms=400)
            time.sleep(1.2)

            # 3. Check for newly arrived deposits / updated balance
            elements = shell_mgr.get_screen_elements(active.device_id)
            balance = TransactionParser.extract_balance_from_elements(elements) or active.latest_balance or "Unavailable"
            if balance != "Unavailable":
                self.monitor.device_mgr.update_balance(active.device_id, balance)
                try:
                    t_id = self.db.get_primary_tenant_id() if hasattr(self.db, "get_primary_tenant_id") else "tenant-kamruzzaman"
                    self.db.update_device_balance(t_id, active.device_id, balance)
                except Exception as e:
                    logger.debug("Error updating DB device balance: %s", e)

            msg = (
                "✅ *Back & Refresh Completed!*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 *Device:* `#{active.serial} {active.name}`\n"
                f"💳 *Current Balance:* `{balance}`\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            self.notifier.send_control_panel(text=msg, active_device_name=self.get_active_label(chat_id=chat_id), chat_id=chat_id)
        except Exception as e:
            logger.error("Error during back and refresh: %s", e)
            self.notifier.send_message(f"❌ *Back & Refresh Failed:* {e}", chat_id=chat_id)

    def handle_back(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Send Android Back keyevent on active cloud phone."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            return
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"🔙 Back key sent to #{active.serial}")
        try:
            client, _ = self.get_tenant_geelark_client(chat_id)
            from geelark.shell import ShellManager
            shell_mgr = ShellManager(client)
            shell_mgr.back(active.device_id)
            self.notifier.send_message(f"🔙 *Back key pressed on #{active.serial}*", chat_id=chat_id)
        except Exception as e:
            self.notifier.send_message(f"❌ *Back Failed:* {e}", chat_id=chat_id)

    def handle_check_balance(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Fetch and report current balance for active device."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"💳 Checking balance for #{active.serial}...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        now = time.time()
        if active.latest_balance and (now - active.last_synced_at) < 60:
            age_sec = max(1, int(now - active.last_synced_at))
            msg = (
                "💳 *Chime Balance Check*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 *Device:* `#{active.serial} {active.name}`\n"
                f"Available Checking: *{active.latest_balance}*\n"
                f"🕒 *Synced:* {age_sec}s ago (Instant Sync)\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            self.notifier.send_control_panel(text=msg, active_device_name=self.get_active_label(chat_id=chat_id), chat_id=chat_id)
            return

        if not self.ensure_device_ready(active, chat_id=chat_id):
            return

        try:
            client, _ = self.get_tenant_geelark_client(chat_id)
            from geelark.shell import ShellManager
            shell_mgr = ShellManager(client)
            elements = shell_mgr.get_screen_elements(active.device_id)
            balance = TransactionParser.extract_balance_from_elements(elements) or active.latest_balance or "Unavailable"
            if balance != "Unavailable":
                self.monitor.device_mgr.update_balance(active.device_id, balance)
                try:
                    t_id = self.db.get_primary_tenant_id() if hasattr(self.db, "get_primary_tenant_id") else "tenant-kamruzzaman"
                    self.db.update_device_balance(t_id, active.device_id, balance)
                except Exception as e:
                    logger.debug("Error updating DB device balance: %s", e)

            msg = (
                "💳 *Chime Balance Check*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 *Device:* `#{active.serial} {active.name}`\n"
                f"Available Checking: *{balance}*\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            self.notifier.send_control_panel(text=msg, active_device_name=self.get_active_label(chat_id=chat_id), chat_id=chat_id)
        except Exception as e:
            logger.error("Error checking balance: %s", e)
            self.notifier.send_message(f"❌ *Balance Check Failed:* {e}", chat_id=chat_id)

    def handle_screenshot(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Capture and send live device screenshot of active device."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"📸 Taking screenshot of #{active.serial}...")
        self.notifier.send_chat_action("upload_photo", chat_id=chat_id)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        st_list = phone_mgr.query_status([active.device_id])
        if not st_list or st_list[0].get("status") != 0:
            if not self.ensure_device_ready(active, chat_id=chat_id):
                return

        try:
            task_id = phone_mgr.capture_screenshot(active.device_id)
            url = phone_mgr.wait_for_screenshot(task_id, max_attempts=8)

            if url:
                self.notifier.send_photo(
                    url,
                    caption=f"📸 *Live Screenshot*\nDevice: `#{active.serial} {active.name}`",
                    chat_id=chat_id,
                )
            else:
                self.notifier.send_message("⚠️ Could not retrieve screenshot link from GeeLark.", chat_id=chat_id)
        except Exception as e:
            logger.error("Error capturing screenshot: %s", e)
            self.notifier.send_message(f"❌ *Screenshot Failed:* {e}", chat_id=chat_id)

    def handle_status(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Report cloud phone state of active device."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="📱 Querying device...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        try:
            st_list = phone_mgr.query_status([active.device_id])
            status_desc = "Unknown"
            if st_list:
                status_val = st_list[0].get("status", -1)
                status_desc = {0: "Running 🟢", 1: "Starting 🟡", 2: "Shutdown 🔴"}.get(status_val, "Unknown")

            pin_desc = f"Configured (`{active.pin}`)" if active.pin else "⚠️ NOT SET (`/setpin`)"
            msg = (
                "📱 *Cloud Phone Status*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Device: `#{active.serial} {active.name}`\n"
                f"ID: `{active.device_id}`\n"
                f"Status: *{status_desc}*\n"
                f"Chime PIN: *{pin_desc}*\n"
                f"Auto-poll: *Every {self.config.poll_interval_seconds}s*\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            self.notifier.send_control_panel(text=msg, active_device_name=self.get_active_label(chat_id=chat_id), chat_id=chat_id)
        except Exception as e:
            self.notifier.send_message(f"❌ *Status Query Failed:* {e}", chat_id=chat_id)

    def handle_devices_overview(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Send multi-device overview with switch buttons isolated to user's tenant."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="📋 Loading all devices...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        devices = self.get_tenant_devices(chat_id=chat_id)
        if not devices:
            self.notifier.send_message(
                "📱 *No Cloud Devices Found*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "No cloud phones are registered under your client account.\n"
                "Please configure and sync your GeeLark account in the Web Admin Portal.",
                chat_id=chat_id,
            )
            return

        active = self.get_active_device(chat_id=chat_id)
        self.notifier.send_device_overview(devices, active_device_id=active.device_id if active else None, chat_id=chat_id)

    def handle_select_device(self, serial: str, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Switch the active device context isolated per user chat and show its detail card."""
        tenant_devs = self.get_tenant_devices(chat_id=chat_id)
        dev = next((d for d in tenant_devs if str(d.serial) == str(serial) or str(d.device_id) == str(serial)), None)
        if not dev:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text=f"Device #{serial} not found!")
            self.notifier.send_message(f"❌ Device `#{serial}` not found under your client account.", chat_id=chat_id)
            return

        if chat_id:
            self.user_active_device[str(chat_id).strip()] = dev.device_id

        dev_clean_name = (dev.name or "").strip() or f"Phone #{dev.serial}"
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"Selected #{dev.serial} {dev_clean_name}!")

        # Query hardware status from GeeLark
        phone_mgr = self.get_tenant_phone_manager(chat_id)
        is_running = False
        try:
            st_list = phone_mgr.query_status([dev.device_id])
            if st_list and st_list[0].get("status") == 0:
                is_running = True
        except Exception:
            pass

        status_tag = "Monitoring 🟢" if dev.enabled else "Paused 🔴"
        hardware_tag = "Running 🟢" if is_running else "Shutdown 🔴"
        pin_tag = f"`{dev.pin}`" if dev.pin else "⚠️ Not Set (`/setpin <pin>`)"
        bal_tag = dev.latest_balance or "Pending sync"

        msg = (
            f"📱 *Device Control Card*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 *Device:* `#{dev.serial} {dev_clean_name}`\n"
            f"⚡️ *Hardware Power:* *{hardware_tag}*\n"
            f"📊 *Auto-Monitoring:* *{status_tag}*\n"
            f"💳 *Current Balance:* `{bal_tag}`\n"
            f"🔐 *Chime PIN:* {pin_tag}\n"
            f"🆔 *GeeLark ID:* `{dev.device_id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Use the buttons below to control this phone directly:"
        )

        device_card_keyboard = [
            [
                {"text": "⏸ Disconnect & Pause" if dev.enabled else "🔌 Connect & Monitor", "callback_data": f"action_toggle_dev_{dev.serial}"},
                {"text": "🛑 Power OFF" if is_running else "⚡️ Power ON", "callback_data": f"action_power_off_{dev.serial}" if is_running else f"action_power_on_{dev.serial}"},
            ],
            [
                {"text": "🔄 Refresh Screen", "callback_data": "action_refresh"},
                {"text": "🔙 Back & Refresh", "callback_data": "action_back_refresh"},
            ],
            [
                {"text": "📸 View Screenshot", "callback_data": "action_screen"},
                {"text": "💳 Check Balance", "callback_data": "action_balance"},
            ],
            [
                {"text": "🔐 Set Chime PIN", "callback_data": f"action_pin_prompt_{dev.serial}"},
                {"text": "🔙 Android Back", "callback_data": "action_back"},
            ],
            [
                {"text": "📋 All Devices List", "callback_data": "action_devices_overview"},
                {"text": "🏠 Main Menu", "callback_data": "action_menu"},
            ],
        ]

        self.notifier.send_control_panel(
            text=msg,
            custom_keyboard=device_card_keyboard,
            active_device_name=f"#{dev.serial} {dev_clean_name}",
            chat_id=chat_id,
        )

    def handle_setpin(self, text: str, chat_id: Optional[str] = None) -> None:
        """Handle /setpin command to assign a Chime passcode to a device."""
        raw_clean = text.strip().lower()
        if raw_clean in ("🔐 set device pin", "set pin", "device pin", "/setpin", "setpin"):
            self.notifier.send_message(
                "💡 *How to Set / Update Device PIN:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• `/setpin <pin>` — Set PIN for currently active device\n"
                "• `/setpin <serial> <pin>` — Set PIN for a specific device\n\n"
                "_Example:_ `/setpin 226 1122`",
                chat_id=chat_id,
            )
            return

        parts = text.strip().split()
        sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
        tenant_id = sub.tenant_id if sub else None

        if len(parts) == 2:
            # /setpin <pin> -> apply to currently active device
            pin = parts[1].strip()
            active = self.get_active_device(chat_id=chat_id)
            if not active:
                self.notifier.send_message("❌ No active device found to assign PIN.", chat_id=chat_id)
                return
            updated = self.monitor.device_mgr.set_device_pin(active.device_id, pin)
            if tenant_id:
                self.db.set_device_pin(tenant_id, active.device_id, pin)
            if updated:
                self.notifier.send_message(
                    f"✅ *PIN Updated Successfully!*\n"
                    f"📱 Device: `#{updated.serial} {updated.name}`\n"
                    f"🔐 New PIN: `{pin}`",
                    chat_id=chat_id,
                )
            else:
                self.notifier.send_message("❌ Failed to update PIN.", chat_id=chat_id)
        elif len(parts) >= 3 and not parts[1].lower() in ("set", "device", "pin"):
            # /setpin <serial_or_name> <pin>
            target = parts[1].strip()
            pin = parts[2].strip()
            tenant_devs = self.get_tenant_devices(chat_id=chat_id)
            target_dev = next((d for d in tenant_devs if str(d.serial) == str(target) or str(d.device_id) == str(target) or str(d.name) == str(target)), None)
            if not target_dev:
                self.notifier.send_message(
                    f"❌ Could not find device matching `{target}` under your account. Use `/devices` to list your devices.",
                    chat_id=chat_id,
                )
                return
            updated = self.monitor.device_mgr.set_device_pin(target_dev.device_id, pin)
            if tenant_id:
                self.db.set_device_pin(tenant_id, target_dev.device_id, pin)
            if updated:
                self.notifier.send_message(
                    f"✅ *PIN Updated Successfully!*\n"
                    f"📱 Device: `#{updated.serial} {updated.name}`\n"
                    f"🔐 New PIN: `{pin}`",
                    chat_id=chat_id,
                )
            else:
                self.notifier.send_message("❌ Failed to update PIN.", chat_id=chat_id)
        else:
            self.notifier.send_message(
                "💡 *Usage of /setpin:*\n"
                "• `/setpin <pin>` — Set PIN for currently active device\n"
                "• `/setpin <serial> <pin>` — Set PIN for a specific device\n\n"
                "_Example:_ `/setpin 226 1122`",
                chat_id=chat_id,
            )

    def handle_scan_devices(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Scan GeeLark account for active devices and update registry."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="🔍 Scanning GeeLark account...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)
        self.notifier.send_message("🔍 *Querying GeeLark API for all cloud phones...*", chat_id=chat_id)
        try:
            phone_mgr = self.get_tenant_phone_manager(chat_id)
            sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
            tenant_id = sub.tenant_id if sub else None

            res = phone_mgr.list_phones(page=1, page_size=100)
            items = res.get("items", [])
            synced = []
            for item in items:
                p_id = str(item.get("id", ""))
                serial = str(item.get("serialNo", ""))
                name = str(item.get("serialName", f"Phone-{serial}"))
                if tenant_id:
                    self.db.upsert_device(tenant_id, p_id, serial, name, enabled=True)
                dev_obj = ChimeDevice(
                    device_id=p_id,
                    serial=serial,
                    name=name,
                    enabled=True,
                )
                self.monitor.device_mgr.add_or_update_device(dev_obj)
                synced.append(dev_obj)

            self.notifier.send_message(
                f"✅ *Scan Complete!* Discovered {len(synced)} cloud phone(s) on your GeeLark account.",
                chat_id=chat_id,
            )
            active = self.get_active_device(chat_id=chat_id)
            self.notifier.send_device_overview(synced, active_device_id=active.device_id if active else None, chat_id=chat_id)
        except Exception as e:
            self.notifier.send_message(f"❌ *GeeLark Scan Failed:* {e}", chat_id=chat_id)

    def handle_toggle_device(self, serial: str, callback_id: Optional[str] = None, state: Optional[bool] = None, chat_id: Optional[str] = None) -> None:
        """Toggle monitoring status (connect/disconnect) for device and update overview."""
        tenant_devs = self.get_tenant_devices(chat_id=chat_id)
        dev = next((d for d in tenant_devs if str(d.serial) == str(serial) or str(d.device_id) == str(serial)), None)
        if not dev:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text=f"Device #{serial} not found!")
            return

        new_state = state if state is not None else (not dev.enabled)
        dev.enabled = new_state
        self.monitor.device_mgr.toggle_device_enabled(dev.device_id, state=new_state)

        sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
        if sub:
            self.db.toggle_device_enabled(sub.tenant_id, dev.device_id, state=new_state)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        if new_state:
            try:
                phone_mgr.start_phone([dev.device_id])
            except Exception as e:
                logger.warning("Could not send start_phone for #%s: %s", dev.serial, e)
            status_text = "Connected (Monitoring 🟢)"
        else:
            try:
                phone_mgr.stop_phone([dev.device_id])
            except Exception as e:
                logger.warning("Could not send stop_phone for #%s: %s", dev.serial, e)
            status_text = "Disconnected & Powered OFF 🔴"

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"#{dev.serial} {status_text}!")
        self.notifier.send_message(
            f"🔄 *Device Updated:*\n`#{dev.serial} {dev.name}` is now *{status_text}*.",
            chat_id=chat_id,
        )
        self.handle_devices_overview(chat_id=chat_id)

    def handle_power_off(self, target_identifier: Optional[str] = None, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Explicitly shut down / power off a cloud phone and pause its auto-monitoring."""
        if target_identifier and str(target_identifier).strip().lower() == "all":
            self.handle_disconnect_all(callback_id=callback_id, chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="🛑 Powering off cloud phone...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        tenant_devs = self.get_tenant_devices(chat_id=chat_id)
        if target_identifier:
            dev = next((d for d in tenant_devs if str(d.serial) == str(target_identifier) or str(d.device_id) == str(target_identifier)), None)
        else:
            dev = self.get_active_device(chat_id=chat_id)

        if not dev:
            self.notifier.send_message(f"❌ Device `{target_identifier or 'Active'}` not found under your account.", chat_id=chat_id)
            return

        self.monitor.device_mgr.toggle_device_enabled(dev.device_id, state=False)
        sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
        if sub:
            self.db.toggle_device_enabled(sub.tenant_id, dev.device_id, state=False)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        try:
            phone_mgr.stop_phone([dev.device_id])
            logger.info("Powered off device #%s (%s)", dev.serial, dev.name)
            self.notifier.send_message(
                f"🛑 *Device Powered OFF Successfully!*\n"
                f"📱 Device: `#{dev.serial} {dev.name}`\n"
                "Cloud phone shutdown command sent to GeeLark. Auto-monitoring paused.",
                chat_id=chat_id,
            )
        except Exception as e:
            logger.error("Failed to power off device #%s: %s", dev.serial, e)
            self.notifier.send_message(f"❌ *Failed to power off #{dev.serial}:* {e}", chat_id=chat_id)

        self.handle_devices_overview(chat_id=chat_id)

    def handle_power_on(self, target_identifier: Optional[str] = None, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Explicitly power on / boot a cloud phone and enable auto-monitoring."""
        if target_identifier and str(target_identifier).strip().lower() == "all":
            self.handle_connect_all(callback_id=callback_id, chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="⚡️ Powering on cloud phone...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        tenant_devs = self.get_tenant_devices(chat_id=chat_id)
        if target_identifier:
            dev = next((d for d in tenant_devs if str(d.serial) == str(target_identifier) or str(d.device_id) == str(target_identifier)), None)
        else:
            dev = self.get_active_device(chat_id=chat_id)

        if not dev:
            self.notifier.send_message(f"❌ Device `{target_identifier or 'Active'}` not found under your account.", chat_id=chat_id)
            return

        self.monitor.device_mgr.toggle_device_enabled(dev.device_id, state=True)
        sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
        if sub:
            self.db.toggle_device_enabled(sub.tenant_id, dev.device_id, state=True)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        try:
            self.notifier.send_message(
                f"⚡️ *Powering On Device #{dev.serial} {dev.name}...*\nStarting cloud phone via GeeLark API (~15s)...",
                chat_id=chat_id,
            )
            # Try starting with quick retries if GeeLark capacity is temporarily busy
            started = False
            last_err = ""
            for attempt in range(1, 4):
                try:
                    phone_mgr.start_phone([dev.device_id])
                    started = True
                    break
                except Exception as ex:
                    last_err = str(ex)
                    if attempt < 3:
                        import time
                        time.sleep(2.5)

            if not started:
                raise RuntimeError(last_err or "Unknown start failure")

            logger.info("Initiated start for device #%s", dev.serial)
            self.notifier.send_message(
                f"✅ *Power ON Initiated for #{dev.serial}!* Device will be ready in ~15-20s.",
                chat_id=chat_id,
            )
        except Exception as e:
            logger.error("Failed to power on device #%s: %s", dev.serial, e)
            err_str = str(e)
            if "high demand" in err_str.lower() or "43043" in err_str:
                self.notifier.send_message(
                    f"⚠️ *GeeLark Server Notice for #{dev.serial}:*\n"
                    "GeeLark cloud servers are currently experiencing high demand for Android 14 phones.\n"
                    "Please wait 1-2 minutes and try clicking *Power ON* again.",
                    chat_id=chat_id,
                )
            else:
                self.notifier.send_message(f"❌ *Failed to power on #{dev.serial}:* {err_str}", chat_id=chat_id)

        self.handle_devices_overview(chat_id=chat_id)

    def handle_connect_all(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Enable monitoring across all registered devices for this tenant."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="🔌 Connecting all devices...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        devs = self.get_tenant_devices(chat_id=chat_id)
        if not devs:
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
        for d in devs:
            d.enabled = True
            self.monitor.device_mgr.toggle_device_enabled(d.device_id, state=True)
            if sub:
                self.db.toggle_device_enabled(sub.tenant_id, d.device_id, state=True)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        try:
            phone_mgr.start_phone([d.device_id for d in devs])
        except Exception as e:
            logger.warning("Could not start phones during connect all: %s", e)
        self.notifier.send_message(
            f"✅ *All Devices Connected & Starting!*\n"
            f"Background auto-monitoring is now active across all *{len(devs)}* cloud phones.",
            chat_id=chat_id,
        )
        self.handle_devices_overview(chat_id=chat_id)

    def handle_disconnect_all(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Pause monitoring and shut down all registered devices for this tenant."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="⏸ Pausing & shutting down all devices...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        devs = self.get_tenant_devices(chat_id=chat_id)
        if not devs:
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        sub = self.db.get_subscriber_by_chat_id(str(chat_id).strip()) if chat_id else None
        for d in devs:
            d.enabled = False
            self.monitor.device_mgr.toggle_device_enabled(d.device_id, state=False)
            if sub:
                self.db.toggle_device_enabled(sub.tenant_id, d.device_id, state=False)

        phone_mgr = self.get_tenant_phone_manager(chat_id)
        try:
            phone_mgr.stop_phone([d.device_id for d in devs])
        except Exception as e:
            logger.warning("Could not stop phones during disconnect all: %s", e)
        self.notifier.send_message(
            f"⏸ *All Devices Paused & Powered OFF!*\n"
            f"Auto-monitoring has been paused and cloud phones shut down for all *{len(devs)}* devices.",
            chat_id=chat_id,
        )
        self.handle_devices_overview(chat_id=chat_id)

    def handle_all_balances(self, chat_id: Optional[str] = None) -> None:
        """Display consolidated balance summary across all devices for this tenant."""
        self.notifier.send_chat_action("typing", chat_id=chat_id)
        devices = self.get_tenant_devices(chat_id=chat_id)
        if not devices:
            self.notifier.send_message(
                "💳 *All Devices Balances*\n━━━━━━━━━━━━━━━━━━━━━━\nNo cloud phones registered under your account.",
                chat_id=chat_id,
            )
            return

        lines = [
            f"💳 *All Devices Balances ({len(devices)})*",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for d in devices:
            bal = d.latest_balance or "Pending sync"
            name = d.name or f"Phone-{d.serial}"
            icon = "🟢" if d.enabled else "⚪️"
            lines.append(f"{icon} `#{d.serial}` *{name}*: `{bal}`")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        self.notifier.send_message("\n".join(lines), chat_id=chat_id)

    def handle_pin_prompt(self, serial: str, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Prompt user with exact /setpin instructions for device."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id)
        tenant_devs = self.get_tenant_devices(chat_id=chat_id)
        dev = next((d for d in tenant_devs if str(d.serial) == str(serial) or str(d.device_id) == str(serial)), None)
        name = dev.name if dev else f"Device #{serial}"
        self.notifier.send_pin_request(serial=serial, device_name=name, chat_id=chat_id)

    def handle_recent_history(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Fetch and display recent transaction history for active device."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"📜 Loading history for #{active.serial}...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        if not self.ensure_device_ready(active, chat_id=chat_id):
            return

        try:
            flow = self.get_flow_for_device(active.device_id, chat_id=chat_id)
            elements = flow.open_checking_screen()
            txs = TransactionParser.extract_all_history_transactions(elements)
            balance = TransactionParser.extract_balance_from_elements(elements) or active.latest_balance or "Unavailable"
            if balance != "Unavailable":
                self.monitor.device_mgr.update_balance(active.device_id, balance)
                try:
                    t_id = self.db.get_primary_tenant_id() if hasattr(self.db, "get_primary_tenant_id") else "tenant-kamruzzaman"
                    self.db.update_device_balance(t_id, active.device_id, balance)
                except Exception as e:
                    logger.debug("Error updating DB device balance: %s", e)

            if not txs:
                msg = (
                    "📜 *Recent Transaction History*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📱 *Device:* `#{active.serial} {active.name}`\n"
                    f"💳 *Balance:* `{balance}`\n"
                    "No transactions currently visible on screen.\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                )
            else:
                lines = [
                    "📜 *Recent Transaction History*",
                    "━━━━━━━━━━━━━━━━━━━━━━",
                    f"📱 *Device:* `#{active.serial} {active.name}`",
                    f"💳 *Balance:* `{balance}`",
                    "",
                ]
                for tx in txs[:10]:
                    icon = "🟢" if tx["is_inbound"] else "🔴"
                    lines.append(f"{icon} *{tx['title']}*")
                    lines.append(f"   💵 `{tx['amount']}`")
                    if tx.get("detail"):
                        lines.append(f"   🕒 {tx['detail']}")
                    if tx.get("note"):
                        lines.append(f"   📝 {tx['note']}")
                    lines.append("")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━")
                msg = "\n".join(lines)

            history_keyboard = [
                [
                    {"text": "📸 Get History Screenshot", "callback_data": "action_history_screen"},
                ],
                [
                    {"text": "🔄 Refresh History", "callback_data": "action_history"},
                    {"text": "💳 Check Balance", "callback_data": "action_balance"},
                ],
                [
                    {"text": "📱 Switch Device", "callback_data": "action_switch_device"},
                    {"text": "🏠 Main Menu", "callback_data": "action_menu"},
                ],
            ]
            self.notifier.send_control_panel(
                text=msg,
                custom_keyboard=history_keyboard,
                active_device_name=self.get_active_label(chat_id=chat_id),
                chat_id=chat_id,
            )
        except Exception as e:
            logger.error("Error loading transaction history on %s: %s", active.name, e)
            self.notifier.send_message(f"❌ *History Load Failed:* {e}", chat_id=chat_id)

    def handle_history_screenshot(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Capture and send screenshot specifically of the Chime Checking/History screen for active device."""
        active = self.get_active_device(chat_id=chat_id)
        if not active:
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="No devices found.")
            self.notifier.send_message("⚠️ No cloud phones registered under your account.", chat_id=chat_id)
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"📸 Taking history screenshot of #{active.serial}...")
        self.notifier.send_chat_action("upload_photo", chat_id=chat_id)

        if not self.ensure_device_ready(active, chat_id=chat_id):
            return

        try:
            flow = self.get_flow_for_device(active.device_id, chat_id=chat_id)
            flow.open_checking_screen()

            phone_mgr = self.get_tenant_phone_manager(chat_id)
            task_id = phone_mgr.capture_screenshot(active.device_id)
            url = phone_mgr.wait_for_screenshot(task_id, max_attempts=8)

            if url:
                self.notifier.send_photo(
                    url,
                    caption=f"📸 *Chime Transaction History Screenshot*\nDevice: `#{active.serial} {active.name}`",
                    chat_id=chat_id,
                )
            else:
                self.notifier.send_message("⚠️ Could not retrieve history screenshot link from GeeLark.", chat_id=chat_id)

            self.notifier.send_control_panel(active_device_name=self.get_active_label(chat_id=chat_id), chat_id=chat_id)
        except Exception as e:
            logger.error("Error capturing history screenshot on %s: %s", active.name, e)
    def handle_unregistered_access_request(self, msg: dict) -> None:
        """Handle incoming /start or command from an unregistered user by requesting admin approval."""
        chat_obj = msg.get("chat", {})
        from_obj = msg.get("from", {})
        sender_id = str(chat_obj.get("id") or from_obj.get("id", "")).strip()
        if not sender_id:
            return

        first_name = from_obj.get("first_name", "")
        last_name = from_obj.get("last_name", "")
        username = from_obj.get("username", "")
        full_name = f"{first_name} {last_name}".strip() or "New User"
        user_tag = f"@{username}" if username else "No username"

        # Check if already registered in DB
        sub = self.db.get_subscriber_by_chat_id(sender_id)
        if sub:
            if not sub.is_active or not sub.receive_alerts:
                self.notifier.send_message(
                    "⏳ *Account Inactive / Pending*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Your account has been registered but is currently waiting for administrator approval.\n"
                    "You will be notified here automatically once approved.",
                    reply_markup={"remove_keyboard": True},
                    chat_id=sender_id,
                )
            else:
                self.notifier.send_message(
                    f"👁 *Notification Account Active*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Welcome back, *{full_name}*!\n"
                    "You will automatically receive notifications here whenever a new Chime payment arrives.",
                    reply_markup={"remove_keyboard": True},
                    chat_id=sender_id,
                )
            return

        # Send confirmation to the requesting user (NO MENU, remove any keyboard)
        self.notifier.send_message(
            f"⏳ *Access Request Submitted!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Hello *{full_name}*! Your request to receive Chime payment notifications has been sent to the administrator for approval.\n\n"
            "You will be notified here automatically once approved.",
            reply_markup={"remove_keyboard": True},
            chat_id=sender_id,
        )

        # Notify all Admins
        all_admins = self.get_all_admins()
        if not all_admins:
            logger.warning("No admin chat ID configured to approve access request.")
            return

        # Avoid spamming admin repeatedly if already pending
        if sender_id in self._pending_requests:
            logger.info("Access request already pending for chat ID %s", sender_id)
            return

        self._pending_requests.add(sender_id)

        admin_msg = (
            "🔔 *New User Access Request*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Name:* {full_name}\n"
            f"🔗 *Username:* {user_tag}\n"
            f"🆔 *Chat ID:* `{sender_id}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Do you want to approve this user to receive new payment notifications?"
        )
        approval_keyboard = [
            [
                {"text": "✅ Approve", "callback_data": f"action_approve_{sender_id}"},
                {"text": "❌ Decline", "callback_data": f"action_decline_{sender_id}"},
            ]
        ]
        for a_chat in all_admins:
            self.notifier.send_control_panel(
                text=admin_msg,
                custom_keyboard=approval_keyboard,
                chat_id=a_chat,
            )

    def handle_admin_approve(self, target_id: str, callback_id: Optional[str] = None, admin_chat_id: Optional[str] = None) -> None:
        """Admin callback to approve a user to receive payment alerts."""
        if admin_chat_id and not self.is_admin(admin_chat_id):
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="⛔️ Unauthorized: Only Admin can approve users.")
            return

        tenants = self.db.get_all_tenants()
        tenant_id = tenants[0].id if tenants else "tenant-kamruzzaman"

        # Register subscriber as viewer_only with receive_alerts=True
        sub = self.db.add_subscriber(
            tenant_id=tenant_id,
            chat_id=target_id,
            username="",
            role="viewer_only",
            receive_alerts=True,
        )
        self._pending_requests.discard(target_id)

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"✅ User {target_id} approved!")

        # Confirmation to Admin
        self.notifier.send_message(
            f"✅ *User Approved Successfully!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Chat ID:* `{target_id}`\n"
            "Role: `Notification Only (No Menu)`\n"
            "This user will now receive all incoming deposit alerts automatically.",
            chat_id=admin_chat_id,
        )

        # Notification to the User (NO MENU at all!)
        self.notifier.send_message(
            "🎉 *Access Request Approved!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "The administrator has approved your request.\n\n"
            "You will now automatically receive instant notifications whenever a new Chime payment arrives!",
            reply_markup={"remove_keyboard": True},
            chat_id=target_id,
        )

        # Refresh overview for admin
        if admin_chat_id:
            self.handle_users_overview(chat_id=admin_chat_id)

    def handle_admin_decline(self, target_id: str, callback_id: Optional[str] = None, admin_chat_id: Optional[str] = None) -> None:
        """Admin callback to decline an access request."""
        if admin_chat_id and not self.is_admin(admin_chat_id):
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="⛔️ Unauthorized: Only Admin can decline.")
            return

        self._pending_requests.discard(target_id)

        # Delete from database if exists
        self.db.delete_subscriber_by_chat_id(target_id)

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"❌ User {target_id} declined.")

        # Confirmation to Admin
        self.notifier.send_message(
            f"❌ *Access Request Declined*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"User Chat ID `{target_id}` was declined.",
            chat_id=admin_chat_id,
        )

        # Notification to the User
        self.notifier.send_message(
            "❌ *Access Request Declined*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "Your request was not approved by the administrator.",
            reply_markup={"remove_keyboard": True},
            chat_id=target_id,
        )

        # Refresh overview for admin
        if admin_chat_id:
            self.handle_users_overview(chat_id=admin_chat_id)

    # -------------------------------------------------------------------------
    # Multi-Admin & Member Management
    # -------------------------------------------------------------------------

    def handle_users_overview(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Interactive multi-admin & member management panel."""
        if not self.is_admin(chat_id):
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="⛔️ Unauthorized")
            return

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="👥 Loading user directory...")

        subs = self.db.get_all_subscribers(active_only=True)
        primary_admin = self.allowed_chat_id

        admins = []
        members = []
        seen_ids = set()

        # Find primary super admin username from DB if available
        super_name = "Primary Super Admin 🛡"
        for s in subs:
            if primary_admin and str(s.chat_id).strip() == primary_admin:
                if s.username:
                    super_name = f"@{s.username.lstrip('@')}"
                break

        if primary_admin:
            admins.append({"chat_id": primary_admin, "name": super_name, "is_super": True})
            seen_ids.add(primary_admin)

        for s in subs:
            s_chat = str(s.chat_id).strip()
            if s_chat in seen_ids:
                continue
            seen_ids.add(s_chat)
            u_clean = s.username.strip() if s.username else ""
            if u_clean:
                label = u_clean if u_clean.startswith(("+", "@")) else f"@{u_clean}"
            else:
                label = f"User-{s_chat[-4:]}"

            if s.role == "full_controller":
                admins.append({"chat_id": s_chat, "name": label, "is_super": False})
            else:
                members.append({"chat_id": s_chat, "name": label, "alerts": s.receive_alerts})

        lines = [
            "👥 *Telegram User Management*",
            f"👑 *Admins:* {len(admins)} | 👤 *Members:* {len(members)}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "👑 *Admins (Full Controls):*",
        ]
        for a in admins:
            badge = " [Super Admin 🛡]" if a.get("is_super") else " [Admin]"
            lines.append(f"• `{a['chat_id']}` {a['name']}{badge}")

        lines.append("")
        lines.append("👤 *Members (Payment Alerts Only - No Menu):*")
        if members:
            for m in members:
                lines.append(f"• `{m['chat_id']}` {m['name']} [Alerts: 🟢]")
        else:
            lines.append("• _No regular members subscribed yet._")

        if self._pending_requests:
            lines.append("")
            lines.append("⏳ *Pending Access Requests:*")
            for p_id in list(self._pending_requests):
                lines.append(f"• `{p_id}` (Awaiting Decision)")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 *Manage actions per user:*")

        buttons = []

        # Pending approval buttons if any
        if self._pending_requests:
            for p_id in list(self._pending_requests):
                buttons.append([
                    {"text": f"✅ Approve #{p_id[-4:]}", "callback_data": f"action_approve_{p_id}"},
                    {"text": f"❌ Decline #{p_id[-4:]}", "callback_data": f"action_decline_{p_id}"},
                ])

        # Buttons for other admins (can demote to member or remove)
        for a in admins:
            if a.get("is_super"):
                continue
            a_id = a["chat_id"]
            short_label = a["name"].replace("@", "")[:10]
            buttons.append([
                {"text": f"🔻 Demote ({short_label})", "callback_data": f"action_demote_{a_id}"},
                {"text": f"🗑 Remove ({short_label})", "callback_data": f"action_remove_user_{a_id}"},
            ])

        # Buttons for members (can promote to admin or remove)
        for m in members:
            m_id = m["chat_id"]
            short_label = m["name"].replace("@", "")[:10]
            buttons.append([
                {"text": f"👑 Make Admin ({short_label})", "callback_data": f"action_promote_{m_id}"},
                {"text": f"🗑 Remove ({short_label})", "callback_data": f"action_remove_user_{m_id}"},
            ])

        # Action shortcuts
        buttons.append([
            {"text": "➕ Add Admin", "callback_data": "action_prompt_add_admin"},
            {"text": "➕ Add Member", "callback_data": "action_prompt_add_member"},
        ])
        buttons.append([
            {"text": "🔄 Refresh Users", "callback_data": "action_users_overview"},
            {"text": "🏠 Main Menu", "callback_data": "action_menu"},
        ])

        self.notifier.send_control_panel(
            text="\n".join(lines),
            custom_keyboard=buttons,
            chat_id=chat_id,
        )

    def handle_promote_user(self, target_id: str, callback_id: Optional[str] = None, admin_chat_id: Optional[str] = None) -> None:
        """Promote a member or user to full admin."""
        if not self.is_admin(admin_chat_id):
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="⛔️ Unauthorized")
            return

        tenants = self.db.get_all_tenants()
        tenant_id = tenants[0].id if tenants else "tenant-kamruzzaman"

        self.db.add_subscriber(
            tenant_id=tenant_id,
            chat_id=target_id,
            role="full_controller",
            receive_alerts=True,
        )

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"👑 User {target_id} promoted to Admin!")

        self.notifier.send_message(
            f"👑 *User Promoted to Admin!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Chat ID:* `{target_id}`\n"
            "Role: `full_controller (Admin)`\n"
            "This user can now access controls, manage devices, and manage users.",
            chat_id=admin_chat_id,
        )

        # Notify the promoted user and send them the admin menu & control panel
        self.notifier.send_message(
            "👑 *You have been promoted to Admin!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "You now have full administrator access to monitor Chime devices, check balances, and manage users.",
            chat_id=target_id,
        )
        self.notifier.send_bottom_menu(active_device_name=self.get_active_label(chat_id=target_id), chat_id=target_id)
        self.notifier.send_control_panel(active_device_name=self.get_active_label(chat_id=target_id), chat_id=target_id)

        # Refresh overview for admin
        self.handle_users_overview(chat_id=admin_chat_id)

    def handle_demote_user(self, target_id: str, callback_id: Optional[str] = None, admin_chat_id: Optional[str] = None) -> None:
        """Demote an admin to notification-only member."""
        if not self.is_admin(admin_chat_id):
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="⛔️ Unauthorized")
            return

        if self.allowed_chat_id and str(target_id).strip() == self.allowed_chat_id:
            msg = "🛡 *Protected Account:*\nThe Primary Super Admin cannot be demoted."
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="🛡 Super Admin cannot be demoted!")
            self.notifier.send_message(msg, chat_id=admin_chat_id)
            return

        tenants = self.db.get_all_tenants()
        tenant_id = tenants[0].id if tenants else "tenant-kamruzzaman"

        self.db.add_subscriber(
            tenant_id=tenant_id,
            chat_id=target_id,
            role="viewer_only",
            receive_alerts=True,
        )

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"🔻 User {target_id} demoted to Member.")

        self.notifier.send_message(
            f"🔻 *Admin Demoted to Member!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Chat ID:* `{target_id}`\n"
            "Role: `viewer_only (Notification Only)`\n"
            "All control menus have been disabled for this user.",
            chat_id=admin_chat_id,
        )

        # Notify demoted user and remove their keyboard!
        self.notifier.send_message(
            "ℹ️ *Account Role Updated*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "Your role has been set to `Notification Only`.\n"
            "You will continue to receive incoming payment alerts, but control menus have been removed.",
            reply_markup={"remove_keyboard": True},
            chat_id=target_id,
        )

        # Refresh overview for admin
        self.handle_users_overview(chat_id=admin_chat_id)

    def handle_remove_user(self, target_id: str, callback_id: Optional[str] = None, admin_chat_id: Optional[str] = None) -> None:
        """Remove an admin or member completely."""
        if not self.is_admin(admin_chat_id):
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="⛔️ Unauthorized")
            return

        if self.allowed_chat_id and str(target_id).strip() == self.allowed_chat_id:
            msg = "🛡 *Protected Account:*\nThe Primary Super Admin cannot be removed."
            if callback_id:
                self.notifier.answer_callback_query(callback_id, text="🛡 Super Admin cannot be removed!")
            self.notifier.send_message(msg, chat_id=admin_chat_id)
            return

        self.db.delete_subscriber_by_chat_id(target_id)
        self._pending_requests.discard(target_id)

        if callback_id:
            self.notifier.answer_callback_query(callback_id, text=f"🗑 User {target_id} removed.")

        self.notifier.send_message(
            f"🗑 *User Removed Successfully!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Chat ID `{target_id}` has been deleted from subscribers.",
            chat_id=admin_chat_id,
        )

        # Notify target user
        self.notifier.send_message(
            "🔒 *Subscription Deactivated*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "Your Chime notification access has been revoked by an administrator.",
            reply_markup={"remove_keyboard": True},
            chat_id=target_id,
        )

        # Refresh overview for admin
        self.handle_users_overview(chat_id=admin_chat_id)

    def handle_prompt_add_user(self, role: str, callback_id: Optional[str] = None, admin_chat_id: Optional[str] = None) -> None:
        """Send instructional message showing command syntax."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id)
        if role == "admin":
            msg = (
                "👑 *How to Add an Admin:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Send the command:\n"
                "`/add_admin <chat_id> [name]`\n\n"
                "_Example:_\n`/add_admin 123456789 John`\n\n"
                "The new admin will receive full device and user management privileges."
            )
        else:
            msg = (
                "👤 *How to Add a Member:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Send the command:\n"
                "`/add_member <chat_id> [name]`\n\n"
                "_Example:_\n`/add_member 123456789 John`\n\n"
                "The member will only receive payment notifications with zero control menus."
            )
        self.notifier.send_message(msg, chat_id=admin_chat_id)

    def handle_add_admin_command(self, raw_text: str, admin_chat_id: str) -> None:
        """Command to register or promote a chat_id to Admin directly."""
        if not self.is_admin(admin_chat_id):
            return
        parts = raw_text.split()
        if len(parts) < 2 or not parts[1].replace("-", "").isdigit():
            self.notifier.send_message(
                "ℹ️ *Add Admin Command Usage:*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "`/add_admin <chat_id> [username/name]`\n\n"
                "_Example:_\n`/add_admin 123456789 John`",
                chat_id=admin_chat_id,
            )
            return
        target_chat_id = parts[1].strip()
        custom_name = parts[2].strip() if len(parts) >= 3 else ""

        tenants = self.db.get_all_tenants()
        tenant_id = tenants[0].id if tenants else "tenant-kamruzzaman"

        self.db.add_subscriber(
            tenant_id=tenant_id,
            chat_id=target_chat_id,
            username=custom_name,
            role="full_controller",
            receive_alerts=True,
        )
        self._pending_requests.discard(target_chat_id)

        self.notifier.send_message(
            f"👑 *Admin Added Successfully!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Chat ID:* `{target_chat_id}`\n"
            f"👤 *Name/Tag:* {custom_name or 'Not set'}\n"
            "Role: `full_controller (Admin)`\n"
            "This user now has full admin controller rights.",
            chat_id=admin_chat_id,
        )
        # Send welcome & menu to new admin
        self.notifier.send_message(
            "👑 *Welcome! You have been granted Admin Access!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "You have full controls to monitor devices, check balances, and manage users.",
            chat_id=target_chat_id,
        )
        self.notifier.send_bottom_menu(active_device_name=self.get_active_label(chat_id=target_chat_id), chat_id=target_chat_id)
        self.notifier.send_control_panel(active_device_name=self.get_active_label(chat_id=target_chat_id), chat_id=target_chat_id)

    def handle_add_member_command(self, raw_text: str, admin_chat_id: str) -> None:
        """Command to register a chat_id as notification-only Member directly."""
        if not self.is_admin(admin_chat_id):
            return
        parts = raw_text.split()
        if len(parts) < 2 or not parts[1].replace("-", "").isdigit():
            self.notifier.send_message(
                "ℹ️ *Add Member Command Usage:*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "`/add_member <chat_id> [username/name]`\n\n"
                "_Example:_\n`/add_member 123456789 John`",
                chat_id=admin_chat_id,
            )
            return
        target_chat_id = parts[1].strip()
        custom_name = parts[2].strip() if len(parts) >= 3 else ""

        tenants = self.db.get_all_tenants()
        tenant_id = tenants[0].id if tenants else "tenant-kamruzzaman"

        self.db.add_subscriber(
            tenant_id=tenant_id,
            chat_id=target_chat_id,
            username=custom_name,
            role="viewer_only",
            receive_alerts=True,
        )
        self._pending_requests.discard(target_chat_id)

        self.notifier.send_message(
            f"✅ *Member Added Successfully!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Chat ID:* `{target_chat_id}`\n"
            f"👤 *Name/Tag:* {custom_name or 'Not set'}\n"
            "Role: `viewer_only (Notification Only - No Menu)`\n"
            "This member will now receive all new payment notifications.",
            chat_id=admin_chat_id,
        )
        # Send welcome notification to member (NO MENU!)
        self.notifier.send_message(
            "🎉 *Welcome!*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "You have been added to receive instant Chime deposit notifications.\n"
            "Whenever a new payment arrives, you will receive an alert here automatically!",
            reply_markup={"remove_keyboard": True},
            chat_id=target_chat_id,
        )

    def handle_promote_command(self, raw_text: str, admin_chat_id: str) -> None:
        """Direct text command: /promote <chat_id>."""
        if not self.is_admin(admin_chat_id):
            return
        parts = raw_text.split()
        if len(parts) < 2 or not parts[1].replace("-", "").isdigit():
            self.notifier.send_message("ℹ️ Usage: `/promote <chat_id>`", chat_id=admin_chat_id)
            return
        self.handle_promote_user(parts[1].strip(), admin_chat_id=admin_chat_id)

    def handle_demote_command(self, raw_text: str, admin_chat_id: str) -> None:
        """Direct text command: /demote <chat_id>."""
        if not self.is_admin(admin_chat_id):
            return
        parts = raw_text.split()
        if len(parts) < 2 or not parts[1].replace("-", "").isdigit():
            self.notifier.send_message("ℹ️ Usage: `/demote <chat_id>`", chat_id=admin_chat_id)
            return
        self.handle_demote_user(parts[1].strip(), admin_chat_id=admin_chat_id)

    def handle_remove_command(self, raw_text: str, admin_chat_id: str) -> None:
        """Direct text command: /remove <chat_id>."""
        if not self.is_admin(admin_chat_id):
            return
        parts = raw_text.split()
        if len(parts) < 2 or not parts[1].replace("-", "").isdigit():
            self.notifier.send_message("ℹ️ Usage: `/remove <chat_id>`", chat_id=admin_chat_id)
            return
        self.handle_remove_user(parts[1].strip(), admin_chat_id=admin_chat_id)

    def handle_help(self, callback_id: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        """Send complete bot commands guide with use cases and syntax."""
        if callback_id:
            self.notifier.answer_callback_query(callback_id, text="📖 Opening Command Guide...")
        self.notifier.send_chat_action("typing", chat_id=chat_id)

        is_admin_user = self.is_admin(chat_id)

        lines = [
            "📖 *Chime Bot Commands & Use Cases Guide*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "নিচে বটের সব কমান্ড এবং সেগুলোর কাজ বিস্তারিত দেওয়া হলো:\n",
            "🎛 *Navigation & Menus (মেনু ও নেভিগেশন)*",
            "• `/menu` বা `/start` — মূল কন্ট্রোল প্যানেল ও বাটন মেনু ওপেন করে।",
            "• `/help` — সব কমান্ডের পূর্ণাঙ্গ ব্যবহার নির্দেশিকা দেখায়।",
            "• `/hide` — টেলিগ্রাম নিচের কিবোর্ড মিনিমাইজ বা হাইড করে।\n",
            "🔄 *Monitoring & Balance (চেকিং ও রিফ্রেশ)*",
            "• `/refresh` বা `refresh` — সক্রিয় ডিভাইসে স্ক্রিন রিফ্রেশ করে লাইভ ব্যালেন্স ও ডিপোজিট চেক করে।",
            "• `/back` বা `/back_refresh` — ট্রানজ্যাকশন ডিটেইলস/সাব-স্ক্রিন থেকে ১ ধাপ ব্যাক এসে রিফ্রেশ করে।",
            "• `/balance` — অ্যাক্টিভ Chime একাউন্টের বর্তমান চেকিং ব্যালেন্স দেখায়।",
            "• `/balances` বা `/balance all` — সব ডিভাইসের ব্যালেন্স একসাথে লিস্ট আকারে দেখায়।",
            "• `/history` বা `/tx` — সাম্প্রতিক Chime ডিপোজিট লেনদেনের হিস্ট্রি দেখায়।",
            "• `/screen` বা `/screenshot` — ক্লাউড ফোনের বর্তমান লাইভ স্ক্রিনশট তুলে পাঠায়।",
            "• `/status` — ক্লাউড ফোনের পাওয়ার স্টেট ও মনিটরিং স্ট্যাটাস দেখায়।\n",
            "🛑 *Power & Monitoring Controls (ডিভাইস অন/অফ)*",
            "• `/off` বা `/stop` — বর্তমান সক্রিয় ফোন শাটডাউন ও Chime মনিটরিং বন্ধ করে।",
            "• `/off <serial>` (যেমন: `/off 226`) — নির্দিষ্ট ডিভাইস বন্ধ ও মনিটরিং পজ করে।",
            "• `/off all` বা `/pause all` — সবকটি ডিভাইস একসাথে শাটডাউন ও মনিটরিং বন্ধ করে।",
            "• `/on` বা `/power on` — সক্রিয় ক্লাউড ফোন অন করে Chime খুলে মনিটরিং শুরু করে।",
            "• `/on <serial>` (যেমন: `/on 226`) — নির্দিষ্ট ডিভাইস অন ও মনিটরিং শুরু করে।",
            "• `/on all` বা `/connect all` — সব ফোন একসাথে অন ও Chime মনিটরিং শুরু করে।\n",
            "📱 *Device Management (ডিভাইস কন্ট্রোল)*",
            "• `/devices` বা `/list` — সব GeeLark ফোনের তালিকা, স্ট্যাটাস ও বাটন দেখায়।",
            "• `/switch <serial>` (যেমন: `/switch 226`) — নির্দিষ্ট ফোনকে কারেন্ট অ্যাক্টিভ ডিভাইস করে।",
            "• `/setpin <pin>` (যেমন: `/setpin 1122`) — অ্যাক্টিভ ডিভাইসের Chime আনলক পিন সেট করে।",
            "• `/setpin <serial> <pin>` — নির্দিষ্ট ডিভাইসের Chime পিন সেট করে।",
            "• `/scan` বা `/sync` — GeeLark একাউন্ট থেকে নতুন ক্লাউড ফোন ডাটাবেজে সিঙ্ক করে।",
        ]

        if is_admin_user:
            lines.extend([
                "\n👑 *User & Admin Management (ইউজার পরিচালনা)*",
                "• `/users` বা `/admins` — অ্যাডমিন ও মেম্বারদের তালিকা ও কন্ট্রোল প্যানেল দেখায়।",
                "• `/add_admin <chat_id> [name]` — নতুন অ্যাডমিন যুক্ত করে (ফুল এক্সেস পায়)।",
                "• `/add_member <chat_id> [name]` — নতুন মেম্বার যুক্ত করে (শুধু ডিপোজিট অ্যালার্ট পাবে, নো মেনু)।",
                "• `/promote <chat_id>` — মেম্বারকে অ্যাডমিনে উন্নীত করে।",
                "• `/demote <chat_id>` — অ্যাডমিনকে সাধারণ মেম্বারে ডিমোট করে।",
                "• `/remove <chat_id>` — ইউজারকে পুরোপুরি রিমুভ করে সাবস্ক্রিপশন বাতিল করে।",
            ])

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 *Tip:* বাটন মেনুর মাধ্যমেও এই সব কমান্ড এক ক্লিকেই ব্যবহার করা যায়।")

        help_keyboard = [
            [
                {"text": "🏠 Main Menu", "callback_data": "action_menu"},
                {"text": "📋 All Devices", "callback_data": "action_devices_overview"},
            ],
            [
                {"text": "🔄 Refresh Now", "callback_data": "action_refresh"},
                {"text": "🔙 Back & Refresh", "callback_data": "action_back_refresh"},
            ],
        ]

        self.notifier.send_control_panel(
            text="\n".join(lines),
            custom_keyboard=help_keyboard,
            active_device_name=self.get_active_label(chat_id=chat_id),
            chat_id=chat_id,
        )

    def process_update(self, update: dict) -> None:
        """Dispatch incoming update to appropriate handler."""
        # Check Callback Query (Button click)
        if "callback_query" in update:
            cb = update["callback_query"]
            sender_id = str(cb.get("from", {}).get("id", ""))
            cb_id = cb.get("id")
            data = cb.get("data", "")

            access = self.check_access(sender_id)
            if not access:
                logger.warning("Unauthorized button click from %s", sender_id)
                if cb_id:
                    self.notifier.answer_callback_query(cb_id, text="🔒 Access Denied: Unregistered Chat ID")
                self.notifier.send_message(
                    "🔒 *Access Restricted*\n"
                    f"Your Telegram Chat ID (`{sender_id}`) is not registered.\n"
                    "Please contact the administrator to subscribe.",
                    chat_id=sender_id,
                )
                return

            tenant, subscriber = access

            if not tenant.is_active or tenant.subscription_status == "expired":
                if cb_id:
                    self.notifier.answer_callback_query(cb_id, text="⛔️ Subscription Expired")
                self.notifier.send_message(
                    "⛔️ *Subscription Expired*\n"
                    f"Your Chime Alert subscription for *{tenant.name}* has expired.\n"
                    "Please contact the administrator to renew your access.",
                    chat_id=sender_id,
                )
                return

            if subscriber.role == "viewer_only":
                if cb_id:
                    self.notifier.answer_callback_query(cb_id, text="⚠️ Viewer-Only: Action restricted")
                self.notifier.send_message(
                    "👁 *Viewer-Only Account*\n"
                    f"Organization: *{tenant.name}*\n"
                    "You have view-only access. Incoming deposit alerts will be delivered here automatically, but manual buttons and control actions are restricted to the account manager.",
                    chat_id=sender_id,
                )
                return

            if data == "action_refresh":
                self.handle_manual_refresh(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_back_refresh":
                self.handle_back_and_refresh(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_back":
                self.handle_back(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_balance":
                self.handle_check_balance(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_history":
                self.handle_recent_history(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_history_screen":
                self.handle_history_screenshot(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_screen":
                self.handle_screenshot(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_status":
                self.handle_status(callback_id=cb_id, chat_id=sender_id)
            elif data in ("action_devices_overview", "action_switch_device"):
                self.handle_devices_overview(callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_select_dev_"):
                target_serial = data.replace("action_select_dev_", "")
                self.handle_select_device(target_serial, callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_toggle_dev_"):
                target_serial = data.replace("action_toggle_dev_", "")
                self.handle_toggle_device(target_serial, callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_power_off_"):
                target_serial = data.replace("action_power_off_", "")
                self.handle_power_off(target_serial, callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_power_on_"):
                target_serial = data.replace("action_power_on_", "")
                self.handle_power_on(target_serial, callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_pin_prompt_"):
                target_serial = data.replace("action_pin_prompt_", "")
                self.handle_pin_prompt(target_serial, callback_id=cb_id, chat_id=sender_id)
            elif data == "action_connect_all":
                self.handle_connect_all(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_disconnect_all":
                self.handle_disconnect_all(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_scan_devices":
                self.handle_scan_devices(callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_approve_"):
                target_chat = data.replace("action_approve_", "").strip()
                self.handle_admin_approve(target_chat, callback_id=cb_id, admin_chat_id=sender_id)
            elif data.startswith("action_decline_"):
                target_chat = data.replace("action_decline_", "").strip()
                self.handle_admin_decline(target_chat, callback_id=cb_id, admin_chat_id=sender_id)
            elif data == "action_users_overview":
                self.handle_users_overview(callback_id=cb_id, chat_id=sender_id)
            elif data.startswith("action_promote_"):
                target_chat = data.replace("action_promote_", "").strip()
                self.handle_promote_user(target_chat, callback_id=cb_id, admin_chat_id=sender_id)
            elif data.startswith("action_demote_"):
                target_chat = data.replace("action_demote_", "").strip()
                self.handle_demote_user(target_chat, callback_id=cb_id, admin_chat_id=sender_id)
            elif data.startswith("action_remove_user_"):
                target_chat = data.replace("action_remove_user_", "").strip()
                self.handle_remove_user(target_chat, callback_id=cb_id, admin_chat_id=sender_id)
            elif data == "action_prompt_add_admin":
                self.handle_prompt_add_user("admin", callback_id=cb_id, admin_chat_id=sender_id)
            elif data == "action_prompt_add_member":
                self.handle_prompt_add_user("member", callback_id=cb_id, admin_chat_id=sender_id)
            elif data == "noop":
                if cb_id:
                    self.notifier.answer_callback_query(cb_id)
            elif data == "action_open_bottom_menu":
                if cb_id:
                    self.notifier.answer_callback_query(cb_id, text="🎛 Opening bottom menu...")
                self.notifier.send_bottom_menu(active_device_name=self.get_active_label(chat_id=sender_id), chat_id=sender_id)
            elif data == "action_help":
                self.handle_help(callback_id=cb_id, chat_id=sender_id)
            elif data == "action_menu":
                if cb_id:
                    self.notifier.answer_callback_query(cb_id)
                self.notifier.send_control_panel(active_device_name=self.get_active_label(chat_id=sender_id), chat_id=sender_id)
            return

        # Check Message Command
        if "message" in update:
            msg = update["message"]
            sender_id = str(msg.get("chat", {}).get("id", ""))
            access = self.check_access(sender_id)
            if not access:
                logger.info("Access request from unregistered user %s", sender_id)
                self.handle_unregistered_access_request(msg)
                return

            tenant, subscriber = access

            if not tenant.is_active or tenant.subscription_status == "expired":
                self.notifier.send_message(
                    "⛔️ *Subscription Expired*\n"
                    f"Your Chime Alert subscription for *{tenant.name}* has expired.\n"
                    "Please contact the administrator to renew your access.",
                    chat_id=sender_id,
                )
                return

            if subscriber.role == "viewer_only":
                self.notifier.send_message(
                    "👁 *Notification Account*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Your account is active in notification-only mode.\n"
                    "You will automatically receive incoming payment alerts here whenever a deposit arrives.",
                    reply_markup={"remove_keyboard": True},
                    chat_id=sender_id,
                )
                return

            raw_text = (msg.get("text") or "").strip()
            text = raw_text.lower()

            # 1. PIN configuration
            if text.startswith("/setpin") or "set pin" in text or "device pin" in text:
                self.handle_setpin(raw_text, chat_id=sender_id)
            # 2. Power Controls (Explicit On/Off)
            elif (
                text.startswith("/power off")
                or text.startswith("/power_off")
                or text.startswith("/stop")
                or text.startswith("/shutdown")
                or text == "/off"
                or text.startswith("/off ")
                or text == "off"
                or text.startswith("off ")
                or text == "stop"
                or text.startswith("stop ")
            ):
                parts = raw_text.split()
                target = None
                if text.startswith("/power") or text.startswith("power"):
                    target = parts[2] if len(parts) >= 3 else None
                else:
                    target = parts[1] if len(parts) >= 2 else None

                if target and target.lower() == "all":
                    self.handle_disconnect_all(chat_id=sender_id)
                else:
                    self.handle_power_off(target, chat_id=sender_id)
            elif (
                text.startswith("/power on")
                or text.startswith("/power_on")
                or text.startswith("/start_device")
                or (text.startswith("/power") and "on" in text)
                or text == "/on"
                or text.startswith("/on ")
                or text == "on"
                or text.startswith("on ")
            ):
                parts = raw_text.split()
                target = None
                if text.startswith("/power") or text.startswith("power"):
                    target = parts[2] if len(parts) >= 3 else None
                else:
                    target = parts[1] if len(parts) >= 2 else None

                if target and target.lower() == "all":
                    self.handle_connect_all(chat_id=sender_id)
                else:
                    self.handle_power_on(target, chat_id=sender_id)
            # 3. Batch & single connect/disconnect
            elif text in ("/connect all", "/connect_all", "connect all", "/on all", "on all", "/start all", "start all"):
                self.handle_connect_all(chat_id=sender_id)
            elif text in ("/disconnect all", "/disconnect_all", "/pause all", "disconnect all", "pause all", "/stop all", "stop all", "/off all", "off all"):
                self.handle_disconnect_all(chat_id=sender_id)
            elif text.startswith("/connect") or text.startswith("connect "):
                parts = raw_text.split()
                if len(parts) >= 2:
                    if parts[1].lower() == "all":
                        self.handle_connect_all(chat_id=sender_id)
                    else:
                        self.handle_toggle_device(parts[1], state=True, chat_id=sender_id)
                else:
                    self.handle_devices_overview(chat_id=sender_id)
            elif text.startswith("/disconnect") or text.startswith("/pause") or text == "pause" or text.startswith("pause "):
                parts = raw_text.split()
                if len(parts) >= 2:
                    if parts[1].lower() == "all":
                        self.handle_disconnect_all(chat_id=sender_id)
                    else:
                        self.handle_toggle_device(parts[1], state=False, chat_id=sender_id)
                else:
                    active = self.get_active_device(chat_id=sender_id)
                    if active:
                        self.handle_toggle_device(active.serial, state=False, chat_id=sender_id)
                    else:
                        self.handle_devices_overview(chat_id=sender_id)
            # 3. Device switching & overview
            elif text in ("/devices", "/device", "/list") or "all devices" in text:
                self.handle_devices_overview(chat_id=sender_id)
            elif text in ("/balances", "/balance all", "all balances", "all balance"):
                self.handle_all_balances(chat_id=sender_id)
            elif text.startswith("/switch") or "switch device" in text:
                parts = raw_text.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    self.handle_select_device(parts[1], chat_id=sender_id)
                else:
                    self.handle_devices_overview(chat_id=sender_id)
            elif text in ("/scan", "/sync"):
                self.handle_scan_devices(chat_id=sender_id)
            # 4. Navigation & menus
            elif text in ("/start", "/menu"):
                self.notifier.send_bottom_menu(active_device_name=self.get_active_label(chat_id=sender_id), chat_id=sender_id)
                self.notifier.send_control_panel(active_device_name=self.get_active_label(chat_id=sender_id), chat_id=sender_id)
            elif text in ("/hide", "/close", "/dismiss") or "hide" in text or "close" in text:
                self.notifier.hide_bottom_menu(chat_id=sender_id)
            elif text in ("/help", "help", "/commands", "commands", "/guide", "guide") or "command" in text or "help" in text:
                self.handle_help(chat_id=sender_id)
            # 5. Actions
            elif (
                text in ("/back_refresh", "/backrefresh", "/back", "back", "🔙 back & refresh", "back & refresh", "back and refresh", "back refresh")
                or "back and refresh" in text
                or "back & refresh" in text
                or "back refresh" in text
            ):
                self.handle_back_and_refresh(chat_id=sender_id)
            elif text in ("/android_back", "android back"):
                self.handle_back(chat_id=sender_id)
            elif text in ("/refresh", "/check") or "refresh" in text:
                self.handle_manual_refresh(chat_id=sender_id)
            elif text == "/balance" or "balance" in text:
                self.handle_check_balance(chat_id=sender_id)
            elif text in ("/history", "/transactions", "/tx") or "history" in text:
                self.handle_recent_history(chat_id=sender_id)
            elif text in ("/history_screenshot", "/histscreen"):
                self.handle_history_screenshot(chat_id=sender_id)
            elif text in ("/screenshot", "/screen") or "screenshot" in text or "capture" in text:
                self.handle_screenshot(chat_id=sender_id)
            elif text == "/status" or "status" in text:
                self.handle_status(chat_id=sender_id)
            # 6. User & Member Management
            elif text.startswith("/add_admin") or text.startswith("/addadmin"):
                self.handle_add_admin_command(raw_text, sender_id)
            elif text.startswith("/add_member") or text.startswith("/addmember"):
                self.handle_add_member_command(raw_text, sender_id)
            elif text.startswith("/promote") or text.startswith("/make_admin") or text.startswith("/makeadmin"):
                self.handle_promote_command(raw_text, sender_id)
            elif text.startswith("/demote"):
                self.handle_demote_command(raw_text, sender_id)
            elif text.startswith("/remove_user") or text.startswith("/removeuser") or text.startswith("/delete_user") or (text.startswith("/remove") and len(raw_text.split()) >= 2):
                self.handle_remove_command(raw_text, sender_id)
            elif (
                text in ("/users", "/user", "/members", "/member", "/admins", "/admin", "users", "user", "members", "member", "admins", "admin")
                or "manage user" in text
                or "all user" in text
            ):
                self.handle_users_overview(chat_id=sender_id)
            else:
                self.notifier.send_message(
                    "❓ *Command Not Recognized*\n"
                    "Tap `/menu` or use the buttons below to control cloud phones.",
                    chat_id=sender_id,
                )

    def start_polling(self) -> None:
        """Poll Telegram getUpdates continuously."""
        if not self.bot_token:
            logger.warning("TelegramBotService: Bot token not set. Polling aborted.")
            return

        self.is_running = True
        logger.info("Telegram interactive bot listener active for chat ID %s...", self.allowed_chat_id)

        while self.is_running:
            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            params = {"offset": self.last_update_id + 1, "timeout": 20}
            try:
                res = self.session.get(url, params=params, timeout=25)
                res_data = res.json()
                if res_data.get("ok"):
                    for update in res_data.get("result", []):
                        self.last_update_id = max(self.last_update_id, update.get("update_id", 0))
                        try:
                            self.process_update(update)
                        except Exception as err:
                            logger.error("Error processing Telegram update %s: %s", update.get("update_id"), err, exc_info=True)
            except requests.RequestException:
                time.sleep(2)
            except Exception as e:
                logger.error("Error in Telegram bot loop: %s", e)
                time.sleep(2)

    def stop(self) -> None:
        """Stop the polling loop."""
        self.is_running = False
