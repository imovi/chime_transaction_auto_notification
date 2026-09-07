"""Telegram Notifier module for dispatching instant deposit alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DepositAlert:
    """Represents a structured deposit event."""

    sender: str
    amount: str
    time_str: str
    note: Optional[str] = None
    balance: Optional[str] = None
    device_name: Optional[str] = None
    screenshot_url: Optional[str] = None


class TelegramNotifier:
    """Sends formatted alerts and screenshots to Telegram via Bot API."""

    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=15, pool_maxsize=15, max_retries=3)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @property
    def is_configured(self) -> bool:
        """Check whether bot token and chat ID are provided."""
        return bool(self.bot_token and self.chat_id)

    def format_message(self, alert: DepositAlert) -> str:
        """Format deposit details into clean Telegram message."""
        lines = [
            "💰 *New Chime Deposit Received!*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"👤 *From:* {alert.sender}",
            f"💵 *Amount:* `{alert.amount}`",
        ]
        if alert.note:
            lines.append(f"📝 *Note:* {alert.note}")
        if alert.time_str:
            lines.append(f"🕒 *Time:* {alert.time_str}")
        if alert.balance:
            lines.append(f"💳 *Available Balance:* `{alert.balance}`")
        if alert.device_name:
            lines.append(f"📱 *Device:* `{alert.device_name}`")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    @staticmethod
    def get_persistent_reply_keyboard() -> dict:
        """Return resizable and minimizable bottom ReplyKeyboardMarkup."""
        return {
            "keyboard": [
                [{"text": "🔄 Refresh & Check Now"}],
                [{"text": "💳 Check Balance"}, {"text": "📜 Recent History"}],
                [{"text": "📸 Screen Capture"}, {"text": "📱 Device Status"}],
                [{"text": "📱 Switch Device"}, {"text": "📋 All Devices"}],
                [{"text": "👥 Manage Users"}, {"text": "🔐 Set Device PIN"}],
                [{"text": "❌ Hide Menu"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def send_message(
        self,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup: Optional[dict] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Send text message to configured Telegram chat with auto-retry."""
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            logger.warning("Telegram is not configured. Skipping alert dispatch.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        for attempt in range(1, 4):
            try:
                res = self.session.post(url, json=payload, timeout=15)
                res_data = res.json()
                if not res_data.get("ok"):
                    desc = res_data.get("description", "")
                    logger.error("Telegram API error: %s", desc)
                    if "parse" in desc.lower() and "parse_mode" in payload:
                        # Fall back to plain text delivery
                        payload.pop("parse_mode", None)
                        fallback_res = self.session.post(url, json=payload, timeout=15)
                        if fallback_res.json().get("ok"):
                            logger.info("Telegram message delivered via plain text fallback.")
                            return True
                    return False
                logger.info("Telegram message sent successfully.")
                return True
            except requests.RequestException as e:
                logger.warning("Telegram send attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    import time
                    time.sleep(1.5)
        return False

    def send_bottom_menu(
        self,
        text: Optional[str] = None,
        active_device_name: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Send message that anchors the persistent bottom Reply Keyboard."""
        header = f"📱 *Active Device:* `{active_device_name}`\n" if active_device_name else ""
        msg = text or (
            f"🤖 *Chime Cloud Phone Controller Ready!*\n"
            f"{header}"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Use the menu below for 1-tap commands.\n"
            "_(You can minimize it anytime using the keyboard icon or tap '❌ Hide Menu')_"
        )
        return self.send_message(msg, reply_markup=self.get_persistent_reply_keyboard(), chat_id=chat_id)

    def hide_bottom_menu(self, text: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
        """Hide/collapse the bottom menu and provide a button to reopen it."""
        msg = text or (
            "🔽 *Bottom Menu Minimized!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "The bottom keyboard has been hidden.\n"
            "Tap below or type `/menu` to open it again anytime."
        )
        # 1. Dismiss bottom keyboard with remove_keyboard
        self.send_message(msg, reply_markup={"remove_keyboard": True}, chat_id=chat_id)
        # 2. Provide quick inline button to restore it anytime
        reopen_keyboard = [
            [{"text": "🎛 Open Bottom Menu", "callback_data": "action_open_bottom_menu"}],
            [{"text": "💳 Check Balance", "callback_data": "action_balance"}, {"text": "🔄 Refresh", "callback_data": "action_refresh"}],
        ]
        return self.send_control_panel(
            text="Tap below to reopen the bottom menu:",
            custom_keyboard=reopen_keyboard,
            chat_id=chat_id,
        )

    def send_photo(
        self,
        photo_url: str,
        caption: Optional[str] = None,
        chat_id: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Send photo with caption to configured Telegram chat with auto-retry."""
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        payload = {
            "chat_id": target_chat,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        for attempt in range(1, 4):
            try:
                res = self.session.post(url, json=payload, timeout=20)
                res_data = res.json()
                if not res_data.get("ok"):
                    desc = res_data.get("description", "")
                    logger.error("Telegram photo upload failed: %s", desc)
                    if "parse" in desc.lower() and "parse_mode" in payload:
                        payload.pop("parse_mode", None)
                        fallback_res = self.session.post(url, json=payload, timeout=20)
                        if fallback_res.json().get("ok"):
                            logger.info("Telegram photo delivered via plain text caption fallback.")
                            return True
                    return False
                logger.info("Telegram photo sent successfully.")
                return True
            except requests.RequestException as e:
                logger.warning("Telegram photo attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    import time
                    time.sleep(1.5)
        return False

    def send_deposit_alert(self, alert: DepositAlert, target_chat_ids: Optional[List[str]] = None) -> bool:
        """Dispatch a full deposit alert, with photo if available, or text fallback to all target chats."""
        formatted_text = self.format_message(alert)
        chats = target_chat_ids if (target_chat_ids and len(target_chat_ids) > 0) else ([self.chat_id] if self.chat_id else [])

        alert_inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📸 View Screenshot", "callback_data": "action_screen"},
                    {"text": "💳 Check Balance", "callback_data": "action_balance"},
                ],
                [
                    {"text": "📜 Recent History", "callback_data": "action_history"},
                    {"text": "🎛 Open Control Menu", "callback_data": "action_menu"},
                ],
            ]
        }

        any_success = False
        for c_id in chats:
            is_admin = bool(self.chat_id and str(c_id).strip() == str(self.chat_id).strip())
            keyboard = alert_inline_keyboard if is_admin else None
            sent = False
            if alert.screenshot_url:
                sent = self.send_photo(
                    alert.screenshot_url,
                    caption=formatted_text,
                    chat_id=c_id,
                    reply_markup=keyboard,
                )
            if not sent:
                sent = self.send_message(
                    formatted_text,
                    chat_id=c_id,
                    reply_markup=keyboard,
                )
            if sent:
                any_success = True

        return any_success

    def send_control_panel(
        self,
        text: Optional[str] = None,
        custom_keyboard: Optional[list] = None,
        active_device_name: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Send an interactive control panel with action buttons to Telegram."""
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False

        header = f"📱 *Active Device:* `{active_device_name}`\n" if active_device_name else ""
        message_text = text or (
            f"🎛 *Chime Cloud Phone Controller*\n"
            f"{header}"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Auto-monitoring is running across all registered devices.\n"
            "Use the buttons below for manual controls on the active device:"
        )

        keyboard = {"inline_keyboard": custom_keyboard} if custom_keyboard else {
            "inline_keyboard": [
                [
                    {"text": "🔄 Refresh & Check Now", "callback_data": "action_refresh"},
                ],
                [
                    {"text": "💳 Check Balance", "callback_data": "action_balance"},
                    {"text": "📜 Recent History", "callback_data": "action_history"},
                ],
                [
                    {"text": "📸 Screen Capture", "callback_data": "action_screen"},
                    {"text": "📱 Device Status", "callback_data": "action_status"},
                ],
                [
                    {"text": "📱 Switch Device", "callback_data": "action_switch_device"},
                    {"text": "📋 All Devices", "callback_data": "action_devices_overview"},
                ],
                [
                    {"text": "👥 Manage Users", "callback_data": "action_users_overview"},
                ],
            ]
        }

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": message_text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        }

        for attempt in range(1, 4):
            try:
                res = self.session.post(url, json=payload, timeout=15)
                res_data = res.json()
                if not res_data.get("ok"):
                    desc = res_data.get("description", "")
                    logger.error("Failed to send control panel: %s", desc)
                    if "parse" in desc.lower() and "parse_mode" in payload:
                        payload.pop("parse_mode", None)
                        fallback_res = self.session.post(url, json=payload, timeout=15)
                        if fallback_res.json().get("ok"):
                            logger.info("Control panel delivered via plain text fallback.")
                            return True
                    return False
                return True
            except requests.RequestException as e:
                logger.warning("Failed to send control panel attempt %d: %s", attempt, e)
                if attempt < 3:
                    import time
                    time.sleep(1.0)
        return False

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        """Acknowledge Telegram callback query to dismiss loading spinner."""
        if not self.is_configured:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            res = self.session.post(url, json=payload, timeout=10)
            return bool(res.json().get("ok"))
        except requests.RequestException:
            return False

    def send_chat_action(self, action: str = "typing", chat_id: Optional[str] = None) -> bool:
        """Send chat action (e.g. typing) so user immediately sees bot activity."""
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendChatAction"
        try:
            self.session.post(url, json={"chat_id": target_chat, "action": action}, timeout=5)
            return True
        except requests.RequestException:
            return False

    def send_device_overview(self, devices: list, active_device_id: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
        """Send comprehensive multi-device status overview with Connect/Disconnect toggle buttons."""
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False

        def _clean_str(s: Optional[str]) -> str:
            if not s:
                return ""
            return str(s).replace("*", "").replace("`", "").replace("_", " ").strip()

        total = len(devices)
        monitoring_count = sum(1 for d in devices if getattr(d, "enabled", False))
        paused_count = total - monitoring_count

        lines = [
            f"📱 *GeeLark Cloud Devices ({total})*",
            f"📊 *Status:* {monitoring_count} Monitoring 🟢 | {paused_count} Paused 🔴",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        buttons = []

        # Batch actions row
        buttons.append([
            {"text": f"🔌 Connect All ({total})", "callback_data": "action_connect_all"},
            {"text": "⏸ Disconnect All", "callback_data": "action_disconnect_all"},
        ])

        for idx, dev in enumerate(devices, 1):
            is_active = (dev.device_id == active_device_id)
            raw_name = getattr(dev, "name", "") or ""
            clean_name = _clean_str(raw_name) or f"Phone #{dev.serial}"
            status_icon = "🟢" if getattr(dev, "enabled", False) else "🔴"
            status_text = "Monitoring" if getattr(dev, "enabled", False) else "Paused"
            pin_status = f"`{dev.pin}`" if getattr(dev, "pin", None) else "⚠️ Not Set"
            bal = getattr(dev, "latest_balance", None) or "Pending sync"
            active_tag = " 🌟 *(Active)*" if is_active else ""

            lines.append(f"{idx}️⃣ {status_icon} *#{dev.serial} {clean_name}*{active_tag}")
            lines.append(f"   ├ 📊 Status: *{status_text}* | 💳 Balance: `{bal}`")
            lines.append(f"   └ 🔐 PIN: {pin_status}")
            lines.append("")

            # Action row for this device: 2 buttons per row so device name is never truncated
            display_name = clean_name
            if len(display_name) > 16:
                display_name = display_name[:15] + "…"

            dev_btn_text = f"{'✅' if is_active else status_icon} #{dev.serial} {display_name}"
            dev_btn = {"text": dev_btn_text, "callback_data": f"action_select_dev_{dev.serial}"}

            toggle_btn = (
                {"text": "⏸ Disconnect", "callback_data": f"action_toggle_dev_{dev.serial}"}
                if getattr(dev, "enabled", False)
                else {"text": "🔌 Connect", "callback_data": f"action_toggle_dev_{dev.serial}"}
            )

            buttons.append([dev_btn, toggle_btn])

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 *Tip:* Tap any device button on the left to open its full control card (Power ON/OFF, PIN, Screenshot).")

        footer = [
            [{"text": "🔍 Scan & Refresh from GeeLark", "callback_data": "action_scan_devices"}],
            [{"text": "🏠 Main Menu", "callback_data": "action_menu"}],
        ]

        keyboard = {"inline_keyboard": buttons + footer}

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": "\n".join(lines),
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        }

        for attempt in range(1, 4):
            try:
                res = self.session.post(url, json=payload, timeout=20)
                res_data = res.json()
                if not res_data.get("ok"):
                    desc = res_data.get("description", "")
                    logger.error("Telegram send_device_overview failed: %s", desc)
                    if "parse" in desc.lower() and "parse_mode" in payload:
                        payload.pop("parse_mode", None)
                        fallback_res = self.session.post(url, json=payload, timeout=20)
                        if fallback_res.json().get("ok"):
                            logger.info("send_device_overview delivered via plain text fallback.")
                            return True
                    return False
                return True
            except requests.RequestException as e:
                logger.warning("Telegram send_device_overview attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    import time
                    time.sleep(1.5)
        return False

    def send_pin_request(self, serial: str, device_name: str, chat_id: Optional[str] = None) -> bool:
        """Send prompt to Telegram asking user for device PIN."""
        msg = (
            "🔐 *Chime PIN Required for Device!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 *Device:* `#{serial} {device_name}`\n"
            "Chime is asking for a passcode, but no PIN is saved for this device.\n\n"
            "👉 *Please set the PIN now:*\n"
            f"`/setpin {serial} <your_4_digit_pin>`\n\n"
            f"_Example:_ `/setpin {serial} 1122`\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self.send_message(msg, chat_id=chat_id)
