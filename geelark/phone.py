"""Cloud Phone lifecycle and profile management module."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from geelark.client import GeeLarkClient


class PhoneManager:
    """Manages cloud phone lifecycles, power states, and device fingerprints."""

    def __init__(self, client: Optional[GeeLarkClient] = None) -> None:
        self.client = client or GeeLarkClient()

    def list_phones(
        self,
        page: int = 1,
        page_size: int = 20,
        ids: Optional[List[str]] = None,
        serial_name: Optional[str] = None,
        group_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        open_status: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fetch list of cloud phones matching filters."""
        payload: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if ids:
            payload["ids"] = ids
        if serial_name:
            payload["serialName"] = serial_name
        if group_name:
            payload["groupName"] = group_name
        if tags:
            payload["tags"] = tags
        if open_status is not None:
            payload["openStatus"] = open_status

        response = self.client.post("/open/v1/phone/list", payload=payload)
        return response.get("data", {})

    def start_phone(
        self,
        phone_ids: List[str],
        width: int = 336,
        center: int = 1,
        energy_saving_mode: int = 0,
    ) -> Dict[str, Any]:
        """Start one or multiple cloud phones."""
        payload = {
            "ids": phone_ids,
            "width": width,
            "center": center,
            "energySavingMode": energy_saving_mode,
        }
        response = self.client.post("/open/v1/phone/start", payload=payload)
        data = response.get("data", {})
        if data.get("failAmount", 0) > 0 and data.get("failDetails"):
            fail_msg = data["failDetails"][0].get("msg", "Unknown start failure")
            raise GeeLarkError(f"GeeLark start failed: {fail_msg}")
        return data

    def stop_phone(self, phone_ids: List[str]) -> Dict[str, Any]:
        """Stop one or multiple cloud phones."""
        payload = {"ids": phone_ids}
        response = self.client.post("/open/v1/phone/stop", payload=payload)
        data = response.get("data", {})
        if data.get("failAmount", 0) > 0 and data.get("failDetails"):
            fail_msg = data["failDetails"][0].get("msg", "Unknown stop failure")
            raise GeeLarkError(f"GeeLark stop failed: {fail_msg}")
        return data

    def query_status(self, phone_ids: List[str]) -> List[Dict[str, Any]]:
        """Query power and operational status of cloud phones."""
        payload = {"ids": phone_ids}
        response = self.client.post("/open/v1/phone/status", payload=payload)
        data = response.get("data", {})
        return data.get("successDetails") or data.get("items", [])

    def delete_phone(self, phone_ids: List[str]) -> Dict[str, Any]:
        """Delete cloud phones."""
        payload = {"ids": phone_ids}
        response = self.client.post("/open/v1/phone/delete", payload=payload)
        return response.get("data", {})

    def one_click_new_machine(
        self,
        phone_id: str,
        change_brand_model: bool = True,
        keep_net_type: bool = False,
        keep_phone_number: bool = False,
        keep_region: bool = False,
        keep_language: bool = False,
    ) -> Dict[str, Any]:
        """Reset and randomize hardware fingerprint for a cloud phone (One-click new machine V2)."""
        payload = {
            "id": phone_id,
            "changeBrandModel": change_brand_model,
            "keepNetType": keep_net_type,
            "keepPhoneNumber": keep_phone_number,
            "keepRegion": keep_region,
            "keepLanguage": keep_language,
        }
        response = self.client.post("/open/v2/phone/newOne", payload=payload)
        return response

    def capture_screenshot(self, phone_id: str) -> str:
        """Trigger a screenshot capture task and return taskId."""
        payload = {"id": phone_id}
        response = self.client.post("/open/v1/phone/screenShot", payload=payload)
        return response.get("data", {}).get("taskId", "")

    def get_screenshot_result(self, task_id: str) -> Dict[str, Any]:
        """Query screenshot status and obtain download link."""
        payload = {"taskId": task_id}
        response = self.client.post("/open/v1/phone/screenShot/result", payload=payload)
        return response.get("data", {})

    def wait_for_screenshot(
        self,
        task_id: str,
        max_attempts: int = 10,
        interval_seconds: float = 2.0,
    ) -> Optional[str]:
        """Poll screenshot task until ready and return download link."""
        for _ in range(max_attempts):
            res = self.get_screenshot_result(task_id)
            status = res.get("status")
            if status == 2:  # Succeeded
                return res.get("downloadLink")
            if status == 3 or status == 0:  # Failed
                return None
            time.sleep(interval_seconds)
        return None
