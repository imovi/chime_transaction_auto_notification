"""Unit tests for Telegram persistent bottom menu and message routing."""

import unittest
from unittest.mock import MagicMock

from automation.telegram_bot_service import TelegramBotService
from telegram_notifier import TelegramNotifier


class TestBottomMenu(unittest.TestCase):
    """Test suite verifying bottom ReplyKeyboardMarkup structure and routing."""

    def test_persistent_reply_keyboard_structure(self) -> None:
        """Verify ReplyKeyboardMarkup format allows minimizing (is_persistent=False)."""
        kb = TelegramNotifier.get_persistent_reply_keyboard()
        self.assertIn("keyboard", kb)
        self.assertTrue(kb.get("is_persistent"))  # True pins Telegram keyboard open permanently
        self.assertTrue(kb.get("resize_keyboard"))

        self.assertEqual(len(kb["keyboard"]), 6)

        # Check button rows
        flat_buttons = [btn["text"] for row in kb["keyboard"] for btn in row]
        self.assertIn("🔄 Refresh & Check Now", flat_buttons)
        self.assertIn("💳 Check Balance", flat_buttons)
        self.assertIn("📜 Recent History", flat_buttons)
        self.assertIn("📸 Screen Capture", flat_buttons)
        self.assertIn("📱 Device Status", flat_buttons)
        self.assertIn("📱 Switch Device", flat_buttons)
        self.assertIn("📋 All Devices", flat_buttons)
        self.assertIn("👥 Manage Users", flat_buttons)
        self.assertIn("🔐 Set Device PIN", flat_buttons)
        self.assertIn("❌ Hide Menu", flat_buttons)

    def test_routing_bottom_menu_buttons(self) -> None:
        """Verify bot service maps bottom button text strings to handlers."""
        monitor_mock = MagicMock()
        service = TelegramBotService(monitor=monitor_mock, db=MagicMock())
        service.allowed_chat_id = "12345"
        service.notifier = MagicMock()

        # Mock handlers
        service.handle_manual_refresh = MagicMock()
        service.handle_check_balance = MagicMock()
        service.handle_recent_history = MagicMock()
        service.handle_screenshot = MagicMock()
        service.handle_status = MagicMock()
        service.handle_devices_overview = MagicMock()
        service.handle_setpin = MagicMock()

        # 1. Refresh
        service.process_update({"message": {"chat": {"id": 12345}, "text": "🔄 Refresh"}})
        service.handle_manual_refresh.assert_called_once()

        # 2. Balance
        service.process_update({"message": {"chat": {"id": 12345}, "text": "💳 Balance"}})
        service.handle_check_balance.assert_called_once()

        # 3. History
        service.process_update({"message": {"chat": {"id": 12345}, "text": "📜 History"}})
        service.handle_recent_history.assert_called_once()

        # 4. Screenshot
        service.process_update({"message": {"chat": {"id": 12345}, "text": "📸 Screenshot"}})
        service.handle_screenshot.assert_called_once()

        # 5. Status
        service.process_update({"message": {"chat": {"id": 12345}, "text": "📱 Device Status"}})
        service.handle_status.assert_called_once()

        # 6. Switch Device
        service.process_update({"message": {"chat": {"id": 12345}, "text": "📱 Switch Device"}})
        service.handle_devices_overview.assert_called_once()

        # 7. Set PIN
        service.process_update({"message": {"chat": {"id": 12345}, "text": "🔐 Set Device PIN"}})
        service.handle_setpin.assert_called_once()

        # 8. Hide Menu
        service.process_update({"message": {"chat": {"id": 12345}, "text": "❌ Hide Menu"}})
        service.notifier.hide_bottom_menu.assert_called_once()

        # 9. Callback to Re-open Bottom Menu
        service.process_update({"callback_query": {"id": "cb1", "from": {"id": 12345}, "data": "action_open_bottom_menu"}})
        service.notifier.send_bottom_menu.assert_called_once()

        # 10. Callback to Toggle Device Connect/Disconnect
        service.handle_toggle_device = MagicMock()
        service.process_update({"callback_query": {"id": "cb2", "from": {"id": 12345}, "data": "action_toggle_dev_226"}})
        service.handle_toggle_device.assert_called_once_with("226", callback_id="cb2", chat_id="12345")

        # 11. Callback to Scan Devices from GeeLark
        service.handle_scan_devices = MagicMock()
        service.process_update({"callback_query": {"id": "cb3", "from": {"id": 12345}, "data": "action_scan_devices"}})
        service.handle_scan_devices.assert_called_once_with(callback_id="cb3", chat_id="12345")

        # 12. Power Off & Power On Callbacks
        service.handle_power_off = MagicMock()
        service.handle_power_on = MagicMock()
        service.process_update({"callback_query": {"id": "cb4", "from": {"id": 12345}, "data": "action_power_off_226"}})
        service.handle_power_off.assert_called_once_with("226", callback_id="cb4", chat_id="12345")

        service.process_update({"callback_query": {"id": "cb5", "from": {"id": 12345}, "data": "action_power_on_226"}})
        service.handle_power_on.assert_called_once_with("226", callback_id="cb5", chat_id="12345")

        # 13. Power Text Commands
        service.process_update({"message": {"chat": {"id": 12345}, "text": "/power off 226"}})
        service.handle_power_off.assert_called_with("226", chat_id="12345")

        service.process_update({"message": {"chat": {"id": 12345}, "text": "/stop 226"}})
        service.handle_power_off.assert_called_with("226", chat_id="12345")

        service.process_update({"message": {"chat": {"id": 12345}, "text": "/off"}})
        service.handle_power_off.assert_called_with(None, chat_id="12345")

        service.handle_disconnect_all = MagicMock()
        service.handle_connect_all = MagicMock()

        service.process_update({"message": {"chat": {"id": 12345}, "text": "/stop all"}})
        service.handle_disconnect_all.assert_called_once_with(chat_id="12345")

        service.process_update({"message": {"chat": {"id": 12345}, "text": "/power on 226"}})
        service.handle_power_on.assert_called_with("226", chat_id="12345")

        service.process_update({"message": {"chat": {"id": 12345}, "text": "/on all"}})
        service.handle_connect_all.assert_called_once_with(chat_id="12345")


if __name__ == "__main__":
    unittest.main()

