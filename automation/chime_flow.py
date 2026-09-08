"""Chime app automation workflow orchestrator."""

from __future__ import annotations

import logging
import time
from typing import Optional

from geelark.client import GeeLarkClient
from geelark.phone import PhoneManager
from geelark.shell import ShellManager

logger = logging.getLogger(__name__)

CHIME_PACKAGE_NAME = "com.onedebit.chime"


class ChimeAutomationFlow:
    """Orchestrates Chime app interactions on a target GeeLark cloud phone."""

    def __init__(self, phone_id: str, client: Optional[GeeLarkClient] = None) -> None:
        self.phone_id = phone_id
        self.client = client or GeeLarkClient()
        self.shell = ShellManager(self.client)
        self.phone_mgr = PhoneManager(self.client)

    def launch_chime(self) -> None:
        """Launch Chime application on the device."""
        logger.info("Launching Chime app on phone %s...", self.phone_id)
        self.shell.launch_app(self.phone_id, CHIME_PACKAGE_NAME)

    def reset_chime_storage(self) -> None:
        """Clear cache and app data for a fresh session."""
        logger.info("Resetting Chime app storage on phone %s...", self.phone_id)
        self.shell.clear_app_data(self.phone_id, CHIME_PACKAGE_NAME)

    def force_stop_chime(self) -> None:
        """Force close the Chime app."""
        logger.info("Stopping Chime app on phone %s...", self.phone_id)
        self.shell.stop_app(self.phone_id, CHIME_PACKAGE_NAME)

    def tap_coordinates(self, x: int, y: int, delay_after: float = 1.0) -> None:
        """Tap screen coordinates and wait for render."""
        self.shell.tap(self.phone_id, x, y)
        time.sleep(delay_after)

    def enter_text(self, text: str, delay_after: float = 1.0) -> None:
        """Type text into active input field."""
        self.shell.type_text(self.phone_id, text)
        time.sleep(delay_after)

    def capture_step_screenshot(self) -> Optional[str]:
        """Trigger screenshot for visual step validation."""
        task_id = self.phone_mgr.capture_screenshot(self.phone_id)
        if not task_id:
            return None
        return self.phone_mgr.wait_for_screenshot(task_id, max_attempts=8, interval_seconds=1.5)

    def inspect_ui_xml(self) -> str:
        """Fetch current screen UI dump for element parsing."""
        return self.shell.dump_hierarchy_xml(self.phone_id)

    def is_in_foreground(self) -> bool:
        """Check if Chime app is the active foreground window."""
        try:
            out = self.shell.execute(
                self.phone_id,
                "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' || true",
            )
            if CHIME_PACKAGE_NAME in out:
                return True
            out2 = self.shell.execute(
                self.phone_id,
                "dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' || true",
            )
            return CHIME_PACKAGE_NAME in out2
        except Exception:
            return False

    def is_connection_error(self, elements: Optional[list[str]] = None) -> bool:
        """Check if current screen displays 'Please check your connection' or network error."""
        elems = elements if elements is not None else self.shell.get_screen_elements(self.phone_id)
        error_keywords = (
            "please check your connection",
            "check your connection",
            "no internet connection",
            "no connection",
            "network error",
            "unable to connect",
            "can't connect",
            "cannot connect",
            "something went wrong",
        )
        return any(any(k in it.lower() for k in error_keywords) for it in elems)

    def restart_app_cleanly(self, pin: Optional[str] = "1122") -> dict:
        """Exit app, force-stop/clear from recents, and relaunch Chime fresh.

        Matches user requirement: 'full app theke ber hoy recent er theke app kete dukbe'
        """
        logger.info("Executing clean app restart for %s on phone %s...", CHIME_PACKAGE_NAME, self.phone_id)
        # 1. Exit app to Home screen
        try:
            self.shell.home(self.phone_id)
            time.sleep(0.8)
        except Exception:
            pass

        # 2. Force-stop app to wipe from recents & memory
        try:
            self.shell.stop_app(self.phone_id, CHIME_PACKAGE_NAME)
            time.sleep(1.5)
        except Exception as e:
            logger.warning("Could not force-stop app on %s: %s", self.phone_id, e)

        # 3. Collapse notification shade or system dialogs
        try:
            self.shell.execute(self.phone_id, "cmd statusbar collapse || true")
        except Exception:
            pass

        # 4. Relaunch Chime
        try:
            self.shell.launch_app(self.phone_id, CHIME_PACKAGE_NAME)
            time.sleep(3.0)
        except Exception as e:
            logger.error("Failed to launch Chime during restart on %s: %s", self.phone_id, e)

        # 5. Read screen elements
        elements = self.shell.get_screen_elements(self.phone_id)

        # 6. Check if PIN is requested
        pin_prompts = [
            "passcode",
            "enter pin",
            "forgot passcode",
            "enter your pin",
            "enter your passcode",
            "unlock passcode",
            "pin input",
            "mobile unlock",
        ]
        needs_pin = any(any(p in it.lower() for p in pin_prompts) for it in elements)
        if needs_pin and pin:
            logger.info("Entering PIN after clean restart on %s...", self.phone_id)
            self.shell.enter_pin(self.phone_id, pin)
            time.sleep(1.5)
            elements = self.shell.get_screen_elements(self.phone_id)
            if any(any(p in it.lower() for p in pin_prompts) for it in elements):
                self.shell.enter_pin(self.phone_id, pin)
                time.sleep(1.5)
                elements = self.shell.get_screen_elements(self.phone_id)

        # 7. Check if connection error still shows
        has_conn_err = self.is_connection_error(elements)
        if not has_conn_err:
            elements = self.open_checking_screen(elements)

        return {
            "unlocked": not needs_pin or bool(pin),
            "needs_pin": needs_pin and not bool(pin),
            "connection_error": has_conn_err,
            "elements": elements,
            "restarted": True,
        }

    def ensure_open_and_unlocked(self, pin: Optional[str] = "1122") -> dict:
        """Ensure Chime is in foreground and unlock PIN screen if prompted.

        Returns:
            dict with 'unlocked': bool, 'needs_pin': bool, 'connection_error': bool, 'elements': list[str]
        """
        # 1. Wake up screen and dismiss any notification shades
        try:
            self.shell.execute(self.phone_id, "input keyevent 224 && input keyevent 82 && cmd statusbar collapse")
        except Exception:
            pass

        # 2. Relaunch if not in foreground
        if not self.is_in_foreground():
            logger.info("Chime is not active in foreground. Launching app on %s...", self.phone_id)
            try:
                self.shell.launch_app(self.phone_id, CHIME_PACKAGE_NAME)
                time.sleep(2.5)
            except Exception as e:
                logger.warning("Launch app failed on %s: %s, attempting clean restart...", self.phone_id, e)
                return self.restart_app_cleanly(pin=pin)

        # 3. Check screen elements
        elements = self.shell.get_screen_elements(self.phone_id)

        # 4. Check for connection error ("Please check your connection")
        if self.is_connection_error(elements):
            logger.warning("Connection error screen detected on %s ('Please check your connection'). Restarting app cleanly...", self.phone_id)
            return self.restart_app_cleanly(pin=pin)

        # 5. Check if PIN / Passcode screen is showing
        pin_prompts = [
            "passcode",
            "enter pin",
            "forgot passcode",
            "enter your pin",
            "enter your passcode",
            "unlock passcode",
            "pin input",
            "mobile unlock",
        ]
        needs_pin = any(any(p in it.lower() for p in pin_prompts) for it in elements)

        if needs_pin:
            if pin:
                logger.info("🔐 PIN prompt detected on %s. Entering PIN %s...", self.phone_id, pin)
                self.shell.enter_pin(self.phone_id, pin)
                time.sleep(1.5)
                # Fetch fresh elements after unlock
                elements = self.shell.get_screen_elements(self.phone_id)
                # If still showing PIN screen, retry once
                if any(any(p in it.lower() for p in pin_prompts) for it in elements):
                    self.shell.enter_pin(self.phone_id, pin)
                    time.sleep(1.5)
                    elements = self.shell.get_screen_elements(self.phone_id)
                # Check connection error after unlock
                if self.is_connection_error(elements):
                    return self.restart_app_cleanly(pin=pin)
                elements = self.open_checking_screen(elements)
                return {"unlocked": True, "needs_pin": False, "connection_error": False, "elements": elements}
            else:
                logger.warning("🔐 PIN prompt detected on %s but no PIN is configured!", self.phone_id)
                return {"unlocked": False, "needs_pin": True, "connection_error": False, "elements": elements}

        # 6. Automatically keep device on the Checking / Transactions screen
        elements = self.open_checking_screen(elements)
        return {"unlocked": True, "needs_pin": False, "connection_error": False, "elements": elements}

    def is_on_checking_screen(self, elements: Optional[list[str]] = None) -> bool:
        """Check if current screen is already the Checking/Transactions screen."""
        elems = elements if elements is not None else self.shell.get_screen_elements(self.phone_id)
        has_transactions = any("transaction" in it.lower() for it in elems)
        has_navigate_up = any(it.strip() in ("Navigate up", "Back") for it in elems)
        has_checking = any(it.strip() == "Checking" for it in elems)
        has_actions = any(it.strip() in ("Transfer", "Cards", "Insights") for it in elems)
        return has_transactions or (has_checking and (has_actions or has_navigate_up))

    def open_checking_screen(self, current_elements: Optional[list[str]] = None) -> list[str]:
        """Ensure the phone is on the Checking/Transactions screen, staying on it permanently."""
        elems = current_elements if current_elements is not None else self.shell.get_screen_elements(self.phone_id)
        if self.is_on_checking_screen(elems):
            return elems

        # Not yet on Checking screen (e.g. on Home screen). Tap checking_account_row (x=540, y=305)
        logger.info("Opening Checking/Transactions screen on %s...", self.phone_id)
        self.shell.tap(self.phone_id, 540, 305)
        time.sleep(1.2)
        return self.shell.get_screen_elements(self.phone_id)

    def return_to_home(self) -> None:
        """No-op: App now permanently stays on the Checking screen so transactions update in place."""
        pass
