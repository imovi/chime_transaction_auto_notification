"""Core Chime continuous monitoring engine."""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Dict, List, Optional

from config import Config, default_config
from geelark.client import GeeLarkClient
from geelark.phone import PhoneManager
from geelark.shell import ShellManager
from automation.chime_flow import ChimeAutomationFlow
from automation.device_manager import ChimeDevice, DeviceManager
from automation.seen_store import SeenTransactionStore
from automation.transaction_parser import ParsedTransaction, TransactionParser
from database.db import Database
from telegram_notifier import DepositAlert, TelegramNotifier

logger = logging.getLogger(__name__)


class ChimeMonitor:
    """Continuously monitors registered cloud phone Chime accounts for new incoming deposits."""

    def __init__(
        self,
        config: Optional[Config] = None,
        client: Optional[GeeLarkClient] = None,
        notifier: Optional[TelegramNotifier] = None,
        seen_store: Optional[SeenTransactionStore] = None,
        device_mgr: Optional[DeviceManager] = None,
        db: Optional[Database] = None,
    ) -> None:
        self.config = config or default_config
        self.client = client or GeeLarkClient(self.config)
        self.phone_mgr = PhoneManager(self.client)
        self.shell_mgr = ShellManager(self.client)
        self.notifier = notifier or TelegramNotifier(
            self.config.telegram_bot_token,
            self.config.telegram_chat_id,
        )
        self.seen_store = seen_store or SeenTransactionStore()
        self.db = db or Database()
        self.device_mgr = device_mgr or DeviceManager(seed_config=self.config, db=self.db)
        self.resolved_phone_id: Optional[str] = None
        self.resolved_phone_name: Optional[str] = None
        self._flows: Dict[str, ChimeAutomationFlow] = {}
        self._pin_notified: set[str] = set()
        self.latest_balance: Optional[str] = None
        self.latest_balance_time: float = 0.0
        self.on_transaction = None

    def get_flow(self, phone_id: Optional[str] = None) -> ChimeAutomationFlow:
        """Get or create ChimeAutomationFlow instance for specified phone."""
        p_id = phone_id or self.resolve_target_device()
        if p_id not in self._flows:
            self._flows[p_id] = ChimeAutomationFlow(p_id, self.client)
        return self._flows[p_id]

    @property
    def flow(self) -> ChimeAutomationFlow:
        """Backward compatible flow property."""
        return self.get_flow()

    def resolve_target_device(self) -> str:
        """Find cloud phone ID by configured ID, serial, or name."""
        if self.resolved_phone_id:
            return self.resolved_phone_id

        if self.config.target_phone_id:
            self.resolved_phone_id = self.config.target_phone_id
            self.resolved_phone_name = self.config.target_phone_name or self.config.target_phone_id
            return self.resolved_phone_id

        res = self.phone_mgr.list_phones(page=1, page_size=100)
        items: List[Dict] = res.get("items", [])

        target_serial = self.config.target_phone_serial
        target_name = self.config.target_phone_name

        for phone in items:
            p_id = phone.get("id")
            p_serial = str(phone.get("serialNo", ""))
            p_name = str(phone.get("serialName", ""))

            if target_serial and p_serial == target_serial:
                self.resolved_phone_id = p_id
                self.resolved_phone_name = p_name or f"#{p_serial}"
                logger.info("Matched device by serial #%s -> ID %s (%s)", target_serial, p_id, p_name)
                return p_id

            if target_name and target_name.lower() in p_name.lower():
                self.resolved_phone_id = p_id
                self.resolved_phone_name = p_name
                logger.info("Matched device by name '%s' -> ID %s", target_name, p_id)
                return p_id

        if items:
            fallback = items[0]
            self.resolved_phone_id = fallback.get("id")
            self.resolved_phone_name = fallback.get("serialName", self.resolved_phone_id)
            logger.warning("No exact match for %s / %s. Defaulting to first device: %s (%s)",
                           target_serial, target_name, self.resolved_phone_id, self.resolved_phone_name)
            return self.resolved_phone_id

        raise RuntimeError("No cloud phones found in your GeeLark account.")

    def ensure_running(self, phone_id: str) -> None:
        """Ensure device is powered on."""
        status_list = self.phone_mgr.query_status([phone_id])
        if not status_list or status_list[0].get("status") != 0:
            logger.info("Target phone %s is not running. Initiating startup...", phone_id)
            self.phone_mgr.start_phone([phone_id])
            for _ in range(20):
                time.sleep(3)
                st = self.phone_mgr.query_status([phone_id])
                if st and st[0].get("status") == 0:
                    logger.info("Phone %s is now active.", phone_id)
                    return
            raise TimeoutError(f"Phone {phone_id} failed to reach running state.")

    def poll_single_device(self, dev: ChimeDevice, dry_run: bool = False) -> List[ParsedTransaction]:
        """Check notifications and UI deposits for a single device, dispatching alerts immediately."""
        phone_id = dev.device_id
        flow = self.get_flow(phone_id)

        # 0. Only monitor if phone is already powered on
        try:
            st_list = self.phone_mgr.query_status([phone_id])
            if not st_list or st_list[0].get("status") != 0:
                logger.debug("Phone #%s (%s) is not running (status=%s). Skipping poll pass.",
                             dev.serial, dev.name, st_list[0].get("status") if st_list else "unknown")
                return []
        except Exception as e:
            logger.debug("Could not query power status for phone #%s (%s): %s", dev.serial, dev.name, e)
            return []

        # 1. Ensure Chime app is open and unlocked with device-specific PIN
        elements: list[str] = []
        try:
            unlock_res = flow.ensure_open_and_unlocked(pin=dev.pin)
            elements = unlock_res.get("elements", [])
            if unlock_res.get("needs_pin"):
                if dev.device_id not in self._pin_notified:
                    self.notifier.send_pin_request(dev.serial, dev.name)
                    self._pin_notified.add(dev.device_id)
                logger.warning("Device #%s (%s) requires PIN. Skipping poll pass.", dev.serial, dev.name)
                return []
            else:
                self._pin_notified.discard(dev.device_id)
        except Exception as e:
            logger.debug("Error checking foreground/PIN on %s: %s", dev.name, e)

        dev_detected: List[ParsedTransaction] = []
        new_events: List[tuple] = []

        # 2. Check native Android push notifications for this device
        try:
            notif_dump = self.shell_mgr.execute(phone_id, "dumpsys notification --noredact")
            notif_txs = TransactionParser.parse_notification_dump(notif_dump)
            for tx in notif_txs:
                tx_hash = self.seen_store.compute_hash(
                    f"{dev.serial}:{tx.sender}", tx.amount, tx.time_str, tx.note, balance=tx.balance_after
                )
                if self.seen_store.check_and_mark_seen(tx_hash):
                    new_events.append((tx, tx_hash))
        except Exception as e:
            logger.debug("Notification check exception on %s: %s", dev.name, e)

        # 3. Check active screen UI elements (reuse elements from unlock_res)
        try:
            if not elements:
                elements = self.shell_mgr.get_screen_elements(phone_id)
            current_balance = TransactionParser.extract_balance_from_elements(elements)
            if current_balance:
                self.device_mgr.update_balance(dev.device_id, current_balance)
                self.latest_balance = current_balance
                self.latest_balance_time = time.time()

            ui_txs = TransactionParser.parse_elements(elements)
            for tx in ui_txs:
                balance = tx.balance_after or current_balance or ""
                tx_hash = self.seen_store.compute_hash(
                    f"{dev.serial}:{tx.sender}", tx.amount, tx.time_str, tx.note, balance=balance
                )
                if self.seen_store.check_and_mark_seen(tx_hash):
                    tx_with_bal = ParsedTransaction(
                        sender=tx.sender,
                        amount=tx.amount,
                        time_str=tx.time_str,
                        note=tx.note,
                        balance_after=balance,
                        source="ui",
                    )
                    new_events.append((tx_with_bal, tx_hash))
        except Exception as e:
            logger.debug("UI hierarchy check exception on %s: %s", dev.name, e)

        # 4. Dispatch alerts for all newly detected deposits on this device immediately
        for tx, cur_tx_hash in new_events:
            dev_detected.append(tx)
            logger.info("🎉 NEW DEPOSIT ON #%s (%s): %s (%s)", dev.serial, dev.name, tx.amount, tx.sender)

            # Automatic screenshots disabled; users can click [📸 View Screenshot] on demand
            screenshot_url = None

            alert = DepositAlert(
                sender=tx.sender,
                amount=tx.amount,
                time_str=tx.time_str or "Just now",
                note=tx.note,
                balance=tx.balance_after,
                device_name=f"#{dev.serial} {dev.name}",
                screenshot_url=screenshot_url,
            )

            # 1. Record transaction for history
            try:
                t_id = self.db.get_primary_tenant_id() if hasattr(self.db, "get_primary_tenant_id") else "tenant-kamruzzaman"
                self.db.check_and_record_transaction(
                    tenant_id=t_id,
                    device_id=dev.device_id,
                    tx_hash=cur_tx_hash,
                    sender=tx.sender,
                    amount=tx.amount,
                    time_str=tx.time_str or "Just now",
                    note=tx.note or "",
                    balance_after=tx.balance_after or "",
                )
            except Exception as e:
                logger.debug("Could not record transaction in DB: %s", e)

            # 2. Target Telegram chats: master chat + all active subscribers configured to receive alerts
            target_chats_set = set()
            if self.config.telegram_chat_id:
                target_chats_set.add(str(self.config.telegram_chat_id))
            try:
                subs = self.db.get_all_subscribers(active_only=True)
                for s in subs:
                    if s.receive_alerts and s.chat_id:
                        target_chats_set.add(str(s.chat_id))
            except Exception as e:
                logger.debug("Error querying subscribers for alert broadcast: %s", e)
            target_chats = list(target_chats_set)

            if dry_run:
                print("\n[DRY RUN ALERT PREVIEW]")
                print(self.notifier.format_message(alert))
            else:
                if target_chats:
                    self.notifier.send_deposit_alert(alert, target_chat_ids=target_chats)
                else:
                    logger.info("No active alert subscribers configured for device #%s. Alert not sent.", dev.serial)

        return dev_detected

    def poll_once(self, dry_run: bool = False) -> List[ParsedTransaction]:
        """Perform concurrent detection passes across all enabled registered devices."""
        devices = self.device_mgr.get_all_devices(enabled_only=True)
        if not devices:
            all_devices = self.device_mgr.get_all_devices(enabled_only=False)
            if all_devices:
                # Registered devices exist but all are disabled/paused. Do NOT auto-power on!
                logger.debug("No devices enabled for monitoring. Skipping poll pass.")
                return []

            phone_id = self.resolve_target_device()
            devices = [
                ChimeDevice(
                    device_id=phone_id,
                    serial=self.config.target_phone_serial or "1",
                    name=self.resolved_phone_name or "Device-1",
                    pin=self.config.app_pin,
                    enabled=True,
                )
            ]

        detected_all: List[ParsedTransaction] = []
        max_workers = min(len(devices), 8) if devices else 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_dev = {
                executor.submit(self.poll_single_device, dev, dry_run): dev
                for dev in devices
            }
            for future in concurrent.futures.as_completed(future_to_dev):
                dev = future_to_dev[future]
                try:
                    dev_detected = future.result()
                    detected_all.extend(dev_detected)
                except Exception as e:
                    logger.error("Error polling device #%s (%s): %s", dev.serial, dev.name, e)

        return detected_all

    def refresh_screen(self, phone_id: Optional[str] = None) -> None:
        """Perform pull-down gesture to refresh transactions screen on target or all devices concurrently."""
        p_ids = [phone_id] if phone_id else [d.device_id for d in self.device_mgr.get_all_devices(enabled_only=True)]
        if not p_ids:
            return

        def _swipe(p_id: str) -> None:
            try:
                self.shell_mgr.swipe(p_id, 360, 500, 360, 1100, 400)
            except Exception as e:
                logger.debug("Pull to refresh skipped on %s: %s", p_id, e)

        max_workers = min(len(p_ids), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(_swipe, p_ids))

    def run_forever(self, interval_seconds: Optional[int] = None, dry_run: bool = False) -> None:
        """Continuous multi-device monitoring loop."""
        interval = interval_seconds or self.config.poll_interval_seconds
        devices = self.device_mgr.get_all_devices(enabled_only=True)

        logger.info("Chime Multi-Device Monitor active across %d device(s). Polling every %ds...",
                    len(devices), interval)

        while True:
            try:
                # 1. Pull down to refresh Chime screen
                self.refresh_screen()
                time.sleep(1.0)

                # 2. Check for newly arrived deposits across all devices
                new_txs = self.poll_once(dry_run=dry_run)
                if new_txs:
                    logger.info("Processed %d new deposits across devices.", len(new_txs))

                time.sleep(interval)
            except KeyboardInterrupt:
                logger.info("Monitoring terminated by user.")
                break
            except Exception as e:
                logger.error("Error during monitoring loop: %s", e, exc_info=True)
                time.sleep(interval)
