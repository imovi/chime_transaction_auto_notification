"""Unit tests for connection error detection, auto-restart from recents, and admin alerts."""

import unittest
from unittest.mock import MagicMock, patch

from automation.chime_flow import ChimeAutomationFlow
from automation.chime_monitor import ChimeMonitor
from automation.device_manager import ChimeDevice


class TestConnectionRecovery(unittest.TestCase):
    """Test suite verifying auto-recovery when Chime shows 'Please check your connection'."""

    def setUp(self) -> None:
        self.mock_client = MagicMock()
        self.flow = ChimeAutomationFlow("phone-123", self.mock_client)
        self.flow.shell = MagicMock()

    def test_is_connection_error_detection(self) -> None:
        """Verify various network/connection error prompts are correctly identified."""
        # 1. Exact match from user screenshot
        elements = [
            "android.widget.ImageView",
            "Please check your connection.",
            "Navigate up",
        ]
        self.assertTrue(self.flow.is_connection_error(elements))

        # 2. Case-insensitive and partial variants
        self.assertTrue(self.flow.is_connection_error(["check your connection"]))
        self.assertTrue(self.flow.is_connection_error(["No internet connection"]))
        self.assertTrue(self.flow.is_connection_error(["Network error occurred"]))
        self.assertTrue(self.flow.is_connection_error(["Something went wrong"]))

        # 3. Normal checking screen (no error)
        normal_elements = ["Checking", "$140.00", "Transactions", "Today"]
        self.assertFalse(self.flow.is_connection_error(normal_elements))

    @patch("time.sleep")
    def test_restart_app_cleanly_executes_home_stop_and_launch(self, _mock_sleep) -> None:
        """Verify restart_app_cleanly exits to home, force-stops app from recents, and relaunches."""
        self.flow.shell.get_screen_elements.return_value = ["Checking", "Transactions", "$150.00"]

        res = self.flow.restart_app_cleanly(pin="1122")

        # 1. Check Home key pressed
        self.flow.shell.home.assert_called_once_with("phone-123")
        # 2. Check force-stop executed to clear recents
        self.flow.shell.stop_app.assert_called_once_with("phone-123", "com.onedebit.chime")
        # 3. Check relaunch app called
        self.flow.shell.launch_app.assert_called_once_with("phone-123", "com.onedebit.chime")
        # 4. Result asserts
        self.assertTrue(res.get("restarted"))
        self.assertFalse(res.get("connection_error"))

    @patch("time.sleep")
    def test_ensure_open_and_unlocked_auto_recovers_on_connection_error(self, _mock_sleep) -> None:
        """Verify ensure_open_and_unlocked detects connection error and triggers clean restart."""
        self.flow.is_in_foreground = MagicMock(return_value=True)

        # First call shows connection error, restart resolves it to normal Checking screen
        self.flow.shell.get_screen_elements.side_effect = [
            ["Please check your connection."],           # initial screen
            ["Checking", "Transactions", "$120.00"],     # screen after clean restart
            ["Checking", "Transactions", "$120.00"],     # subsequent check
        ]

        res = self.flow.ensure_open_and_unlocked(pin="1122")

        self.flow.shell.stop_app.assert_called_once_with("phone-123", "com.onedebit.chime")
        self.assertTrue(res.get("restarted"))
        self.assertFalse(res.get("connection_error"))

    def test_chime_monitor_sends_admin_alert_on_persistent_connection_error(self) -> None:
        """Verify ChimeMonitor dispatches admin Telegram notification if connection error persists."""
        mock_notifier = MagicMock()
        mock_db = MagicMock()
        mock_db.get_all_subscribers.return_value = []

        monitor = ChimeMonitor(
            client=self.mock_client,
            notifier=mock_notifier,
            db=mock_db,
        )
        monitor.phone_mgr.query_status = MagicMock(return_value=[{"status": 0}])

        mock_flow = MagicMock()
        mock_flow.ensure_open_and_unlocked.return_value = {
            "unlocked": True,
            "needs_pin": False,
            "connection_error": True,
            "elements": ["Please check your connection."],
        }
        monitor.get_flow = MagicMock(return_value=mock_flow)

        dev = ChimeDevice(device_id="p-1", serial="226", name="Phone-226", enabled=True)
        txs = monitor.poll_single_device(dev)

        # No transactions extracted
        self.assertEqual(txs, [])
        # Admin alert sent
        mock_notifier.send_message.assert_called()
        alert_text = mock_notifier.send_message.call_args[0][0]
        self.assertIn("No Internet / Connection Error", alert_text)
        self.assertIn("#226 Phone-226", alert_text)


if __name__ == "__main__":
    unittest.main()
