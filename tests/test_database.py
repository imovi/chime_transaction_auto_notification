"""Unit tests for multi-tenant database, subscriptions, and RBAC."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from database.db import Database, Tenant, TelegramSubscriber, TenantDevice


class TestDatabaseLayer(unittest.TestCase):
    """Test suite for Database operations."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_saas.db")
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

    def test_create_tenant_and_expiry(self) -> None:
        tenant = self.db.create_tenant(
            name="Client A",
            contact="@client_a",
            duration_days=30,
            geelark_app_id="APP123",
            geelark_bearer_token="TOKEN123",
        )
        self.assertIsNotNone(tenant.id)
        self.assertEqual(tenant.name, "Client A")
        self.assertTrue(tenant.is_active)
        self.assertGreaterEqual(tenant.days_remaining, 29)

        # Extend subscription
        extended = self.db.extend_subscription(tenant.id, additional_days=15)
        self.assertIsNotNone(extended)
        self.assertGreaterEqual(extended.days_remaining, 44)

    def test_check_and_expire_tenants(self) -> None:
        # Create tenant with past expiry
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=2)
        tenant = self.db.create_tenant(
            name="Expired Client",
            contact="client@test.com",
            duration_days=1,
        )
        self.db.update_tenant(tenant.id, {"expires_at": past.isoformat()})

        expired = self.db.check_and_expire_tenants()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].id, tenant.id)

        refetched = self.db.get_tenant(tenant.id)
        self.assertEqual(refetched.subscription_status, "expired")
        self.assertFalse(refetched.is_active)

    def test_subscriber_rbac(self) -> None:
        tenant = self.db.create_tenant(name="Client B", contact="b@test.com")

        # Add Full Controller
        sub1 = self.db.add_subscriber(tenant.id, "111111", username="admin_b", role="full_controller")
        self.assertEqual(sub1.role, "full_controller")

        # Add Viewer Only
        sub2 = self.db.add_subscriber(tenant.id, "222222", username="viewer_b", role="viewer_only")
        self.assertEqual(sub2.role, "viewer_only")

        lookup1 = self.db.get_subscriber_by_chat_id("111111")
        self.assertIsNotNone(lookup1)
        self.assertEqual(lookup1.role, "full_controller")

        lookup2 = self.db.get_subscriber_by_chat_id("222222")
        self.assertIsNotNone(lookup2)
        self.assertEqual(lookup2.role, "viewer_only")

        # Update role
        updated = self.db.add_subscriber(tenant.id, "222222", role="full_controller")
        self.assertEqual(updated.role, "full_controller")

    def test_device_management(self) -> None:
        tenant = self.db.create_tenant(name="Client C", contact="c@test.com")

        dev = self.db.upsert_device(tenant.id, "p100", "1", "Katie", pin="1122", enabled=True)
        self.assertEqual(dev.name, "Katie")
        self.assertEqual(dev.pin, "1122")
        self.assertTrue(dev.enabled)

        # Update PIN
        updated_dev = self.db.set_device_pin(tenant.id, "1", "9988")
        self.assertIsNotNone(updated_dev)
        self.assertEqual(updated_dev.pin, "9988")

        # Toggle enabled
        toggled = self.db.toggle_device_enabled(tenant.id, "1")
        self.assertFalse(toggled.enabled)

    def test_transaction_deduplication(self) -> None:
        tenant = self.db.create_tenant(name="Client D", contact="d@test.com")

        recorded1 = self.db.check_and_record_transaction(
            tenant.id, "p100", "hash123", "Alice", "+$50.00", "12:00 PM", "Lunch", "$150.00"
        )
        self.assertTrue(recorded1)

        # Second attempt should return False
        recorded2 = self.db.check_and_record_transaction(
            tenant.id, "p100", "hash123", "Alice", "+$50.00", "12:00 PM", "Lunch", "$150.00"
        )
        self.assertFalse(recorded2)


if __name__ == "__main__":
    unittest.main()
