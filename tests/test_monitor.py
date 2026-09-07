"""Unit tests for transaction parser, deduplication store, and telegram alerts."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from automation.seen_store import SeenTransactionStore
from automation.transaction_parser import TransactionParser
from telegram_notifier import DepositAlert, TelegramNotifier

SAMPLE_CHIME_UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout">
    <node index="0" text="Checking" class="android.widget.TextView" />
    <node index="1" text="$129.80" class="android.widget.TextView" />
    <node index="2" text="Transactions" class="android.widget.TextView" />
    <node index="3" text="Today" class="android.widget.TextView" />
    <node index="4" text="Oxylabs" class="android.widget.TextView" />
    <node index="5" text="-$17.39" class="android.widget.TextView" />
    <node index="6" text="Yesterday" class="android.widget.TextView" />
    <node index="7" text="Transfer from Jessie B." class="android.widget.TextView" />
    <node index="8" text="3:03 PM • Pay Anyone Transfer" class="android.widget.TextView" />
    <node index="9" text="For: Nails 💅" class="android.widget.TextView" />
    <node index="10" text="+$147.00" class="android.widget.TextView" />
    <node index="11" text="$147.20" class="android.widget.TextView" />
    <node index="12" text="Transfer to Marilyn G." class="android.widget.TextView" />
    <node index="13" text="-$90.00" class="android.widget.TextView" />
    <node index="14" text="Transfer from Kinzlie C." class="android.widget.TextView" />
    <node index="15" text="7:32 AM • Pay Anyone Transfer" class="android.widget.TextView" />
    <node index="16" text="For: Tickets!" class="android.widget.TextView" />
    <node index="17" text="+$120.00" class="android.widget.TextView" />
    <node index="18" text="$120.20" class="android.widget.TextView" />
  </node>
</hierarchy>
"""


class TestTransactionParser(unittest.TestCase):
    """Test suite for TransactionParser."""

    def test_parse_ui_hierarchy_extracts_only_inbound_deposits(self) -> None:
        txs = TransactionParser.parse_ui_hierarchy(SAMPLE_CHIME_UI_XML)
        self.assertEqual(len(txs), 2)

        # First deposit: Jessie B. +$147.00
        tx1 = txs[0]
        self.assertEqual(tx1.sender, "Transfer from Jessie B.")
        self.assertEqual(tx1.amount, "+$147.00")
        self.assertEqual(tx1.note, "For: Nails 💅")
        self.assertIn("3:03 PM", tx1.time_str)
        self.assertEqual(tx1.balance_after, "$147.20")

        # Second deposit: Kinzlie C. +$120.00
        tx2 = txs[1]
        self.assertEqual(tx2.sender, "Transfer from Kinzlie C.")
        self.assertEqual(tx2.amount, "+$120.00")
        self.assertEqual(tx2.note, "For: Tickets!")
        self.assertIn("7:32 AM", tx2.time_str)
        self.assertEqual(tx2.balance_after, "$120.20")

    def test_extract_checking_balance(self) -> None:
        balance = TransactionParser.extract_checking_balance(SAMPLE_CHIME_UI_XML)
        self.assertEqual(balance, "$129.80")

    def test_parse_notification_dump(self) -> None:
        dump = """
        NotificationRecord(pkg=com.onedebit.chime id=101)
          tickerText=null
          android.title=Transfer received
          android.text=Jessie B. sent you $147.00
        """
        txs = TransactionParser.parse_notification_dump(dump)
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].sender, "Transfer from Jessie B.")
        self.assertEqual(txs[0].amount, "+$147.00")

    def test_parse_elements_consecutive_same_amount_and_sender(self) -> None:
        """Verify that two identical amount transfers from the same sender are both extracted accurately without bleeding."""
        elements = [
            "Checking",
            "$339.80",
            "Transactions",
            "Today",
            "&#128111;, Transfer from Aeriel D., 10:24 AM • Pay Anyone Transfer, For: 💰, +$50.00, $339.80",
            "&#128111;",
            "Transfer from Aeriel D.",
            "10:24 AM • Pay Anyone Transfer",
            "For: 💰",
            "+$50.00",
            "$339.80",
            "&#128111;, Transfer from Aeriel D., 10:23 AM • Pay Anyone Transfer, For: 💰, +$50.00, $289.80",
            "&#128111;",
            "Transfer from Aeriel D.",
            "10:23 AM • Pay Anyone Transfer",
            "For: 💰",
            "+$50.00",
            "$289.80",
        ]
        txs = TransactionParser.parse_elements(elements)
        self.assertEqual(len(txs), 2)

        self.assertEqual(txs[0].sender, "Transfer from Aeriel D.")
        self.assertEqual(txs[0].amount, "+$50.00")
        self.assertEqual(txs[0].time_str, "10:24 AM • Pay Anyone Transfer")
        self.assertEqual(txs[0].note, "For: 💰")
        self.assertEqual(txs[0].balance_after, "$339.80")

        self.assertEqual(txs[1].sender, "Transfer from Aeriel D.")
        self.assertEqual(txs[1].amount, "+$50.00")
        self.assertEqual(txs[1].time_str, "10:23 AM • Pay Anyone Transfer")
        self.assertEqual(txs[1].note, "For: 💰")
        self.assertEqual(txs[1].balance_after, "$289.80")


