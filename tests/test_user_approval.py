import unittest
from unittest.mock import MagicMock
from automation.telegram_bot_service import TelegramBotService
from database.db import Database, Tenant, TelegramSubscriber
from config import Config
from telegram_notifier import DepositAlert

class TestUserApprovalWorkflow(unittest.TestCase):
    def setUp(self):
        self.mock_monitor = MagicMock()
        self.mock_config = Config(
            telegram_bot_token="test_token",
            telegram_chat_id="8466594075",
        )
        self.mock_monitor.config = self.mock_config
        self.mock_notifier = MagicMock()
        self.mock_monitor.notifier = self.mock_notifier

        self.mock_db = MagicMock(spec=Database)
        self.service = TelegramBotService(self.mock_monitor, config=self.mock_config, db=self.mock_db)

    def test_unregistered_user_start_triggers_admin_request(self):
        self.mock_db.get_subscriber_by_chat_id.return_value = None
        msg_update = {
            "message": {
                "chat": {"id": 999111222},
                "from": {"id": 999111222, "first_name": "New", "last_name": "Applicant", "username": "newapp"},
                "text": "/start",
            }
        }
        self.service.process_update(msg_update)
        self.mock_notifier.send_message.assert_any_call(
            unittest.mock.ANY,
            reply_markup={"remove_keyboard": True},
            chat_id="999111222",
        )
        self.mock_notifier.send_control_panel.assert_called_once()
        admin_call_kwargs = self.mock_notifier.send_control_panel.call_args[1]
        self.assertEqual(admin_call_kwargs["chat_id"], "8466594075")
        self.assertIn("New User Access Request", admin_call_kwargs["text"])
        kb = admin_call_kwargs["custom_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "action_approve_999111222")
        self.assertEqual(kb[0][1]["callback_data"], "action_decline_999111222")

    def test_admin_approves_user(self):
        self.mock_db.get_all_tenants.return_value = [Tenant(id="tenant-kamruzzaman", name="kamruzzaman", contact="")]
        cb_update = {
            "callback_query": {
                "id": "cb_appr_1",
                "from": {"id": 8466594075},
                "data": "action_approve_999111222",
            }
        }
        self.service.process_update(cb_update)
        self.mock_db.add_subscriber.assert_called_once_with(
            tenant_id="tenant-kamruzzaman",
            chat_id="999111222",
            username="",
            role="viewer_only",
            receive_alerts=True,
        )
        self.mock_notifier.answer_callback_query.assert_called_once_with("cb_appr_1", text="✅ User 999111222 approved!")

    def test_admin_declines_user(self):
        cb_update = {
            "callback_query": {
                "id": "cb_decl_1",
                "from": {"id": 8466594075},
                "data": "action_decline_999111222",
            }
        }
        self.service.process_update(cb_update)
        self.mock_notifier.answer_callback_query.assert_called_once_with("cb_decl_1", text="❌ User 999111222 declined.")

    def test_viewer_only_cannot_access_menu_or_commands(self):
        tenant = Tenant(id="tenant-kamruzzaman", name="kamruzzaman", contact="")
        sub = TelegramSubscriber(
            id="sub1", tenant_id="tenant-kamruzzaman", chat_id="8751675655",
            username="viewer", role="viewer_only", receive_alerts=True
        )
        self.mock_db.get_subscriber_by_chat_id.return_value = sub
        self.mock_db.get_tenant.return_value = tenant

        msg_update = {
            "message": {
                "chat": {"id": 8751675655},
                "from": {"id": 8751675655, "first_name": "Viewer"},
                "text": "/start",
            }
        }
        self.service.process_update(msg_update)
        self.mock_notifier.send_bottom_menu.assert_not_called()
        self.mock_notifier.send_control_panel.assert_not_called()

    def test_deposit_alert_only_has_menu_for_admin_not_for_viewers(self):
        from telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier("fake_token", chat_id="8466594075")
        notifier.session = MagicMock()
        alert = DepositAlert(sender="Transfer from Alice", amount="+$100.00", time_str="Just now", balance="$200.00")
        notifier.send_deposit_alert(alert, target_chat_ids=["8466594075", "8751675655"])
        calls = notifier.session.post.call_args_list
        admin_call = [c for c in calls if c[1]["json"]["chat_id"] == "8466594075"][0]
        viewer_call = [c for c in calls if c[1]["json"]["chat_id"] == "8751675655"][0]
        self.assertIsNotNone(admin_call[1]["json"].get("reply_markup"))
        self.assertIsNone(viewer_call[1]["json"].get("reply_markup"))

    def test_is_admin_and_get_all_admins(self):
        # Super admin
        self.assertTrue(self.service.is_admin("8466594075"))
        # Non-admin
        self.mock_db.get_subscriber_by_chat_id.return_value = None
        self.assertFalse(self.service.is_admin("111111111"))

        # Database admin
        sub_admin = TelegramSubscriber(
            id="s1", tenant_id="t1", chat_id="222222222", role="full_controller", is_active=True
        )
        self.mock_db.get_subscriber_by_chat_id.return_value = sub_admin
        self.assertTrue(self.service.is_admin("222222222"))

        # Database viewer
        sub_viewer = TelegramSubscriber(
            id="s2", tenant_id="t1", chat_id="333333333", role="viewer_only", is_active=True
        )
        self.mock_db.get_subscriber_by_chat_id.return_value = sub_viewer
        self.assertFalse(self.service.is_admin("333333333"))

        # get_all_admins should return both super admin and db admin
        self.mock_db.get_all_subscribers.return_value = [sub_admin, sub_viewer]
        admins = self.service.get_all_admins()
        self.assertIn("8466594075", admins)
        self.assertIn("222222222", admins)
        self.assertNotIn("333333333", admins)

    def test_promote_member_to_admin(self):
        self.mock_db.get_all_tenants.return_value = [Tenant(id="t1", name="Kamruzzaman", contact="")]
        self.mock_db.get_all_subscribers.return_value = []
        cb_update = {
            "callback_query": {
                "id": "cb_prom_1",
                "from": {"id": 8466594075},
                "data": "action_promote_8751675655",
            }
        }
        self.service.process_update(cb_update)
        self.mock_db.add_subscriber.assert_called_with(
            tenant_id="t1",
            chat_id="8751675655",
            role="full_controller",
            receive_alerts=True,
        )
        # Verify promoted user gets control panel and bottom menu
        self.mock_notifier.send_bottom_menu.assert_called_with(
            active_device_name=unittest.mock.ANY,
            chat_id="8751675655",
        )

    def test_demote_admin_to_member(self):
        self.mock_db.get_all_tenants.return_value = [Tenant(id="t1", name="Kamruzzaman", contact="")]
        self.mock_db.get_all_subscribers.return_value = []
        cb_update = {
            "callback_query": {
                "id": "cb_dem_1",
                "from": {"id": 8466594075},
                "data": "action_demote_222222222",
            }
        }
        self.service.process_update(cb_update)
        self.mock_db.add_subscriber.assert_called_with(
            tenant_id="t1",
            chat_id="222222222",
            role="viewer_only",
            receive_alerts=True,
        )
        # Demoted user must have their keyboard removed
        self.mock_notifier.send_message.assert_any_call(
            unittest.mock.ANY,
            reply_markup={"remove_keyboard": True},
            chat_id="222222222",
        )

    def test_super_admin_cannot_be_demoted_or_removed(self):
        # Setup another admin attempting to demote or remove super admin
        sub_admin = TelegramSubscriber(
            id="s1", tenant_id="t1", chat_id="222222222", role="full_controller", is_active=True
        )
        self.mock_db.get_subscriber_by_chat_id.return_value = sub_admin

        # Attempt to demote Super Admin
        self.service.handle_demote_user("8466594075", admin_chat_id="222222222")
        self.mock_db.add_subscriber.assert_not_called()
        self.mock_notifier.send_message.assert_called_with(
            "🛡 *Protected Account:*\nThe Primary Super Admin cannot be demoted.",
            chat_id="222222222",
        )

        # Attempt to remove Super Admin
        self.service.handle_remove_user("8466594075", admin_chat_id="222222222")
        self.mock_db.delete_subscriber_by_chat_id.assert_not_called()
        self.mock_notifier.send_message.assert_called_with(
            "🛡 *Protected Account:*\nThe Primary Super Admin cannot be removed.",
            chat_id="222222222",
        )

    def test_add_admin_and_add_member_commands(self):
        self.mock_db.get_all_tenants.return_value = [Tenant(id="t1", name="Kamruzzaman", contact="")]

        # 1. Add admin command
        msg_admin = {
            "message": {
                "chat": {"id": 8466594075},
                "from": {"id": 8466594075},
                "text": "/add_admin 555444333 Jack",
            }
        }
        self.service.process_update(msg_admin)
        self.mock_db.add_subscriber.assert_called_with(
            tenant_id="t1",
            chat_id="555444333",
            username="Jack",
            role="full_controller",
            receive_alerts=True,
        )

        # 2. Add member command
        msg_member = {
            "message": {
                "chat": {"id": 8466594075},
                "from": {"id": 8466594075},
                "text": "/add_member 666777888 Jill",
            }
        }
        self.service.process_update(msg_member)
        self.mock_db.add_subscriber.assert_called_with(
            tenant_id="t1",
            chat_id="666777888",
            username="Jill",
            role="viewer_only",
            receive_alerts=True,
        )
        # Member notification must have remove_keyboard=True and no menu
        self.mock_notifier.send_message.assert_any_call(
            unittest.mock.ANY,
            reply_markup={"remove_keyboard": True},
            chat_id="666777888",
        )

if __name__ == "__main__":
    unittest.main()
