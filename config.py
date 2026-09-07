"""Configuration module for GeeLark Cloud Phone API client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Holds configuration parameters for GeeLark API integration."""

    base_url: str = os.getenv("GEELARK_BASE_URL", "https://openapi.geelark.com").rstrip("/")
    auth_mode: str = os.getenv("GEELARK_AUTH_MODE", "token").lower()
    bearer_token: Optional[str] = os.getenv("GEELARK_BEARER_TOKEN")
    app_id: Optional[str] = os.getenv("GEELARK_APP_ID")
    api_key: Optional[str] = os.getenv("GEELARK_API_KEY")

    # Telegram Alert Configuration
    telegram_bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")

    # Chime Device Target & Monitoring Parameters
    target_phone_serial: Optional[str] = os.getenv("CHIME_TARGET_PHONE_SERIAL", "226")
    target_phone_name: Optional[str] = os.getenv("CHIME_TARGET_PHONE_NAME", "Katie-Smith-18")
    target_phone_id: Optional[str] = os.getenv("CHIME_TARGET_PHONE_ID")
    app_pin: Optional[str] = os.getenv("CHIME_APP_PIN", "1122")
    poll_interval_seconds: int = int(os.getenv("CHIME_POLL_INTERVAL", "30"))
    enable_screenshots: bool = os.getenv("ENABLE_TELEGRAM_SCREENSHOTS", "true").lower() == "true"

    def validate(self) -> None:
        """Validate that credentials are provided according to auth_mode."""
        if self.auth_mode == "token":
            if not self.bearer_token:
                raise ValueError(
                    "GEELARK_BEARER_TOKEN is required when auth_mode is 'token'. "
                    "Generate a token in GeeLark desktop app -> GeeHub."
                )
        elif self.auth_mode == "key":
            if not self.app_id or not self.api_key:
                raise ValueError(
                    "Both GEELARK_APP_ID and GEELARK_API_KEY are required when auth_mode is 'key'."
                )
        else:
            raise ValueError(f"Invalid auth_mode: '{self.auth_mode}'. Expected 'token' or 'key'.")


default_config = Config()
