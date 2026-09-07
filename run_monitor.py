"""CLI runner for the Chime Deposit Detection & Telegram Alert Daemon."""

from __future__ import annotations

import argparse
import logging
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from config import Config
from automation.chime_monitor import ChimeMonitor

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ChimeMonitorRunner")


def main() -> None:
    """Entry point for running the deposit monitor."""
    parser = argparse.ArgumentParser(
        description="Chime Deposit Detection & Telegram Alert Daemon",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Polling interval in seconds (default: 20)",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Target phone serial or name (e.g. 226 or Katie-Smith-18)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate detection without sending actual Telegram alerts or updating store",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll check and exit",
    )

    args = parser.parse_args()

    cfg = Config()
    if args.device:
        cfg = Config(
            base_url=cfg.base_url,
            auth_mode=cfg.auth_mode,
            bearer_token=cfg.bearer_token,
            app_id=cfg.app_id,
            api_key=cfg.api_key,
            telegram_bot_token=cfg.telegram_bot_token,
            telegram_chat_id=cfg.telegram_chat_id,
            target_phone_serial=args.device,
            target_phone_name=args.device,
            poll_interval_seconds=args.interval or cfg.poll_interval_seconds,
            enable_screenshots=cfg.enable_screenshots,
        )

    print("=" * 60)
    print("🚀 Chime Deposit Detection & Telegram Alert Service")
    print(f"   Target Device: {cfg.target_phone_serial} / {cfg.target_phone_name}")
    print(f"   Poll Interval: {args.interval or cfg.poll_interval_seconds}s")
    print(f"   Dry Run Mode : {'ENABLED (Simulation Only)' if args.dry_run else 'DISABLED (Live Alerts)'}")
    print(f"   Telegram Bot : {'Configured' if cfg.telegram_bot_token else 'NOT Configured (Add to .env)'}")
    print("=" * 60)

    try:
        monitor = ChimeMonitor(config=cfg)

        # 1. Launch Telegram Interactive Bot Service in background thread if configured
        if not args.dry_run and not args.once and cfg.telegram_bot_token:
            from automation.telegram_bot_service import TelegramBotService
            import threading

            bot_service = TelegramBotService(monitor, config=cfg)
            bot_thread = threading.Thread(
                target=bot_service.start_polling,
                daemon=True,
                name="TelegramBotThread",
            )
            bot_thread.start()
            logger.info("Interactive Telegram Bot controls enabled.")

        if args.once:
            print("\nRunning single poll cycle...")
            detected = monitor.poll_once(dry_run=args.dry_run)
            print(f"\nPoll completed. Found {len(detected)} new inbound transactions.")
        else:
            monitor.run_forever(interval_seconds=args.interval, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\nService stopped by user.")
    except Exception as e:
        logger.error("Monitor execution error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
