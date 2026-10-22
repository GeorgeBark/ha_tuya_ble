"""Resolve configured lock credentials and scope their shared HA context."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import time
from typing import Any

from homeassistant.auth import EVENT_USER_ADDED, EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import util
from .lock_restore import latest_timestamp
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDevice


def format_unlock_record(record: dict[str, Any]) -> str:
    """Keep method and credential ID visible alongside an optional person name."""
    source = util.format_unlock_source(record["method"], record["user_id"])
    if name := record.get("name") or record.get("person_name"):
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
    person_entity_id: str | None
    person_name: str | None
    context: Context
    name: str | None = None

    @property
    def identity_data(self) -> dict[str, str]:
        if self.name is not None:
            return {"name": self.name}
        if self.person_entity_id is not None and self.person_name is not None:
            return {
                "person_entity_id": self.person_entity_id,
                "person_name": self.person_name,
            }
        return {}

    @property
    def record(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            **self.identity_data,
        }


class LockActivity:
    """Per-entry resolver; batch caching makes listener ordering irrelevant."""

    def __init__(
        self, hass: HomeAssistant, device: TuyaBLEDevice, mappings: list[dict[str, Any]]
    ) -> None:
        self.hass = hass
        self.device = device
        if device.product_id in util.RESTORE_LOCK_ACTIVITY:
            device._lock_activity_restored = False
        self.mappings: dict[tuple[str, int], dict[str, str]] = {}
        for item in mappings:
            if (
                not isinstance(item, dict)
                or item.get("method") not in dict(util.UNLOCK_METHODS.values())
                or type(item.get("user_id")) is not int
                or not 0 <= item["user_id"] <= 999
            ):
                continue
            person_id = item.get("person_entity_id")
            name = item.get("name")
            name = name.strip() if isinstance(name, str) else None
            if bool(person_id) == bool(name):
                continue
            if person_id:
                if not isinstance(person_id, str) or not person_id.startswith(
                    "person."
                ):
                    continue
                identity = {"person_entity_id": person_id}
            else:
                identity = {"name": name}
            self.mappings[item["method"], item["user_id"]] = identity
        self._users: dict[str, Any] = {}
        self._unsubscribers = []
        self._batch: list[TuyaBLEDataPoint] | None = None
        self._last_timestamp: float | None = None
        self.activities: dict[int, UnlockActivity] = {}
        self.latest_report: TuyaBLEDataPoint | None = None
        self.has_mapping = False
        self._writers: dict[str, Callable[[], None]] = {}
        self._publishing = False
        self._published_batch: list[TuyaBLEDataPoint] | None = None

    def register_writer(
        self, role: str, writer: Callable[[], None]
    ) -> Callable[[], None]:
        """Register only added entities; order does not depend on their listeners."""
        self._writers[role] = writer

        def unregister() -> None:
            if self._writers.get(role) is writer:
                self._writers.pop(role)

        return unregister

    def publish_ordered_update(self, role: str) -> bool:
        """Publish attributed activity lock-first exactly once per report batch."""
        if (
            self.latest_activity is None
            or self._publishing
            or role not in self._writers
        ):
            return False
        if self._published_batch is self._batch:
            return True
        self._published_batch = self._batch
        self._publishing = True
        try:
            for name in ("lock", "last_unlock", "unlocked"):
                if writer := self._writers.get(name):
                    writer()
        finally:
            self._publishing = False
        return True

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
        self._writers.clear()

    def prepare(self, updates: list[TuyaBLEDataPoint] | None) -> None:
        """Resolve each batch once, before any consumer changes last_unlock."""
        if not getattr(self.device, "_lock_activity_restored", True):
            self.activities = {}
            self.latest_report = None
            self.has_mapping = False
            return
        record = getattr(self.device, "last_unlock", None)
        self._last_timestamp = latest_timestamp(
            self._last_timestamp,
            getattr(self.device, "_lock_activity_watermark", None),
            record.get("timestamp") if record else None,
        )
        if updates is self._batch:
            return
        self._batch = updates
        self.activities = {}
        self.latest_report = None
        self.has_mapping = False
        if not updates:
            return
        last_timestamp = self._last_timestamp
        for dp in sorted(updates, key=lambda update: update.timestamp):
            if dp.id not in util.UNLOCK_METHODS:
                continue
            method = util.UNLOCK_METHODS[dp.id][0]
            identity = (
                self.mappings.get((method, dp.value), {})
                if type(dp.value) is int
                else {}
            )
            person_id = identity.get("person_entity_id")
            name = identity.get("name")
            person = self.hass.states.get(person_id) if person_id else None
            resolved = person is not None or name is not None
            self.has_mapping |= resolved
            # Unmapped BLE acknowledgements never update the lock's activity;
            # do not let them advance its replay watermark either.
            if dp.id == 19 and not resolved:
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
            if not resolved:
                continue
            delay_dp = self.device.datapoints[36]
            delay = int(delay_dp.value) if delay_dp else 5
            if not util.is_lock_event_recent(dp.timestamp, delay, time.time()):
                continue
            user_id = person.attributes.get("user_id") if person else None
            user = self._users.get(user_id)
            self.activities[dp.id] = UnlockActivity(
                method=method,
                user_id=dp.value,
                timestamp=dp.timestamp,
                person_entity_id=person_id,
                person_name=(
                    (person.attributes.get("friendly_name") or person_id)
                    if person
                    else None
                ),
                context=Context(user_id=user.id if user and user.is_active else None),
                name=name,
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
