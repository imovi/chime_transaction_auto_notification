"""Unit tests for GeeLark client authentication, signing, and error handling."""

import hashlib
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from geelark.client import GeeLarkClient
from geelark.exceptions import (
    GeeLarkAPIError,
    GeeLarkAuthenticationError,
    GeeLarkDeviceError,
    GeeLarkError,
    GeeLarkRateLimitError,
)
from geelark.phone import PhoneManager
from geelark.shell import ShellManager


class TestGeeLarkClient(unittest.TestCase):
    """Tests for GeeLarkClient core functionality."""

    def test_token_auth_headers(self) -> None:
        cfg = Config(auth_mode="token", bearer_token="test-secret-token")
        client = GeeLarkClient(cfg)
        headers = client.build_headers(trace_id="1234567890ABCDEF1234567890ABCDEF")

        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["traceId"], "1234567890ABCDEF1234567890ABCDEF")
        self.assertEqual(headers["Authorization"], "Bearer test-secret-token")
        self.assertNotIn("sign", headers)

    def test_key_auth_headers_and_signature(self) -> None:
        app_id = "5ZPEQNCSG313NX2NM6RIUE18SG"
        api_key = "secret_key_abc_123"
        cfg = Config(auth_mode="key", app_id=app_id, api_key=api_key)
        client = GeeLarkClient(cfg)

        trace_id = "AABBCCDDEEFF00112233445566778899"
        headers = client.build_headers(trace_id=trace_id)

        self.assertEqual(headers["appId"], app_id)
        self.assertEqual(headers["traceId"], trace_id)
        self.assertEqual(headers["nonce"], "AABBCC")
        self.assertTrue(headers["ts"].isdigit())

        # Verify SHA-256 calculation
        expected_raw = f"{app_id}{trace_id}{headers['ts']}{headers['nonce']}{api_key}"
        expected_sign = hashlib.sha256(expected_raw.encode("utf-8")).hexdigest().upper()
        self.assertEqual(headers["sign"], expected_sign)

    def test_missing_token_raises_error(self) -> None:
        cfg = Config(auth_mode="token", bearer_token=None)
        client = GeeLarkClient(cfg)
        with self.assertRaises(GeeLarkError):
            client.build_headers()

    def test_missing_key_raises_error(self) -> None:
        cfg = Config(auth_mode="key", app_id="some_id", api_key=None)
        client = GeeLarkClient(cfg)
        with self.assertRaises(GeeLarkError):
            client.build_headers()

    def test_error_mapping(self) -> None:
        client = GeeLarkClient(Config(auth_mode="token", bearer_token="dummy"))

        err_auth = client._map_api_error(40003, "Signature verification failed", "trace1", None)
        self.assertIsInstance(err_auth, GeeLarkAuthenticationError)

        err_rate = client._map_api_error(40007, "Rate limited", "trace2", None)
        self.assertIsInstance(err_rate, GeeLarkRateLimitError)

        err_dev = client._map_api_error(42001, "Phone not found", "trace3", None)
        self.assertIsInstance(err_dev, GeeLarkDeviceError)

        err_generic = client._map_api_error(50000, "Internal error", "trace4", None)
        self.assertIsInstance(err_generic, GeeLarkAPIError)

    @patch("requests.Session.post")
    def test_successful_post(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "traceId": "TRACE123",
            "data": {"total": 5, "items": []},
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = GeeLarkClient(Config(auth_mode="token", bearer_token="dummy"))
        res = client.post("/open/v1/phone/list", payload={"page": 1})
        self.assertEqual(res["code"], 0)
        self.assertEqual(res["data"]["total"], 5)


class TestManagers(unittest.TestCase):
    """Tests for PhoneManager and ShellManager."""

    @patch.object(GeeLarkClient, "post")
    def test_phone_manager_list_and_start(self, mock_post: MagicMock) -> None:
        mock_post.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"items": [{"id": "p1", "serialName": "phone1"}]},
        }
        mgr = PhoneManager()
        phones = mgr.list_phones(page=1, page_size=10)
        self.assertEqual(len(phones["items"]), 1)
        mock_post.assert_called_with(
            "/open/v1/phone/list",
            payload={"page": 1, "pageSize": 10},
        )

    @patch.object(GeeLarkClient, "post")
    def test_shell_manager_tap_and_type(self, mock_post: MagicMock) -> None:
        mock_post.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"status": True, "output": ""},
        }
        shell = ShellManager()
        shell.tap("p1", 100, 200)
        mock_post.assert_called_with(
            "/open/v1/shell/execute",
            payload={"id": "p1", "cmd": "input tap 100 200"},
        )


if __name__ == "__main__":
    unittest.main()
