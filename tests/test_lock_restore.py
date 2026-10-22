"""Durable activity snapshots remain intact while entities are unavailable."""

import json
from unittest.mock import AsyncMock, patch

from homeassistant.core import State
from homeassistant.helpers.restore_state import StoredState, async_get
from homeassistant.util import dt as dt_util
import orjson
import pytest

from custom_components.tuya_ble.lock_restore import (
    LockActivityExtraData,
    latest_timestamp,
    restore_device_activity,
)
from .test_lock_capabilities import make_entities, report


@pytest.mark.parametrize("restart", [False, True], ids=["reload", "restart"])
@pytest.mark.parametrize("ha_command", [False, True])
async def test_unavailable_snapshot_rejects_full_history(
    hass, make_entities, restart, ha_command
):
    device, coordinator, lock, sensors, _ = await make_entities("dappsdq4")
    last = next(s for s in sensors.values() if s._mapping.last_unlock)
    alarm = sensors[21]
    entities = [lock, last, alarm]
    report(device, coordinator, entities, 15, 3, timestamp=990)
    report(device, coordinator, entities, 21, 1, timestamp=990)
    if ha_command:
        await lock.async_unlock()
        last._mapping.getter(last)
    expected = (lock.changed_by, last.native_value, alarm.native_value)
    saved = {}
    for index, entity in enumerate(entities):
        entity.entity_id = f"{'lock' if index == 0 else 'sensor'}.restore_{index}"
        entity.hass = hass
        snapshot = StoredState(
            State(entity.entity_id, "unavailable"),
            entity.extra_restore_state_data,
            dt_util.utcnow(),
        )
        if restart:
            snapshot = StoredState.from_dict(
                json.loads(orjson.dumps(snapshot.as_dict()))
            )
        saved[entity.entity_id] = snapshot
    # A new device and new entities simulate an entry reload as well as startup.
    device, coordinator, lock, sensors, _ = await make_entities("dappsdq4")
    last = next(s for s in sensors.values() if s._mapping.last_unlock)
    alarm = sensors[21]
    entities = [lock, last, alarm]
    async_get(hass).last_states.update(saved)
    for index, entity in enumerate(entities):
        entity.entity_id = f"{'lock' if index == 0 else 'sensor'}.restore_{index}"
        entity.hass = hass
        with patch(
            "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await entity.async_added_to_hass()
    assert device._lock_activity_watermark == 990
    assert (lock.changed_by, last.native_value, alarm.native_value) == expected
    for timestamp in range(900, 970):
        report(
            device,
            coordinator,
            entities,
            [12, 13, 15][timestamp % 3],
            timestamp % 4,
            timestamp=timestamp,
        )
        report(device, coordinator, entities, 21, timestamp % 2, timestamp=timestamp)
        assert (lock.changed_by, last.native_value, alarm.native_value) == expected
    # Saving unavailable again must retain the same activity and watermarks.
    assert (
        lock.extra_restore_state_data.as_dict()
        == saved[lock.entity_id].extra_data.as_dict()
    )
    assert (
        alarm.extra_restore_state_data.as_dict()
        == saved[alarm.entity_id].extra_data.as_dict()
    )
    report(device, coordinator, entities, 12, 7, timestamp=1000)
    report(device, coordinator, entities, 21, 0, timestamp=1000)
    assert lock.changed_by == last.native_value == "Fingerprint 7"
    assert alarm.native_value != expected[2]
    assert (
        device._lock_activity_watermark == alarm._lock_activity_last_timestamp == 1000
    )


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "123"])
def test_invalid_watermarks(value):
    assert latest_timestamp(value) is None
    assert LockActivityExtraData.from_dict({"version": 1, "watermark": value}) is None


@pytest.mark.parametrize("reverse", [False, True])
async def test_independent_restore_watermarks_never_roll_back(make_entities, reverse):
    device, _, _, _, _ = await make_entities("dappsdq4")
    records = [
        ({"method": "card", "user_id": 3, "timestamp": 900}, 900),
        ({"method": "home_assistant", "user_id": None, "timestamp": None}, 990),
    ]
    for record, watermark in reversed(records) if reverse else records:
        restore_device_activity(device, record, watermark)
    assert device._lock_activity_watermark == 990
    assert device.last_unlock == records[1][0]


async def test_extra_data_restored_before_listeners_without_reading_state(
    hass, make_entities
):
    device, _, lock, sensors, _ = await make_entities("dappsdq4")
    last = next(s for s in sensors.values() if s._mapping.last_unlock)
    record = {"method": "card", "user_id": 3, "timestamp": 990}
    lock.async_get_last_extra_data = AsyncMock(
        return_value=LockActivityExtraData(
            watermark=990, last_unlock=record, changed_by="Card 3"
        )
    )
    last.async_get_last_extra_data = AsyncMock(
        return_value=LockActivityExtraData(
            watermark=990, last_unlock=record, value="Card 3", attributes=record
        )
    )

    async def added(entity):
        assert device._lock_activity_watermark == 990
        assert device.last_unlock == record
        if entity is lock:
            assert lock.changed_by == "Card 3"

    for entity in (lock, last):
        entity.async_get_last_state = AsyncMock(
            side_effect=AssertionError("Extra data must take precedence")
        )
        with patch(
            "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
            new=added,
        ):
            await entity.async_added_to_hass()