class TestSeenTransactionStore(unittest.TestCase):
    """Test suite for SeenTransactionStore."""

    def test_deduplication_and_persistence(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            store = SeenTransactionStore(temp_path)
            h1 = store.compute_hash("Transfer from Jessie B.", "+$147.00", "3:03 PM", "Nails")
            h2 = store.compute_hash("Transfer from Kinzlie C.", "+$120.00", "7:32 AM", "Tickets")

            self.assertFalse(store.is_seen(h1))
            store.mark_seen(h1)
            self.assertTrue(store.is_seen(h1))
            self.assertFalse(store.is_seen(h2))

            # Reload from disk into new store instance
            store2 = SeenTransactionStore(temp_path)
            self.assertTrue(store2.is_seen(h1))
            self.assertFalse(store2.is_seen(h2))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_deduplication_allows_same_sender_and_amount_with_different_balance_or_time(self) -> None:
        """Verify that identical sender, amount, and note do NOT collide if balance or time differs."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            store = SeenTransactionStore(temp_path)
            h1 = store.compute_hash("Transfer from Aeriel D.", "+$50.00", "10:24 AM", "For: 💰", "$339.80")
            h2 = store.compute_hash("Transfer from Aeriel D.", "+$50.00", "10:23 AM", "For: 💰", "$289.80")

            self.assertNotEqual(h1, h2)
            self.assertTrue(store.check_and_mark_seen(h1))
            self.assertTrue(store.check_and_mark_seen(h2))
            # Consecutive check should return False (already seen)
            self.assertFalse(store.check_and_mark_seen(h1))
            self.assertFalse(store.check_and_mark_seen(h2))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestTelegramNotifier(unittest.TestCase):
    """Test suite for TelegramNotifier."""

    def test_message_formatting(self) -> None:
        notifier = TelegramNotifier("fake_token", "fake_chat")
        alert = DepositAlert(
            sender="Transfer from Jessie B.",
            amount="+$147.00",
            time_str="3:03 PM",
            note="For: Nails 💅",
            balance="$129.80",
            device_name="#226 Katie-Smith-18",
        )
        msg = notifier.format_message(alert)
        self.assertIn("Jessie B.", msg)
        self.assertIn("+$147.00", msg)
        self.assertIn("Nails 💅", msg)
        self.assertIn("$129.80", msg)
        self.assertIn("#226 Katie-Smith-18", msg)

    @patch("requests.Session.post")
    def test_send_message_success(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier("fake_token", "fake_chat")
        result = notifier.send_message("Test alert")
        self.assertTrue(result)
        mock_post.assert_called_once()


class TestHistoryAndBotService(unittest.TestCase):
    """Test suite for history extraction and bot command routing."""

    def test_extract_all_history_transactions(self) -> None:
        elements = [
            "Checking",
            "$129.80",
            "Transactions",
            "Today",
            "Oxylabs",
            "-$17.39",
            "Yesterday",
            "Transfer from Jessie B.",
            "3:03 PM • Pay Anyone Transfer",
            "For: Nails 💅",
            "+$147.00",
            "$147.20",
            "Transfer to Marilyn G.",
            "-$90.00",
        ]
        history = TransactionParser.extract_all_history_transactions(elements)
        self.assertEqual(len(history), 3)

        # First: Oxylabs -$17.39 (outbound)
        self.assertFalse(history[0]["is_inbound"])
        self.assertEqual(history[0]["amount"], "-$17.39")
        self.assertEqual(history[0]["title"], "Oxylabs")

        # Second: Jessie B. +$147.00 (inbound)
        self.assertTrue(history[1]["is_inbound"])
        self.assertEqual(history[1]["amount"], "+$147.00")
        self.assertEqual(history[1]["title"], "Transfer from Jessie B.")
        self.assertIn("Nails 💅", history[1]["note"])

        # Third: Transfer to Marilyn G. -$90.00 (outbound)
        self.assertFalse(history[2]["is_inbound"])
        self.assertEqual(history[2]["amount"], "-$90.00")
        self.assertEqual(history[2]["title"], "Transfer to Marilyn G.")

    @patch("automation.telegram_bot_service.TelegramBotService.handle_recent_history")
    def test_bot_service_callback_routing(self, mock_handle_history: MagicMock) -> None:
        from automation.telegram_bot_service import TelegramBotService
        mock_monitor = MagicMock()
        service = TelegramBotService(mock_monitor, db=MagicMock())
        service.allowed_chat_id = "12345"

        update = {
            "callback_query": {
                "id": "cb_999",
                "from": {"id": 12345},
                "data": "action_history",
            }
        }
        service.process_update(update)
        mock_handle_history.assert_called_once_with(callback_id="cb_999", chat_id="12345")


    @patch("automation.telegram_bot_service.TelegramBotService.handle_history_screenshot")
    def test_bot_service_history_screenshot_routing(self, mock_handle_screen: MagicMock) -> None:
        from automation.telegram_bot_service import TelegramBotService
        mock_monitor = MagicMock()
        service = TelegramBotService(mock_monitor, db=MagicMock())
        service.allowed_chat_id = "12345"

        update = {
            "callback_query": {
                "id": "cb_888",
                "from": {"id": 12345},
                "data": "action_history_screen",
            }
        }
        service.process_update(update)
        mock_handle_screen.assert_called_once_with(callback_id="cb_888", chat_id="12345")

    @patch("automation.telegram_bot_service.TelegramBotService.handle_devices_overview")
    def test_bot_service_devices_overview_routing(self, mock_overview: MagicMock) -> None:
        from automation.telegram_bot_service import TelegramBotService
        mock_monitor = MagicMock()
        service = TelegramBotService(mock_monitor, db=MagicMock())
        service.allowed_chat_id = "12345"

        update = {
            "callback_query": {
                "id": "cb_777",
                "from": {"id": 12345},
                "data": "action_devices_overview",
            }
        }
        service.process_update(update)
        mock_overview.assert_called_once_with(callback_id="cb_777", chat_id="12345")

    @patch("automation.telegram_bot_service.TelegramBotService.handle_setpin")
    def test_bot_service_setpin_routing(self, mock_setpin: MagicMock) -> None:
        from automation.telegram_bot_service import TelegramBotService
        mock_monitor = MagicMock()
        service = TelegramBotService(mock_monitor, db=MagicMock())
        service.allowed_chat_id = "12345"

        update = {
            "message": {
                "chat": {"id": 12345},
                "text": "/setpin 226 5566",
            }
        }
        service.process_update(update)
        mock_setpin.assert_called_once_with("/setpin 226 5566", chat_id="12345")

    @patch("automation.telegram_bot_service.TelegramBotService.handle_connect_all")
    def test_bot_service_connect_all_routing(self, mock_connect_all: MagicMock) -> None:
        from automation.telegram_bot_service import TelegramBotService
        mock_monitor = MagicMock()
        service = TelegramBotService(mock_monitor, db=MagicMock())
        service.allowed_chat_id = "12345"

        # 1. Callback query
        update_cb = {
            "callback_query": {
                "id": "cb_conn_all",
                "from": {"id": 12345},
                "data": "action_connect_all",
            }
        }
        service.process_update(update_cb)
        mock_connect_all.assert_called_with(callback_id="cb_conn_all", chat_id="12345")

        # 2. Text command
        update_msg = {
            "message": {
                "chat": {"id": 12345},
                "text": "/connect all",
            }
        }
        service.process_update(update_msg)
        self.assertEqual(mock_connect_all.call_count, 2)

    @patch("automation.telegram_bot_service.TelegramBotService.handle_disconnect_all")
    def test_bot_service_disconnect_all_routing(self, mock_disconnect_all: MagicMock) -> None:
        from automation.telegram_bot_service import TelegramBotService
        mock_monitor = MagicMock()
        service = TelegramBotService(mock_monitor, db=MagicMock())
        service.allowed_chat_id = "12345"

        update_cb = {
            "callback_query": {
                "id": "cb_disc_all",
                "from": {"id": 12345},
                "data": "action_disconnect_all",
            }
        }
        service.process_update(update_cb)
        mock_disconnect_all.assert_called_once_with(callback_id="cb_disc_all", chat_id="12345")


class TestMultiDeviceAndConcurrency(unittest.TestCase):
    """Test suite for multi-device batch management and concurrent polling."""

    def test_check_and_mark_seen_atomicity(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            store = SeenTransactionStore(temp_path)
            h = store.compute_hash("Alice", "+$50.00", "12:00 PM")
            self.assertTrue(store.check_and_mark_seen(h))
            self.assertFalse(store.check_and_mark_seen(h))
            self.assertTrue(store.is_seen(h))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_device_manager_batch_toggles_and_summary(self) -> None:
        from automation.device_manager import ChimeDevice, DeviceManager
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            mgr = DeviceManager(temp_path)
            d1 = ChimeDevice("id1", "1", "Dev1", pin="1111", enabled=False)
            d2 = ChimeDevice("id2", "2", "Dev2", pin=None, enabled=False)
            mgr.add_or_update_device(d1)
            mgr.add_or_update_device(d2)

            summary = mgr.get_device_summary()
            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["enabled"], 0)
            self.assertEqual(summary["pinned"], 1)

            # Connect all
            mgr.set_all_devices_enabled(True)
            self.assertEqual(mgr.get_device_summary()["enabled"], 2)

            # Disconnect all
            mgr.set_all_devices_enabled(False)
            self.assertEqual(mgr.get_device_summary()["enabled"], 0)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_chime_monitor_concurrent_poll_once(self) -> None:
        from automation.chime_monitor import ChimeMonitor
        from automation.device_manager import ChimeDevice
        from automation.transaction_parser import ParsedTransaction

        mock_config = MagicMock()
        mock_monitor = ChimeMonitor(config=mock_config, notifier=MagicMock(), seen_store=MagicMock())
        d1 = ChimeDevice("id1", "1", "Phone1", pin="1111", enabled=True)
        d2 = ChimeDevice("id2", "2", "Phone2", pin="2222", enabled=True)

        mock_monitor.device_mgr = MagicMock()
        mock_monitor.device_mgr.get_all_devices.return_value = [d1, d2]

        tx1 = ParsedTransaction(sender="Alice", amount="+$10.00", time_str="1:00 PM")
        tx2 = ParsedTransaction(sender="Bob", amount="+$20.00", time_str="2:00 PM")

        def mock_poll_single(dev: ChimeDevice, dry_run: bool = False):
            return [tx1] if dev.serial == "1" else [tx2]

        mock_monitor.poll_single_device = MagicMock(side_effect=mock_poll_single)
        detected = mock_monitor.poll_once()

        self.assertEqual(len(detected), 2)
        self.assertEqual(mock_monitor.poll_single_device.call_count, 2)

    def test_poll_once_skips_when_all_devices_disabled(self) -> None:
        """Verify poll_once does NOT auto-start devices when all devices are paused/disabled."""
        from automation.chime_monitor import ChimeMonitor
        from automation.device_manager import ChimeDevice

        mock_config = MagicMock()
        mock_monitor = ChimeMonitor(config=mock_config, notifier=MagicMock(), seen_store=MagicMock())
        d1 = ChimeDevice("id1", "1", "Phone1", pin="1111", enabled=False)

        mock_monitor.device_mgr = MagicMock()
        mock_monitor.device_mgr.get_all_devices.side_effect = lambda enabled_only=True: [] if enabled_only else [d1]
        mock_monitor.poll_single_device = MagicMock()
        mock_monitor.phone_mgr = MagicMock()

        detected = mock_monitor.poll_once()

        self.assertEqual(detected, [])
        mock_monitor.poll_single_device.assert_not_called()
        mock_monitor.phone_mgr.start_phone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
