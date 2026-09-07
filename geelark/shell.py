"""Shell execution and Android UI gesture automation module via GeeLark API."""

from __future__ import annotations

import shlex
import threading
import time
from typing import Optional

from geelark.client import GeeLarkClient
from geelark.exceptions import GeeLarkError

_UI_LOCK = threading.Lock()


class ShellManager:
    """Executes low-level Android shell commands and UI interactions on GeeLark phones."""

    def __init__(self, client: Optional[GeeLarkClient] = None) -> None:
        self.client = client or GeeLarkClient()

    def execute(self, phone_id: str, cmd: str) -> str:
        """Execute a raw shell command on the cloud phone and return its stdout."""
        payload = {"id": phone_id, "cmd": cmd}
        res = self.client.post("/open/v1/shell/execute", payload=payload)
        data = res.get("data", {})
        if not data.get("status", False):
            raise GeeLarkError(f"Shell command failed on phone {phone_id}: {cmd} -> {data.get('output')}")
        return data.get("output", "")

    def tap(self, phone_id: str, x: int, y: int) -> str:
        """Simulate tap gesture at screen coordinates (x, y)."""
        return self.execute(phone_id, f"input tap {x} {y}")

    def swipe(
        self,
        phone_id: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> str:
        """Simulate swipe gesture from (x1, y1) to (x2, y2)."""
        return self.execute(phone_id, f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def type_text(self, phone_id: str, text: str) -> str:
        """Type text into currently focused input field, handling spaces."""
        # Android input text treats '%s' as space
        escaped_text = text.replace(" ", "%s")
        return self.execute(phone_id, f"input text {shlex.quote(escaped_text)}")

    def keyevent(self, phone_id: str, keycode: int) -> str:
        """Send an Android keycode event."""
        return self.execute(phone_id, f"input keyevent {keycode}")

    def enter_pin(self, phone_id: str, pin: str) -> None:
        """Send PIN digits to device using atomic clear + input text with fallback."""
        try:
            self.execute(phone_id, f"input text {pin}")
            return
        except Exception:
            pass

        # Fallback to keyevents
        keycodes = [str(int(d) + 7) for d in pin if d.isdigit()]
        if keycodes:
            try:
                self.execute(phone_id, f"input keyevent {' '.join(keycodes)}")
            except Exception as e:
                logger.warning("Error entering PIN via keyevents: %s", e)

    def home(self, phone_id: str) -> str:
        """Press Home button."""
        return self.keyevent(phone_id, 3)

    def back(self, phone_id: str) -> str:
        """Press Back button."""
        return self.keyevent(phone_id, 4)

    def enter(self, phone_id: str) -> str:
        """Press Enter / Return."""
        return self.keyevent(phone_id, 66)

    def clear_app_data(self, phone_id: str, package_name: str) -> str:
        """Reset app data and cache via pm clear."""
        return self.execute(phone_id, f"pm clear {package_name}")

    def launch_app(self, phone_id: str, package_name: str) -> str:
        """Launch app using monkey launcher command."""
        return self.execute(
            phone_id,
            f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1",
        )

    def stop_app(self, phone_id: str, package_name: str) -> str:
        """Force stop an application."""
        return self.execute(phone_id, f"am force-stop {package_name}")

    def dump_hierarchy_xml(self, phone_id: str) -> str:
        """Dump the current UI hierarchy XML using uiautomator."""
        self.execute(phone_id, "uiautomator dump /sdcard/ui_dump.xml")
        time.sleep(0.5)
        return self.execute(phone_id, "cat /sdcard/ui_dump.xml")
    def get_screen_elements(self, phone_id: str) -> list[str]:
        """Extract all visible UI text and content descriptions directly on device safely and quickly."""
        with _UI_LOCK:
            cmd = "uiautomator dump --compressed /sdcard/ui.xml >/dev/null 2>&1 && grep -o -E '(text|content-desc)=\"[^\"]+\"' /sdcard/ui.xml || true"
            for attempt in range(1, 3):
                try:
                    out = self.execute(phone_id, cmd)
                    items: list[str] = []
                    for line in out.splitlines():
                        line = line.strip()
                        if '="' in line:
                            val = line.split('="', 1)[1].rstrip('"')
                            if val:
                                items.append(val)

                    if items:
                        return items
                    time.sleep(0.5)
                except Exception:
                    if attempt == 2:
                        return []
                    time.sleep(0.5)
            return []
