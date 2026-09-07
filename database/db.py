"""Database layer for Multi-Tenant SaaS platform using SQLite.

Manages tenants, subscriptions, role-based Telegram subscribers, and cloud devices.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "chime_saas.db",
)


@dataclass
class Tenant:
    """Represents a subscriber / client organization."""

    id: str
    name: str
    contact: str
    geelark_app_id: Optional[str] = None
    geelark_api_key: Optional[str] = None
    geelark_bearer_token: Optional[str] = None
    geelark_auth_mode: str = "token"
    subscription_status: str = "active"  # 'active', 'expired', 'suspended', 'trial'
    starts_at: str = ""
    expires_at: str = ""
    max_devices: int = 20
    poll_interval_seconds: int = 30
    notes: Optional[str] = None
    created_at: str = ""

    @property
    def is_active(self) -> bool:
        if self.subscription_status != "active":
            return False
        if not self.expires_at:
            return True
        try:
            exp = datetime.datetime.fromisoformat(self.expires_at)
            now = datetime.datetime.now(datetime.timezone.utc)
            return now < exp.replace(tzinfo=datetime.timezone.utc if exp.tzinfo is None else exp.tzinfo)
        except Exception:
            return True

    @property
    def days_remaining(self) -> int:
        if not self.expires_at:
            return 999
        try:
            exp = datetime.datetime.fromisoformat(self.expires_at)
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = exp.replace(tzinfo=datetime.timezone.utc if exp.tzinfo is None else exp.tzinfo) - now
            return max(0, delta.days)
        except Exception:
            return 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_active"] = self.is_active
        d["days_remaining"] = self.days_remaining
        return d


@dataclass
class TelegramSubscriber:
    """Represents an authorized Telegram Chat ID under a tenant with RBAC permissions."""

    id: str
    tenant_id: str
    chat_id: str
    username: Optional[str] = None
    role: str = "full_controller"  # 'full_controller' or 'viewer_only'
    receive_alerts: bool = True
    is_active: bool = True
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TenantDevice:
    """Represents a cloud phone monitored for a tenant."""

    id: str
    tenant_id: str
    device_id: str
    serial: str
    name: str
    pin: Optional[str] = None
    enabled: bool = True
    latest_balance: Optional[str] = None
    last_synced_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Database:
    """Thread-safe SQLite database manager for Multi-Tenant SaaS."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        """Create schema tables and indexes if not exists."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Tenants
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                contact TEXT NOT NULL,
                geelark_app_id TEXT,
                geelark_api_key TEXT,
                geelark_bearer_token TEXT,
                geelark_auth_mode TEXT DEFAULT 'token',
                subscription_status TEXT DEFAULT 'active',
                starts_at TEXT,
                expires_at TEXT,
                max_devices INTEGER DEFAULT 20,
                poll_interval_seconds INTEGER DEFAULT 30,
                notes TEXT,
                created_at TEXT
            )
            """)

            # 2. Telegram Subscribers & Role-Based Access Control (RBAC)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_subscribers (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                username TEXT,
                role TEXT DEFAULT 'full_controller',
                receive_alerts INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_chat_id ON telegram_subscribers(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_tenant_id ON telegram_subscribers(tenant_id)")

            # 3. Tenant Devices
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tenant_devices (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                serial TEXT NOT NULL,
                name TEXT NOT NULL,
                pin TEXT,
                enabled INTEGER DEFAULT 1,
                latest_balance TEXT,
                last_synced_at REAL DEFAULT 0.0,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                UNIQUE(tenant_id, device_id)
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_devices_tenant_id ON tenant_devices(tenant_id)")

            # 4. Seen Transactions
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tenant_transactions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                sender TEXT NOT NULL,
                amount TEXT NOT NULL,
                time_str TEXT,
                note TEXT,
                balance_after TEXT,
                seen_at REAL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                UNIQUE(tenant_id, tx_hash)
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_tenant_hash ON tenant_transactions(tenant_id, tx_hash)")

            conn.commit()
            logger.info("Initialized Multi-Tenant SQLite Database at %s", self.db_path)

    # -------------------------------------------------------------------------
    # Tenant Operations
    # -------------------------------------------------------------------------

    def create_tenant(
        self,
        name: str,
        contact: str,
        geelark_app_id: Optional[str] = None,
        geelark_api_key: Optional[str] = None,
        geelark_bearer_token: Optional[str] = None,
        geelark_auth_mode: str = "token",
        duration_days: int = 30,
        max_devices: int = 20,
        notes: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Tenant:
        """Create a new subscriber tenant with defined duration in days."""
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = now + datetime.timedelta(days=duration_days)
        t_id = tenant_id or str(uuid.uuid4())

        tenant = Tenant(
            id=t_id,
            name=name.strip(),
            contact=contact.strip(),
            geelark_app_id=geelark_app_id.strip() if geelark_app_id else None,
            geelark_api_key=geelark_api_key.strip() if geelark_api_key else None,
            geelark_bearer_token=geelark_bearer_token.strip() if geelark_bearer_token else None,
            geelark_auth_mode=geelark_auth_mode.lower(),
            subscription_status="active",
            starts_at=now.isoformat(),
            expires_at=expires.isoformat(),
            max_devices=max_devices,
            notes=notes,
            created_at=now.isoformat(),
        )

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO tenants (
                id, name, contact, geelark_app_id, geelark_api_key, geelark_bearer_token,
                geelark_auth_mode, subscription_status, starts_at, expires_at,
                max_devices, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tenant.id, tenant.name, tenant.contact, tenant.geelark_app_id,
                tenant.geelark_api_key, tenant.geelark_bearer_token, tenant.geelark_auth_mode,
                tenant.subscription_status, tenant.starts_at, tenant.expires_at,
                tenant.max_devices, tenant.notes, tenant.created_at
            ))
            conn.commit()

        logger.info("Created tenant '%s' (ID: %s) expiring on %s (%d days)", name, t_id, tenant.expires_at, duration_days)
        return tenant

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        """Fetch tenant by ID."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
            row = cursor.fetchone()
            if row:
                return Tenant(**dict(row))
        return None

    def get_all_tenants(self, active_only: bool = False) -> List[Tenant]:
        """List all tenants, optionally filtered by active status."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM tenants ORDER BY created_at DESC"
            cursor.execute(query)
            rows = cursor.fetchall()
            tenants = [Tenant(**dict(r)) for r in rows]
            if active_only:
                return [t for t in tenants if t.is_active]
            return tenants

    def update_tenant(self, tenant_id: str, updates: Dict[str, Any]) -> Optional[Tenant]:
        """Update tenant attributes."""
        allowed_fields = {
            "name", "contact", "geelark_app_id", "geelark_api_key",
            "geelark_bearer_token", "geelark_auth_mode", "subscription_status",
            "expires_at", "max_devices", "poll_interval_seconds", "notes"
        }
        filtered = {k: v for k, v in updates.items() if k in allowed_fields}
        if not filtered:
            return self.get_tenant(tenant_id)

        set_clause = ", ".join([f"{k} = ?" for k in filtered.keys()])
        values = list(filtered.values()) + [tenant_id]

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE tenants SET {set_clause} WHERE id = ?", values)
            conn.commit()

        return self.get_tenant(tenant_id)

    def extend_subscription(self, tenant_id: str, additional_days: int) -> Optional[Tenant]:
        """Extend tenant subscription by N days from current expiry or now."""
        tenant = self.get_tenant(tenant_id)
        if not tenant:
            return None

        now = datetime.datetime.now(datetime.timezone.utc)
        current_expiry = now
        if tenant.expires_at:
            try:
                exp = datetime.datetime.fromisoformat(tenant.expires_at)
                exp_utc = exp.replace(tzinfo=datetime.timezone.utc if exp.tzinfo is None else exp.tzinfo)
                if exp_utc > now:
                    current_expiry = exp_utc
            except Exception:
                current_expiry = now

        new_expiry = current_expiry + datetime.timedelta(days=additional_days)
        return self.update_tenant(tenant_id, {
            "expires_at": new_expiry.isoformat(),
            "subscription_status": "active",
        })

    def get_primary_tenant_id(self) -> str:
        """Fetch primary active tenant ID with safe fallback."""
        tenants = self.get_all_tenants()
        return tenants[0].id if tenants else "tenant-kamruzzaman"

    def delete_tenant(self, tenant_id: str) -> bool:
        """Remove tenant and cascade all related data."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
            cursor.execute("DELETE FROM telegram_subscribers WHERE tenant_id = ?", (tenant_id,))
            cursor.execute("DELETE FROM tenant_devices WHERE tenant_id = ?", (tenant_id,))
            cursor.execute("DELETE FROM tenant_transactions WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cursor.rowcount > 0

    def check_and_expire_tenants(self) -> List[Tenant]:
        """Check all tenants and mark expired if expires_at has passed. Return newly expired."""
        now = datetime.datetime.now(datetime.timezone.utc)
        all_tenants = self.get_all_tenants()
        expired_list: List[Tenant] = []

        for t in all_tenants:
            if t.subscription_status == "active" and t.expires_at:
                try:
                    exp = datetime.datetime.fromisoformat(t.expires_at)
                    exp_utc = exp.replace(tzinfo=datetime.timezone.utc if exp.tzinfo is None else exp.tzinfo)
                    if now >= exp_utc:
                        self.update_tenant(t.id, {"subscription_status": "expired"})
                        t.subscription_status = "expired"
                        expired_list.append(t)
                        logger.warning("Tenant '%s' (%s) subscription expired on %s", t.name, t.id, t.expires_at)
                except Exception as e:
                    logger.error("Error evaluating expiry for %s: %s", t.id, e)

        return expired_list

    # -------------------------------------------------------------------------
    # Telegram Subscribers & Role-Based Access Control (RBAC)
    # -------------------------------------------------------------------------

    def add_subscriber(
        self,
        tenant_id: str,
        chat_id: str,
        username: Optional[str] = None,
        role: str = "full_controller",
        receive_alerts: bool = True,
    ) -> TelegramSubscriber:
        """Register or update a Telegram Chat ID for a tenant with specified role."""
        sub_id = str(uuid.uuid4())
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chat_id_clean = str(chat_id).strip()

        # Check existing
        existing = self.get_subscriber_by_chat_id(chat_id_clean, tenant_id=tenant_id)
        if existing:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                UPDATE telegram_subscribers
                SET role = ?, receive_alerts = ?, username = ?, is_active = 1
                WHERE id = ?
                """, (role, 1 if receive_alerts else 0, username, existing.id))
                conn.commit()
            existing.role = role
            existing.receive_alerts = receive_alerts
            existing.username = username
            return existing

        sub = TelegramSubscriber(
            id=sub_id,
            tenant_id=tenant_id,
            chat_id=chat_id_clean,
            username=username,
            role=role,
            receive_alerts=receive_alerts,
            is_active=True,
            created_at=created_at,
        )

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO telegram_subscribers (
                id, tenant_id, chat_id, username, role, receive_alerts, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sub.id, sub.tenant_id, sub.chat_id, sub.username,
                sub.role, 1 if sub.receive_alerts else 0, 1 if sub.is_active else 0, sub.created_at
            ))
            conn.commit()

        logger.info("Added subscriber Chat ID %s to tenant %s (Role: %s)", chat_id_clean, tenant_id, role)
        return sub

    def get_subscriber_by_chat_id(self, chat_id: str, tenant_id: Optional[str] = None) -> Optional[TelegramSubscriber]:
        """Lookup subscriber by Chat ID."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            if tenant_id:
                cursor.execute("SELECT * FROM telegram_subscribers WHERE chat_id = ? AND tenant_id = ?", (str(chat_id).strip(), tenant_id))
            else:
                cursor.execute("SELECT * FROM telegram_subscribers WHERE chat_id = ?", (str(chat_id).strip(),))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                d["receive_alerts"] = bool(d["receive_alerts"])
                d["is_active"] = bool(d["is_active"])
                return TelegramSubscriber(**d)
        return None

    def get_tenant_subscribers(self, tenant_id: str, active_only: bool = True) -> List[TelegramSubscriber]:
        """Fetch all subscribers mapped to a tenant."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM telegram_subscribers WHERE tenant_id = ?", (tenant_id,))
            rows = cursor.fetchall()
            subs = []
            for r in rows:
                d = dict(r)
                d["receive_alerts"] = bool(d["receive_alerts"])
                d["is_active"] = bool(d["is_active"])
                sub = TelegramSubscriber(**d)
                if not active_only or sub.is_active:
                    subs.append(sub)
            return subs

    def get_all_subscribers(self, active_only: bool = True) -> List[TelegramSubscriber]:
        """Fetch all subscribers across all organizations."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            if active_only:
                cursor.execute("SELECT * FROM telegram_subscribers WHERE is_active = 1")
            else:
                cursor.execute("SELECT * FROM telegram_subscribers")
            rows = cursor.fetchall()
            subs = []
            for r in rows:
                d = dict(r)
                d["receive_alerts"] = bool(d["receive_alerts"])
                d["is_active"] = bool(d["is_active"])
                subs.append(TelegramSubscriber(**d))
            return subs

    def get_subscriber(self, subscriber_id: str) -> Optional[TelegramSubscriber]:
        """Fetch subscriber by primary id."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM telegram_subscribers WHERE id = ?", (subscriber_id,))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                d["receive_alerts"] = bool(d["receive_alerts"])
                d["is_active"] = bool(d["is_active"])
                return TelegramSubscriber(**d)
        return None

    def update_subscriber(self, subscriber_id: str, updates: Dict[str, Any]) -> Optional[TelegramSubscriber]:
        """Update subscriber role or alert settings."""
        allowed_fields = {"role", "receive_alerts", "username", "is_active"}
        filtered = {}
        for k, v in updates.items():
            if k in allowed_fields:
                if k in ("receive_alerts", "is_active"):
                    filtered[k] = 1 if v else 0
                else:
                    filtered[k] = v
        if not filtered:
            return self.get_subscriber(subscriber_id)

        set_clause = ", ".join([f"{k} = ?" for k in filtered.keys()])
        values = list(filtered.values()) + [subscriber_id]

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE telegram_subscribers SET {set_clause} WHERE id = ?", values)
            conn.commit()

        return self.get_subscriber(subscriber_id)

    def delete_subscriber(self, subscriber_id: str) -> bool:
        """Remove a subscriber from tenant."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM telegram_subscribers WHERE id = ?", (subscriber_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_subscriber_by_chat_id(self, chat_id: str) -> bool:
        """Remove a subscriber by Telegram chat ID."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM telegram_subscribers WHERE chat_id = ?", (str(chat_id).strip(),))
            conn.commit()
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # Tenant Cloud Devices
    # -------------------------------------------------------------------------

    def upsert_device(
        self,
        tenant_id: str,
        device_id: str,
        serial: str,
        name: str,
        pin: Optional[str] = None,
        enabled: bool = True,
        latest_balance: Optional[str] = None,
    ) -> TenantDevice:
        """Add or update cloud phone under a tenant."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tenant_devices WHERE tenant_id = ? AND device_id = ?", (tenant_id, device_id))
            existing = cursor.fetchone()

            if existing:
                dev_id_pk = existing["id"]
                cursor.execute("""
                UPDATE tenant_devices
                SET serial = ?, name = ?, pin = COALESCE(?, pin), enabled = ?,
                    latest_balance = COALESCE(?, latest_balance), last_synced_at = ?
                WHERE id = ?
                """, (serial, name, pin, 1 if enabled else 0, latest_balance, datetime.datetime.now().timestamp(), dev_id_pk))
                conn.commit()
                return TenantDevice(
                    id=dev_id_pk,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    serial=serial,
                    name=name,
                    pin=pin or existing["pin"],
                    enabled=enabled,
                    latest_balance=latest_balance or existing["latest_balance"],
                    last_synced_at=datetime.datetime.now().timestamp(),
                )

            dev_pk = str(uuid.uuid4())
            cursor.execute("""
            INSERT INTO tenant_devices (
                id, tenant_id, device_id, serial, name, pin, enabled, latest_balance, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                dev_pk, tenant_id, device_id, serial, name, pin,
                1 if enabled else 0, latest_balance, datetime.datetime.now().timestamp()
            ))
            conn.commit()
            return TenantDevice(
                id=dev_pk,
                tenant_id=tenant_id,
                device_id=device_id,
                serial=serial,
                name=name,
                pin=pin,
                enabled=enabled,
                latest_balance=latest_balance,
                last_synced_at=datetime.datetime.now().timestamp(),
            )

    def get_tenant_devices(self, tenant_id: str, enabled_only: bool = False) -> List[TenantDevice]:
        """Fetch all devices registered to a tenant."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tenant_devices WHERE tenant_id = ? ORDER BY CAST(serial AS INTEGER) ASC, serial ASC", (tenant_id,))
            rows = cursor.fetchall()
            devices = []
            for r in rows:
                d = dict(r)
                d["enabled"] = bool(d["enabled"])
                dev = TenantDevice(**d)
                if not enabled_only or dev.enabled:
                    devices.append(dev)
            return devices

    def get_all_tenant_devices(self, enabled_only: bool = False) -> List[TenantDevice]:
        """Fetch all devices across all tenants."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            if enabled_only:
                cursor.execute("SELECT * FROM tenant_devices WHERE enabled = 1 ORDER BY CAST(serial AS INTEGER) ASC, serial ASC")
            else:
                cursor.execute("SELECT * FROM tenant_devices ORDER BY CAST(serial AS INTEGER) ASC, serial ASC")
            rows = cursor.fetchall()
            devices = []
            for r in rows:
                d = dict(r)
                d["enabled"] = bool(d["enabled"])
                devices.append(TenantDevice(**d))
            return devices

    def update_device_balance(self, tenant_id: Optional[str], device_id: str, balance: str) -> bool:
        """Update latest balance for a device in SQLite database."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tenant_devices
            SET latest_balance = ?, last_synced_at = ?
            WHERE device_id = ? OR serial = ?
            """, (balance, datetime.datetime.now().timestamp(), device_id, device_id))
            conn.commit()
            return cursor.rowcount > 0

    def set_device_pin(self, tenant_id: str, device_serial_or_id: str, pin: str) -> Optional[TenantDevice]:
        """Update device PIN for a specific device under tenant."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tenant_devices
            SET pin = ?
            WHERE tenant_id = ? AND (device_id = ? OR serial = ?)
            """, (pin.strip(), tenant_id, device_serial_or_id, device_serial_or_id))
            conn.commit()

            cursor.execute("""
            SELECT * FROM tenant_devices
            WHERE tenant_id = ? AND (device_id = ? OR serial = ?)
            """, (tenant_id, device_serial_or_id, device_serial_or_id))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                d["enabled"] = bool(d["enabled"])
                return TenantDevice(**d)
        return None

    def toggle_device_enabled(self, tenant_id: str, device_serial_or_id: str, state: Optional[bool] = None) -> Optional[TenantDevice]:
        """Toggle monitoring enabled status for device."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM tenant_devices
            WHERE tenant_id = ? AND (device_id = ? OR serial = ?)
            """, (tenant_id, device_serial_or_id, device_serial_or_id))
            row = cursor.fetchone()
            if not row:
                return None

            new_state = (not bool(row["enabled"])) if state is None else bool(state)
            cursor.execute("""
            UPDATE tenant_devices
            SET enabled = ?
            WHERE id = ?
            """, (1 if new_state else 0, row["id"]))
            conn.commit()

            d = dict(row)
            d["enabled"] = new_state
            return TenantDevice(**d)

    def set_all_tenant_devices_enabled(self, tenant_id: str, enabled: bool) -> List[TenantDevice]:
        """Enable or disable all devices under tenant."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tenant_devices
            SET enabled = ?
            WHERE tenant_id = ?
            """, (1 if enabled else 0, tenant_id))
            conn.commit()
        return self.get_tenant_devices(tenant_id)

    def get_device_tenant(self, device_id_or_serial: str) -> Optional[Tenant]:
        """Lookup the owning tenant of any device by device_id or serial."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT t.* FROM tenants t
            JOIN tenant_devices td ON t.id = td.tenant_id
            WHERE td.device_id = ? OR td.serial = ?
            LIMIT 1
            """, (device_id_or_serial, device_id_or_serial))
            row = cursor.fetchone()
            if row:
                return Tenant(**dict(row))
        return None

    def get_device_by_id_or_serial(self, device_id_or_serial: str) -> Optional[TenantDevice]:
        """Fetch a device record across any tenant by its device_id or serial."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM tenant_devices
            WHERE device_id = ? OR serial = ?
            LIMIT 1
            """, (device_id_or_serial, device_id_or_serial))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                d["enabled"] = bool(d["enabled"])
                return TenantDevice(**d)
        return None

    def get_all_tenant_devices(self, enabled_only: bool = False) -> List[TenantDevice]:
        """Fetch all devices registered across all tenants in the platform."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM tenant_devices
            ORDER BY CAST(serial AS INTEGER) ASC, serial ASC
            """)
            rows = cursor.fetchall()
            devices = []
            for r in rows:
                d = dict(r)
                d["enabled"] = bool(d["enabled"])
                dev = TenantDevice(**d)
                if not enabled_only or dev.enabled:
                    devices.append(dev)
            return devices


    # -------------------------------------------------------------------------
    # Tenant Transactions (Deduplication)
    # -------------------------------------------------------------------------

    def check_and_record_transaction(
        self,
        tenant_id: str,
        device_id: str,
        tx_hash: str,
        sender: str,
        amount: str,
        time_str: str = "",
        note: str = "",
        balance_after: str = "",
    ) -> bool:
        """Atomically check if transaction was seen for tenant. If not, record and return True."""
        tx_id = str(uuid.uuid4())
        now = datetime.datetime.now().timestamp()

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tenant_transactions WHERE tenant_id = ? AND tx_hash = ?", (tenant_id, tx_hash))
            if cursor.fetchone():
                return False

            try:
                cursor.execute("""
                INSERT INTO tenant_transactions (
                    id, tenant_id, device_id, tx_hash, sender, amount, time_str, note, balance_after, seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (tx_id, tenant_id, device_id, tx_hash, sender, amount, time_str, note, balance_after, now))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_recent_transactions(self, tenant_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch latest inbound deposits across tenant(s)."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            if tenant_id:
                cursor.execute("""
                SELECT t.*, d.serial as device_serial, d.name as device_name
                FROM tenant_transactions t
                LEFT JOIN tenant_devices d ON t.tenant_id = d.tenant_id AND t.device_id = d.device_id
                WHERE t.tenant_id = ?
                ORDER BY t.seen_at DESC LIMIT ?
                """, (tenant_id, limit))
            else:
                cursor.execute("""
                SELECT t.*, ten.name as tenant_name, d.serial as device_serial, d.name as device_name
                FROM tenant_transactions t
                JOIN tenants ten ON t.tenant_id = ten.id
                LEFT JOIN tenant_devices d ON t.tenant_id = d.tenant_id AND t.device_id = d.device_id
                ORDER BY t.seen_at DESC LIMIT ?
                """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def export_transactions_csv(self, tenant_id: Optional[str] = None, limit: int = 2000) -> str:
        """Export transaction records to RFC 4180 compliant CSV format."""
        txs = self.get_recent_transactions(tenant_id=tenant_id, limit=limit)
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

        # Header row
        writer.writerow([
            "Transaction ID",
            "Date & Time",
            "Subscriber Organization",
            "Device Serial",
            "Device Name",
            "Sender / Description",
            "Amount",
            "Note",
            "Available Balance After",
            "System Recorded At",
        ])

        for tx in txs:
            recorded_at = ""
            if tx.get("seen_at"):
                try:
                    dt = datetime.datetime.fromtimestamp(float(tx["seen_at"]), tz=datetime.timezone.utc)
                    recorded_at = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    recorded_at = str(tx.get("seen_at", ""))

            writer.writerow([
                tx.get("id", ""),
                tx.get("time_str", ""),
                tx.get("tenant_name", ""),
                f"#{tx.get('device_serial', '')}" if tx.get("device_serial") else "",
                tx.get("device_name", ""),
                tx.get("sender", ""),
                tx.get("amount", ""),
                tx.get("note", ""),
                tx.get("balance_after", ""),
                recorded_at,
            ])

        return output.getvalue()


    # -------------------------------------------------------------------------
    # Auto-Migration from existing devices.json & config.py
    # -------------------------------------------------------------------------

    def clean_and_setup_kamruzzaman(self, config: object, devices_json_path: str) -> Tenant:
        """Remove all demo/test subscribers and setup clean 'kamruzzaman' primary subscriber."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tenant_transactions")
            cursor.execute("DELETE FROM tenant_devices")
            cursor.execute("DELETE FROM telegram_subscribers")
            cursor.execute("DELETE FROM tenants")
            conn.commit()
            logger.info("Cleaned up all previous demo subscribers and transactions")

        tenant = self.create_tenant(
            name="kamruzzaman",
            contact="@kamruzzaman",
            geelark_app_id=getattr(config, "app_id", None) or "5ZPEQNCSG313NX2NM6RIUE18SG",
            geelark_api_key=getattr(config, "api_key", None),
            geelark_bearer_token=getattr(config, "bearer_token", None),
            geelark_auth_mode=getattr(config, "auth_mode", "token"),
            duration_days=365,
            notes="Primary active subscriber: kamruzzaman",
            tenant_id="tenant-kamruzzaman",
        )

        chat_id = getattr(config, "telegram_chat_id", None) or "8466594075"
        self.add_subscriber(
            tenant_id=tenant.id,
            chat_id=str(chat_id),
            username="@kamruzzaman",
            role="full_controller",
            receive_alerts=True,
        )

        if os.path.exists(devices_json_path):
            try:
                with open(devices_json_path, "r", encoding="utf-8") as f:
                    dev_list = json.load(f)
                    for item in dev_list:
                        self.upsert_device(
                            tenant_id=tenant.id,
                            device_id=str(item.get("device_id", "")),
                            serial=str(item.get("serial", "")),
                            name=str(item.get("name", "")),
                            pin=item.get("pin"),
                            enabled=bool(item.get("enabled", False)),
                            latest_balance=item.get("latest_balance"),
                        )
                logger.info("Migrated %d devices into 'kamruzzaman' subscriber", len(dev_list))
            except Exception as e:
                logger.error("Error migrating devices from json: %s", e)

        return tenant

    def auto_migrate_existing(self, config: object, devices_json_path: str) -> Tenant:
        """Seed the default primary tenant from existing config and devices.json so live setup continues uninterrupted."""
        tenants = self.get_all_tenants()
        if tenants:
            return tenants[0]

        return self.clean_and_setup_kamruzzaman(config, devices_json_path)

