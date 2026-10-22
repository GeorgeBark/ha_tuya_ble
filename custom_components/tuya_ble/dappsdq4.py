"""Shared helpers for the dappsdq4 smart lock."""

from __future__ import annotations

from typing import Final

DAPP_SDQ4_LAST_UNLOCK_UPDATED: Final = "tuya_ble_dappsdq4_last_unlock_updated"

DAPP_SDQ4_UNLOCK_METHODS: Final = {
    12: ("fingerprint", "Fingerprint"),
    13: ("password", "Password"),
    14: ("dynamic_password", "Dynamic password"),
    15: ("card", "Card"),
    19: ("ble", "BLE"),
    55: ("temporary_password", "Temporary password"),
    62: ("phone_remote", "Phone remote"),
}
DAPP_SDQ4_HOME_ASSISTANT_METHOD: Final = "home_assistant"


def format_dappsdq4_unlock(method: str, user_id: int | None) -> str:
    """Format a dappsdq4 unlock source consistently across entities."""
    if method == DAPP_SDQ4_HOME_ASSISTANT_METHOD:
        return "Home Assistant"
    for identifier, label in DAPP_SDQ4_UNLOCK_METHODS.values():
        if identifier == method:
            return f"{label} {user_id}"
    return f"{method} {user_id}"


def is_dappsdq4_event_recent(timestamp: float, auto_lock_time: int, now: float) -> bool:
    """Return whether a lock event is fresh enough to represent a new action."""
    return timestamp >= now - (auto_lock_time + 10)
