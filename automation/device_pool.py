"""Device pool manager for allocating, starting, and rotating GeeLark cloud phones."""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from geelark.client import GeeLarkClient
from geelark.phone import PhoneManager

logger = logging.getLogger(__name__)


class DevicePool:
    """Manages cloud phone allocation, power states, and anti-detect rotation."""

    def __init__(self, client: Optional[GeeLarkClient] = None) -> None:
        self.phone_mgr = PhoneManager(client)

    def get_available_phones(self) -> List[Dict]:
        """Fetch list of all available cloud phones."""
        res = self.phone_mgr.list_phones(page=1, page_size=100)
        return res.get("items", [])

    def ensure_phone_started(self, phone_id: str, timeout_seconds: int = 60) -> str:
        """Ensure cloud phone is started and return the remote web streaming URL."""
        status_list = self.phone_mgr.query_status([phone_id])
        if status_list and status_list[0].get("status") == 0:
            logger.info("Phone %s is already running.", phone_id)
            return ""

        logger.info("Starting cloud phone %s...", phone_id)
        start_res = self.phone_mgr.start_phone([phone_id])
        details = start_res.get("successDetails", [])
        web_url = details[0].get("url", "") if details else ""

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            status_list = self.phone_mgr.query_status([phone_id])
            if status_list and status_list[0].get("status") == 0:
                logger.info("Phone %s started successfully.", phone_id)
                return web_url
            time.sleep(3)

        raise TimeoutError(f"Phone {phone_id} did not reach running state within {timeout_seconds}s")

    def stop_phone(self, phone_id: str) -> None:
        """Gracefully shut down a cloud phone."""
        logger.info("Stopping cloud phone %s...", phone_id)
        self.phone_mgr.stop_phone([phone_id])

    def rotate_device_identity(self, phone_id: str) -> None:
        """Perform one-click new machine to randomize IMEI/MAC/hardware fingerprint."""
        logger.info("Rotating device identity for %s...", phone_id)
        self.phone_mgr.one_click_new_machine(
            phone_id=phone_id,
            change_brand_model=True,
            keep_net_type=False,
            keep_phone_number=False,
            keep_region=True,
            keep_language=True,
        )
