"""Transaction extraction and parsing from UI hierarchy XML and notification dumps."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ParsedTransaction:
    """Represents an extracted transaction event."""

    sender: str
    amount: str
    time_str: str = ""
    note: str = ""
    balance_after: str = ""
    source: str = "ui"  # "ui" or "notification"


class TransactionParser:
    """Parses Chime Android UI XML hierarchy and notification text."""

    POSITIVE_AMOUNT_REGEX = re.compile(r"^\+\s*\$[\d,]+(?:\.\d{2})?$")
    NEGATIVE_AMOUNT_REGEX = re.compile(r"^-\s*\$[\d,]+(?:\.\d{2})?$")
    BALANCE_REGEX = re.compile(r"^\$[\d,]+(?:\.\d{2})?$")
    CHIME_NOTIFICATION_PATTERN = re.compile(
        r"([\w\s\.\,\'\-]+)\s+sent\s+you\s+(\$[\d,]+(?:\.\d{2})?)",
        re.IGNORECASE,
    )
    CHIME_RECEIVED_PATTERN = re.compile(
        r"received\s+(\$[\d,]+(?:\.\d{2})?)\s+from\s+([\w\s\.\,\'\-]+)",
        re.IGNORECASE,
    )

    @classmethod
    def parse_elements(cls, elements: List[str]) -> List[ParsedTransaction]:
        """Extract inbound deposit transactions from a flat list of visual UI elements."""
        transactions: List[ParsedTransaction] = []

        for i, item in enumerate(elements):
            if cls.POSITIVE_AMOUNT_REGEX.match(item):
                amount = item
                sender = ""
                time_str = ""
                note = ""
                balance_after = ""

                # 1. Balance after: check element immediately following amount
                if i + 1 < len(elements) and cls.BALANCE_REGEX.match(elements[i + 1]):
                    balance_after = elements[i + 1]

                # 2. Backward scan to find note, time_str, and sender without bleeding into subsequent transactions
                k = i - 1
                while k >= 0 and k >= i - 8:
                    prev = elements[k]

                    # Stop if hitting another transaction amount or section header
                    if (
                        cls.POSITIVE_AMOUNT_REGEX.match(prev)
                        or cls.NEGATIVE_AMOUNT_REGEX.match(prev)
                        or prev in ("Checking", "Available", "Transactions", "Today", "Yesterday")
                        or re.match(r"^[A-Z][a-z]{2}\s+\d+", prev)
                    ):
                        break

                    # Skip composite accessibility strings (e.g. "&#128111;, Transfer from Aeriel D., ...")
                    if prev.startswith("&#") and ("," in prev or len(prev) > 10):
                        k -= 1
                        continue

                    # Skip standalone icon unicode (e.g. "&#128111;")
                    if prev.startswith("&#") and prev.endswith(";"):
                        k -= 1
                        continue

                    # Skip standalone balance (e.g. balance from prior tx)
                    if cls.BALANCE_REGEX.match(prev):
                        k -= 1
                        continue

                    if prev.startswith("For:"):
                        if not note:
                            note = prev
                    elif any(m in prev for m in ("AM", "PM", "•", "Pay Anyone")):
                        if not time_str:
                            time_str = prev
                    elif prev.startswith("Transfer from ") or prev.startswith("Deposit from "):
                        if not sender:
                            sender = prev
                    else:
                        if not sender:
                            sender = prev

                    k -= 1

                if not sender:
                    sender = "Unknown Sender"

                transactions.append(
                    ParsedTransaction(
                        sender=sender,
                        amount=amount,
                        time_str=time_str,
                        note=note,
                        balance_after=balance_after,
                        source="ui",
                    )
                )

        return transactions

    @classmethod
    def parse_ui_hierarchy(cls, xml_content: str) -> List[ParsedTransaction]:
        """Extract inbound deposit transactions from Android UI Automator XML dump."""
        if not xml_content or not xml_content.strip():
            return []

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return []

        elements: List[str] = []
        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            desc = (node.attrib.get("content-desc") or "").strip()
            val = text or desc
            if val:
                elements.append(val)

        return cls.parse_elements(elements)

    @classmethod
    def parse_notification_dump(cls, dump_output: str) -> List[ParsedTransaction]:
        """Extract deposit transactions from dumpsys notification output."""
        transactions: List[ParsedTransaction] = []
        if not dump_output or "com.onedebit.chime" not in dump_output:
            return transactions

        lines = dump_output.splitlines()
        for line in lines:
            line_str = line.strip()
            match_sent = cls.CHIME_NOTIFICATION_PATTERN.search(line_str)
            if match_sent:
                sender_name = match_sent.group(1).strip()
                amt = match_sent.group(2).strip()
                sender_clean = re.sub(r"^.*?android\.text=\s*", "", sender_name)
                transactions.append(
                    ParsedTransaction(
                        sender=f"Transfer from {sender_clean}",
                        amount=f"+{amt}",
                        time_str="Just now",
                        source="notification",
                    )
                )
                continue

            match_recv = cls.CHIME_RECEIVED_PATTERN.search(line_str)
            if match_recv:
                amt = match_recv.group(1).strip()
                sender_name = match_recv.group(2).strip()
                transactions.append(
                    ParsedTransaction(
                        sender=f"Transfer from {sender_name}",
                        amount=f"+{amt}",
                        time_str="Just now",
                        source="notification",
                    )
                )

        return transactions

    @classmethod
    def extract_balance_from_elements(cls, elements: List[str]) -> Optional[str]:
        """Extract available or checking balance from visual elements list."""
        for i, val in enumerate(elements):
            if val in ("Checking", "Available") and i + 1 < len(elements):
                next_val = elements[i + 1]
                if cls.BALANCE_REGEX.match(next_val):
                    return next_val
            if cls.BALANCE_REGEX.match(val) and i > 0 and elements[i - 1] in ("Checking", "Available"):
                return val
        return None

    @classmethod
    def extract_checking_balance(cls, xml_content: str) -> Optional[str]:
        """Extract the header checking account balance e.g. '$129.80'."""
        if not xml_content:
            return None
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return None

        elements = [
            (node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()
            for node in root.iter("node")
        ]

        return cls.extract_balance_from_elements(elements)

    @classmethod
    def extract_all_history_transactions(cls, elements: List[str]) -> List[dict]:
        """Extract both inbound (+) and outbound (-) transactions for history view."""
        history: List[dict] = []
        any_tx_regex = re.compile(r"^([\+\-])\s*\$([\d,]+(?:\.\d{2})?)$")

        for i, item in enumerate(elements):
            match = any_tx_regex.match(item)
            if match:
                sign = match.group(1)
                amount = f"{sign}${match.group(2)}"
                is_inbound = (sign == "+")
                title = ""
                detail = ""
                note = ""

                # Scan backwards from amount index to extract note, detail, and title
                k = i - 1
                while k >= 0 and k >= i - 5:
                    prev = elements[k]
                    # Stop if hitting another amount or main header
                    if any_tx_regex.match(prev) or prev in ("Checking", "Available", "Transactions"):
                        break
                    if prev.startswith("For:"):
                        if not note:
                            note = prev
                    elif any(m in prev for m in ("AM", "PM", "•", "Pay Anyone")):
                        if not detail:
                            detail = prev
                    elif cls.BALANCE_REGEX.match(prev) or prev in ("Today", "Yesterday") or re.match(r"^[A-Z][a-z]{2}\s+\d+", prev):
                        # Balance or date section header - skip
                        pass
                    else:
                        if not title:
                            title = prev
                    k -= 1

                if not title:
                    title = "Unknown Transaction"

                history.append({
                    "title": title,
                    "amount": amount,
                    "is_inbound": is_inbound,
                    "detail": detail,
                    "note": note,
                })

        return history
