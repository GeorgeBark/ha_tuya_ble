"""Availability-independent restore data for opt-in lock activity entities."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from homeassistant.helpers.restore_state import ExtraStoredData


def latest_timestamp(*values: Any) -> float | None:
    """Find the newest valid timestamp, excluding booleans and nonfinite values."""
    timestamps = [
        value
        for value in values
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
    ]
    return max(timestamps) if timestamps else None


def restore_device_activity(
    device,
    record: dict[str, Any] | None,
    watermark: float | None,
    *,
    prefer_record: bool = False,
) -> None:
    """Merge independently restored entities without rolling back device history."""
    current = getattr(device, "last_unlock", None)
    previous = latest_timestamp(
        getattr(device, "_lock_activity_watermark", None),
        current.get("timestamp") if current else None,
    )
    incoming = latest_timestamp(watermark, record.get("timestamp") if record else None)
    device._lock_activity_watermark = latest_timestamp(previous, incoming)
    if record is not None and (
        current is None
        or (incoming is not None and (previous is None or incoming > previous))
        or (prefer_record and incoming == previous)
    ):
        device.last_unlock = record.copy()


@dataclass
class LockActivityExtraData(ExtraStoredData):
    """Only activity is durable: never a physical state, timer or HA Context."""

    watermark: float | None = None
    last_unlock: dict[str, Any] | None = None
    changed_by: str | None = None
    value: str | int | float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "watermark": self.watermark,
            "last_unlock": (
                self.last_unlock.copy() if self.last_unlock is not None else None
            ),
            "changed_by": self.changed_by,
            "value": self.value,
            "attributes": self.attributes.copy(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LockActivityExtraData | None:
        """Validate our own storage format; extra data never falls back to state."""
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        watermark = data.get("watermark")
        record = data.get("last_unlock")
        attributes = data.get("attributes", {})
        if watermark is not None and latest_timestamp(watermark) is None:
            return None
        if not isinstance(attributes, dict):
            return None
        if record is not None and (
            not isinstance(record, dict)
            or not isinstance(record.get("method"), str)
            or (
                record.get("timestamp") is not None
                and latest_timestamp(record["timestamp"]) is None
            )
        ):
            return None
        # Identity fields are opaque scalar metadata, shared with the activity PR.
        for values in (attributes, record or {}):
            if any(
                not isinstance(key, str)
                or (value is not None and type(value) not in (str, int, float, bool))
                or (type(value) is float and not math.isfinite(value))
                for key, value in values.items()
            ):
                return None
        if data.get("changed_by") is not None and not isinstance(
            data["changed_by"], str
        ):
            return None
        value = data.get("value")
        if value is not None and type(value) not in (str, int, float):
            return None
        if type(value) is float and not math.isfinite(value):
            return None
        return cls(
            watermark,
            record.copy() if record is not None else None,
            data.get("changed_by"),
            value,
            attributes.copy(),
        )
