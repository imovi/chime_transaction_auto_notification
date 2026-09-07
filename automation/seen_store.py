"""Persistent storage for tracking processed transactions to avoid duplicate alerts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Set

logger = logging.getLogger(__name__)


class SeenTransactionStore:
    """Manages a persistent set of transaction hashes with thread-safe access."""

    def __init__(self, filepath: str = "data/seen_transactions.json") -> None:
        self.filepath = filepath
        self.seen_hashes: Set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """Load seen transaction hashes from disk."""
        if not os.path.exists(self.filepath) or os.path.getsize(self.filepath) == 0:
            return

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self.seen_hashes = set(data)
                elif isinstance(data, dict):
                    self.seen_hashes = set(data.get("seen_hashes", []))
            logger.debug("Loaded %d seen transaction hashes.", len(self.seen_hashes))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load seen transactions store: %s", e)

    def _save(self) -> None:
        """Save seen transaction hashes to disk atomically."""
        os.makedirs(os.path.dirname(self.filepath) or ".", exist_ok=True)
        temp_file = f"{self.filepath}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump({"seen_hashes": list(self.seen_hashes)}, f, indent=2)
            os.replace(temp_file, self.filepath)
        except OSError as e:
            logger.error("Failed to save seen transactions store: %s", e)

    @staticmethod
    def compute_hash(sender: str, amount: str, time_str: str = "", note: str = "", balance: str = "") -> str:
        """Generate a deterministic SHA-256 fingerprint for a transaction."""
        key = f"{sender.strip().lower()}|{amount.strip()}|{time_str.strip()}|{note.strip()}|{balance.strip()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def is_seen(self, tx_hash: str) -> bool:
        """Check if a transaction hash has already been recorded."""
        with self._lock:
            return tx_hash in self.seen_hashes

    def mark_seen(self, tx_hash: str) -> None:
        """Record a transaction hash and persist to disk."""
        with self._lock:
            if tx_hash not in self.seen_hashes:
                self.seen_hashes.add(tx_hash)
                self._save()

    def check_and_mark_seen(self, tx_hash: str) -> bool:
        """Atomically check if tx_hash is unseen. If unseen, mark it seen and return True; else return False."""
        with self._lock:
            if tx_hash in self.seen_hashes:
                return False
            self.seen_hashes.add(tx_hash)
            self._save()
            return True
