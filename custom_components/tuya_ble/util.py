"""Utility methods for the Tuya integration."""

from __future__ import annotations

# Internal notification for aggregate unlock sensors, not an automation event.
LAST_UNLOCK_UPDATED = "tuya_ble_last_unlock_updated"
HOME_ASSISTANT_METHOD = "home_assistant"
UNLOCK_METHODS = {
    12: ("fingerprint", "Fingerprint"),
    13: ("password", "Password"),
    14: ("dynamic_password", "Dynamic password"),
    15: ("card", "Card"),
    19: ("ble", "BLE"),
    55: ("temporary_password", "Temporary password"),
    62: ("phone_remote", "Phone remote"),
}


def format_unlock_source(method: str, user_id: int | None) -> str:
    """Format a lock's stable method identifier and user ID for display."""
    if method == HOME_ASSISTANT_METHOD:
        return "Home Assistant"
    for identifier, label in UNLOCK_METHODS.values():
        if identifier == method:
            return f"{label} {user_id}"
    return f"{method} {user_id}"


def is_lock_event_recent(timestamp: float, auto_lock_time: int, now: float) -> bool:
    """Return whether a report is within the unlock interval plus delivery margin."""
    return timestamp >= now - (auto_lock_time + 10)


def remap_value(
    value: float | int,
    from_min: float | int = 0,
    from_max: float | int = 255,
    to_min: float | int = 0,
    to_max: float | int = 255,
    reverse: bool = False,
) -> float:
    """Remap a value from its current range, to a new range."""
    if reverse:
        value = from_max - value + from_min
    return ((value - from_min) / (from_max - from_min)) * (to_max - to_min) + to_min
