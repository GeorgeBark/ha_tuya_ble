"""Resolve configured lock credentials and scope their shared HA context."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import time
from typing import Any

from homeassistant.auth import EVENT_USER_ADDED, EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import util
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDevice


def format_unlock_record(record: dict[str, Any]) -> str:
    """Keep method and credential ID visible alongside an optional person name."""
    source = util.format_unlock_source(record["method"], record["user_id"])
    if name := record.get("person_name"):
        return f"{name} · {source}"
    return source


@contextmanager
def scoped_activity_context(entity: Entity, context: Context | None) -> Iterator[None]:
    """Attribute only this write, not the next relock/availability update."""
    if context is None:
        yield
        return
    entity.async_set_context(context)
    try:
        yield
    finally:
        entity.async_set_context(Context())


@dataclass(frozen=True)
class UnlockActivity:
    """An immutable identity/context snapshot for one accepted device report."""

    method: str
    user_id: int
    timestamp: float
    person_entity_id: str
    person_name: str
    context: Context

    @property
    def person_data(self) -> dict[str, str]:
        return {
            "person_entity_id": self.person_entity_id,
            "person_name": self.person_name,
        }

    @property
    def record(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            **self.person_data,
        }


class LockActivity:
    """Per-entry resolver; batch caching makes listener ordering irrelevant."""

    def __init__(
        self, hass: HomeAssistant, device: TuyaBLEDevice, mappings: list[dict[str, Any]]
    ) -> None:
        self.hass = hass
        self.device = device
        self.mappings = {
            (item["method"], item["user_id"]): item["person_entity_id"]
            for item in mappings
            if isinstance(item, dict)
            and item.get("method") in dict(util.UNLOCK_METHODS.values())
            and type(item.get("user_id")) is int
            and 0 <= item["user_id"] <= 999
            and isinstance(item.get("person_entity_id"), str)
            and item["person_entity_id"].startswith("person.")
        }
        self._users: dict[str, Any] = {}
        self._unsubscribers = []
        self._batch: list[TuyaBLEDataPoint] | None = None
        self._last_timestamp: float | None = None
        self.activities: dict[int, UnlockActivity] = {}
        self.latest_report: TuyaBLEDataPoint | None = None
        self.has_mapping = False

    async def async_setup(self) -> None:
        """Preload accounts; callbacks must never await while processing reports."""
        await self._async_refresh_users()
        self._unsubscribers = [
            self.hass.bus.async_listen(EVENT_USER_ADDED, self._async_refresh_users),
            self.hass.bus.async_listen(EVENT_USER_UPDATED, self._async_refresh_users),
            self.hass.bus.async_listen(EVENT_USER_REMOVED, self._handle_user_removed),
        ]

    async def _async_refresh_users(self, event: Event | None = None) -> None:
        self._users = {user.id: user for user in await self.hass.auth.async_get_users()}

    @callback
    def _handle_user_removed(self, event: Event) -> None:
        self._users.pop(event.data["user_id"], None)

    @callback
    def async_close(self) -> None:
        """Remove entry-scoped auth subscriptions on unload."""
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def prepare(self, updates: list[TuyaBLEDataPoint] | None) -> None:
        """Resolve each batch once, before any consumer changes last_unlock."""
        if updates is self._batch:
            return
        self._batch = updates
        self.activities = {}
        self.latest_report = None
        self.has_mapping = False
        if not updates:
            return
        last_timestamp = self._last_timestamp
        if record := getattr(self.device, "last_unlock", None):
            restored_time = record.get("timestamp")
            if isinstance(restored_time, (int, float)):
                last_timestamp = max(last_timestamp or restored_time, restored_time)
        for dp in sorted(updates, key=lambda update: update.timestamp):
            if dp.id not in util.UNLOCK_METHODS:
                continue
            method = util.UNLOCK_METHODS[dp.id][0]
            person_id = (
                self.mappings.get((method, dp.value)) if type(dp.value) is int else None
            )
            person = self.hass.states.get(person_id) if person_id else None
            self.has_mapping |= person is not None
            # Unmapped BLE acknowledgements never update the lock's activity;
            # do not let them advance its replay watermark either.
            if dp.id == 19 and person is None:
                continue
            if dp.id == 19 and time.monotonic() < getattr(
                self.device, "_lock_activity_pending_until", 0.0
            ):
                continue
            if (
                self.device.product_id in util.TIMESTAMPED_UNLOCK_REPORTS
                and last_timestamp is not None
                and dp.timestamp <= last_timestamp
            ):
                continue
            last_timestamp = dp.timestamp
            self._last_timestamp = last_timestamp
            self.latest_report = dp
            if person is None:
                continue
            delay_dp = self.device.datapoints[36]
            delay = int(delay_dp.value) if delay_dp else 5
            if not util.is_lock_event_recent(dp.timestamp, delay, time.time()):
                continue
            user_id = person.attributes.get("user_id")
            user = self._users.get(user_id)
            self.activities[dp.id] = UnlockActivity(
                method=method,
                user_id=dp.value,
                timestamp=dp.timestamp,
                person_entity_id=person_id,
                person_name=person.attributes.get("friendly_name") or person_id,
                context=Context(user_id=user.id if user and user.is_active else None),
            )

    @property
    def latest_activity(self) -> UnlockActivity | None:
        if self.latest_report is not None:
            return self.activities.get(self.latest_report.id)
        return None


def get_lock_activity(coordinator: DataUpdateCoordinator) -> LockActivity | None:
    """Get the optional resolver, without affecting ordinary coordinators."""
    activity = getattr(coordinator, "lock_activity", None)
    if isinstance(activity, LockActivity):
        activity.prepare(coordinator.last_updates)
        return activity
    return None
