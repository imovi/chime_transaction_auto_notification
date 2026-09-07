"""Application management module for GeeLark Cloud Phones."""

from __future__ import annotations

from typing import Any, Dict, Optional

from geelark.client import GeeLarkClient


class AppManager:
    """Manages application installation, startup, and termination on GeeLark phones."""

    def __init__(self, client: Optional[GeeLarkClient] = None) -> None:
        self.client = client or GeeLarkClient()

    def list_installed(
        self,
        env_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """List applications currently installed on target cloud phone."""
        payload = {"envId": env_id, "page": page, "pageSize": page_size}
        response = self.client.post("/open/v1/app/list", payload=payload)
        return response.get("data", {})

    def install_app(self, env_id: str, app_version_id: str) -> Dict[str, Any]:
        """Install an application onto the cloud phone."""
        payload = {"envId": env_id, "appVersionId": app_version_id}
        return self.client.post("/open/v1/app/install", payload=payload)

    def start_app(
        self,
        env_id: str,
        package_name: Optional[str] = None,
        app_version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Launch an application on target cloud phone."""
        payload: Dict[str, Any] = {"envId": env_id}
        if package_name:
            payload["packageName"] = package_name
        elif app_version_id:
            payload["appVersionId"] = app_version_id
        else:
            raise ValueError("Either package_name or app_version_id must be provided.")
        return self.client.post("/open/v1/app/start", payload=payload)

    def stop_app(
        self,
        env_id: str,
        package_name: Optional[str] = None,
        app_version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stop a running application on target cloud phone."""
        payload: Dict[str, Any] = {"envId": env_id}
        if package_name:
            payload["packageName"] = package_name
        elif app_version_id:
            payload["appVersionId"] = app_version_id
        else:
            raise ValueError("Either package_name or app_version_id must be provided.")
        return self.client.post("/open/v1/app/stop", payload=payload)
