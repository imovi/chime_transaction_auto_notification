"""Core HTTP Client for interacting with GeeLark Cloud Phone OpenAPI."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Dict, Optional

import requests

from config import Config, default_config
from geelark.exceptions import (
    GeeLarkAPIError,
    GeeLarkAuthenticationError,
    GeeLarkDeviceError,
    GeeLarkError,
    GeeLarkRateLimitError,
)


class GeeLarkClient:
    """HTTP Client providing authenticated requests to GeeLark OpenAPI."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or default_config
        self.session = requests.Session()

    @staticmethod
    def generate_trace_id() -> str:
        """Generate a 32-character uppercase UUID v4 trace identifier."""
        return uuid.uuid4().hex.upper()

    def build_headers(self, trace_id: Optional[str] = None) -> Dict[str, str]:
        """Construct authentication headers based on configured mode."""
        trace = trace_id or self.generate_trace_id()
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "traceId": trace,
        }

        mode = getattr(self.config, "auth_mode", "auto") or "auto"

        if mode == "token":
            if not self.config.bearer_token:
                raise GeeLarkError("Bearer token not configured. Set GEELARK_BEARER_TOKEN.")
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            return headers

        if mode == "key":
            if not self.config.app_id or not self.config.api_key:
                raise GeeLarkError("appId and apiKey are required for key verification.")
            ts = str(int(time.time() * 1000))
            nonce = trace[:6]
            sign_raw = f"{self.config.app_id}{trace}{ts}{nonce}{self.config.api_key}"
            signature = hashlib.sha256(sign_raw.encode("utf-8")).hexdigest().upper()

            headers.update({
                "appId": self.config.app_id,
                "ts": ts,
                "nonce": nonce,
                "sign": signature,
            })
            if self.config.bearer_token:
                headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            return headers

        # mode == "auto" (All-in-One auto detection)
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            if self.config.app_id and self.config.api_key:
                ts = str(int(time.time() * 1000))
                nonce = trace[:6]
                sign_raw = f"{self.config.app_id}{trace}{ts}{nonce}{self.config.api_key}"
                signature = hashlib.sha256(sign_raw.encode("utf-8")).hexdigest().upper()
                headers.update({
                    "appId": self.config.app_id,
                    "ts": ts,
                    "nonce": nonce,
                    "sign": signature,
                })
            return headers

        if self.config.app_id and self.config.api_key:
            ts = str(int(time.time() * 1000))
            nonce = trace[:6]
            sign_raw = f"{self.config.app_id}{trace}{ts}{nonce}{self.config.api_key}"
            signature = hashlib.sha256(sign_raw.encode("utf-8")).hexdigest().upper()
            headers.update({
                "appId": self.config.app_id,
                "ts": ts,
                "nonce": nonce,
                "sign": signature,
            })
            return headers

        raise GeeLarkError("No valid GeeLark credentials provided (Bearer Token or App ID + API Key required).")

    def _map_api_error(self, code: int, msg: str, trace_id: str, data: Any) -> GeeLarkAPIError:
        """Map GeeLark error codes to specialized exceptions."""
        if code in (40003, 40011, 40013, 40015, 40016):
            return GeeLarkAuthenticationError(code, msg, trace_id, data)
        if code in (40007, 40014, 40017):
            return GeeLarkRateLimitError(code, msg, trace_id, data)
        if 42000 <= code <= 43999:
            return GeeLarkDeviceError(code, msg, trace_id, data)
        return GeeLarkAPIError(code, msg, trace_id, data)

    def post(
        self,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Send an authenticated POST request to a GeeLark OpenAPI endpoint."""
        url = f"{self.config.base_url}/{endpoint.lstrip('/')}"
        trace_id = self.generate_trace_id()
        headers = self.build_headers(trace_id=trace_id)
        body = payload if payload is not None else {}

        try:
            response = self.session.post(url, json=body, headers=headers, timeout=timeout)
            response.raise_for_status()
            res_json = response.json()
        except requests.RequestException as e:
            raise GeeLarkError(f"HTTP request to GeeLark failed: {e}") from e

        code = res_json.get("code", -1)
        msg = res_json.get("msg", "Unknown response")
        resp_trace_id = res_json.get("traceId", trace_id)
        data = res_json.get("data")

        if code != 0:
            raise self._map_api_error(code, msg, resp_trace_id, data)

        return res_json
