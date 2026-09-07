"""ADB remote management module for GeeLark Cloud Phones."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from geelark.client import GeeLarkClient


class ADBManager:
    """Controls ADB status and retrieves remote IP/port/credentials for cloud phones."""

    def __init__(self, client: Optional[GeeLarkClient] = None) -> None:
        self.client = client or GeeLarkClient()

    def set_status(self, phone_ids: List[str], open_adb: bool = True) -> Dict[str, Any]:
        """Enable or disable ADB access on target cloud phones."""
        payload = {"ids": phone_ids, "open": open_adb}
        response = self.client.post("/open/v1/adb/setStatus", payload=payload)
        return response

    def get_adb_info(self, phone_ids: List[str]) -> List[Dict[str, Any]]:
        """Retrieve ADB connection credentials (ip, port, pwd) for target cloud phones."""
        payload = {"ids": phone_ids}
        response = self.client.post("/open/v1/adb/getData", payload=payload)
        return response.get("data", {}).get("items", [])
