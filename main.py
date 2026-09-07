"""GeeLark Automation CLI and diagnostic runner."""

from __future__ import annotations

import argparse
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from config import default_config
from geelark.client import GeeLarkClient
from geelark.exceptions import GeeLarkAPIError, GeeLarkError
from geelark.phone import PhoneManager
from geelark.shell import ShellManager


def check_connection(phone_mgr: PhoneManager) -> None:
    """Verify API connectivity and print account status."""
    print("Checking GeeLark API connection...")
    try:
        data = phone_mgr.list_phones(page=1, page_size=5)
        total = data.get("total", 0)
        items = data.get("items", [])
        print(f"Connection Successful! Total Cloud Phones in account: {total}")
        for item in items:
            status_desc = {0: "Running", 1: "Starting", 2: "Shutdown"}.get(
                item.get("status", -1), "Unknown"
            )
            print(
                f" - [{item.get('id')}] {item.get('serialName')} "
                f"(Status: {status_desc}, SerialNo: {item.get('serialNo')})"
            )
    except GeeLarkAPIError as e:
        print(f"API Error [Code {e.code}]: {e.message}")
        sys.exit(1)
    except GeeLarkError as e:
        print(f"Configuration / Network Error: {e}")
        sys.exit(1)


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="GeeLark Cloud Phone Automation CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: check
    subparsers.add_parser("check", help="Check API credentials and connectivity")

    # Command: list
    list_parser = subparsers.add_parser("list", help="List cloud phones")
    list_parser.add_argument("--page", type=int, default=1, help="Page number")
    list_parser.add_argument("--page-size", type=int, default=10, help="Page size")

    # Command: start
    start_parser = subparsers.add_parser("start", help="Start cloud phone")
    start_parser.add_argument("phone_id", type=str, help="Cloud phone ID")

    # Command: stop
    stop_parser = subparsers.add_parser("stop", help="Stop cloud phone")
    stop_parser.add_argument("phone_id", type=str, help="Cloud phone ID")

    # Command: shell
    shell_parser = subparsers.add_parser("shell", help="Run shell command on phone")
    shell_parser.add_argument("phone_id", type=str, help="Cloud phone ID")
    shell_parser.add_argument("cmd", type=str, help="Android shell command")

    # Command: screenshot
    shot_parser = subparsers.add_parser("screenshot", help="Capture screenshot")
    shot_parser.add_argument("phone_id", type=str, help="Cloud phone ID")

    args = parser.parse_args()

    client = GeeLarkClient(default_config)
    phone_mgr = PhoneManager(client)
    shell_mgr = ShellManager(client)

    if args.command == "check" or args.command is None:
        check_connection(phone_mgr)
    elif args.command == "list":
        res = phone_mgr.list_phones(page=args.page, page_size=args.page_size)
        items = res.get("items", [])
        print(f"Total: {res.get('total', 0)} phones")
        for item in items:
            print(f" - ID: {item.get('id')}, Name: {item.get('serialName')}, Status: {item.get('status')}")
    elif args.command == "start":
        res = phone_mgr.start_phone([args.phone_id])
        print(f"Start requested for {args.phone_id}: {res}")
    elif args.command == "stop":
        res = phone_mgr.stop_phone([args.phone_id])
        print(f"Stop requested for {args.phone_id}: {res}")
    elif args.command == "shell":
        output = shell_mgr.execute(args.phone_id, args.cmd)
        print(f"Output:\n{output}")
    elif args.command == "screenshot":
        task_id = phone_mgr.capture_screenshot(args.phone_id)
        print(f"Screenshot task triggered: {task_id}")
        url = phone_mgr.wait_for_screenshot(task_id)
        print(f"Screenshot URL: {url}")


if __name__ == "__main__":
    main()
