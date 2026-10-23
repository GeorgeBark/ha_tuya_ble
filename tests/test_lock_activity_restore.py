"""Identity and resolver replay protection survive unavailable restore data."""

import json
from unittest.mock import AsyncMock, patch

from homeassistant.core import State
from homeassistant.helpers.restore_state import StoredState, async_get
from homeassistant.util import dt as dt_util
import orjson
import pytest

from custom_components.tuya_ble.lock import EVENT_LOCK
from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType
from .test_lock_capabilities import make_entities, report
from .test_lock_user_names import named_mapping
from .test_lock_users import mapped_lock, mapping


@pytest.mark.parametrize("restart", [False, True], ids=["reload", "restart"])
@pytest.mark.parametrize("named", [False, True], ids=["person", "name"])
@pytest.mark.parametrize("ha_command", [False, True])
async def test_unavailable_identity_and_resolver_watermark(
    hass, mapped_lock, restart, named, ha_command
):
    mappings = [named_mapping() if named else mapping("card", 3)]
    env = await mapped_lock(mappings)
    report(env.device, env.coordinator, env.entities, 15, 3)
    report(env.device, env.coordinator, [env.sensors[21]], 21, 1)
    if ha_command:
        await env.lock.async_unlock()
    await hass.async_block_till_done()
    expected = (env.lock.changed_by, env.last.native_value)
    identity = (
        {"name": "Emergency Fob"}
        if named
        else {"person_entity_id": "person.jorge", "person_name": "Jorge"}
    )
    restored_ids = [entity.entity_id for entity in (env.lock, env.last)]
    for entity in (env.lock, env.last):
        saved = StoredState(
            State(entity.entity_id, "unavailable"),
            entity.extra_restore_state_data,
            dt_util.utcnow(),
        )
        if restart:
            saved = StoredState.from_dict(json.loads(orjson.dumps(saved.as_dict())))
        async_get(hass).last_states[entity.entity_id] = saved
    env = await mapped_lock(mappings)
    for entity, entity_id in zip((env.lock, env.last), restored_ids):
        entity.entity_id = entity_id
        await entity.async_will_remove_from_hass()
        with patch(
            "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await entity.async_added_to_hass()
    await hass.async_block_till_done()
    assert (env.lock.changed_by, env.last.native_value) == expected
    assert env.device._lock_activity_watermark == env.resolver._last_timestamp == 1000
    assert {
        key: env.lock.extra_state_attributes[f"last_unlock_{key}"] for key in identity
    } == identity
    if not ha_command:
        assert {key: env.device.last_unlock[key] for key in identity} == identity
        assert {
            key: env.last.extra_state_attributes[key] for key in identity
        } == identity
    bus = []
    hass.bus.async_listen(EVENT_LOCK, bus.append)
    previous_event = env.event.state
    for timestamp in range(900, 970):
        report(
            env.device,
            env.coordinator,
            list(reversed(env.entities)),
            [12, 13, 15][timestamp % 3],
            timestamp % 4,
            timestamp=timestamp,
        )
        assert (env.lock.changed_by, env.last.native_value) == expected
        assert env.resolver.latest_report is None
    await hass.async_block_till_done()
    assert not bus
    assert env.event.state == previous_event
    report(
        env.device, env.coordinator, list(reversed(env.entities)), 15, 3, timestamp=1001
    )
    await hass.async_block_till_done()
    assert (
        env.lock.changed_by
        == env.last.native_value
        == ("Emergency Fob · Card 3" if named else "Jorge · Card 3")
    )
    assert len(bus) == 1
    assert env.device._lock_activity_watermark == env.resolver._last_timestamp == 1001
    states = [hass.states.get(entity.entity_id) for entity in env.entities]
    assert all(state.context is states[0].context for state in states)
    assert states[0].context.user_id == (None if named else env.user.id)


async def test_reports_during_restore_wait_for_durable_watermark(hass, mapped_lock):
    env = await mapped_lock([named_mapping()])
    report(env.device, env.coordinator, env.entities, 15, 3)
    saved = env.lock.extra_restore_state_data
    bus = []
    hass.bus.async_listen(EVENT_LOCK, bus.append)
    await hass.async_block_till_done()
    bus.clear()

    async def load_extra():
        report(
            env.device,
            env.coordinator,
            list(reversed(env.entities)),
            15,
            3,
            timestamp=999,
        )
        env.resolver.prepare(env.coordinator.last_updates)
        assert env.resolver.latest_report is None
        return saved

    env.lock.async_get_last_extra_data = load_extra
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await env.lock.async_added_to_hass()
    # Reconsider the same batch after restoring: it must be rejected, not cached
    # as an accepted pre-restore activity or attributed event.
    for entity in reversed(env.entities):
        entity._handle_coordinator_update()
    await hass.async_block_till_done()
    assert not bus
    assert env.resolver._last_timestamp == 1000
    assert env.lock.changed_by == env.last.native_value == "Emergency Fob · Card 3"


@pytest.mark.parametrize("named", [False, True], ids=["person", "name"])
@pytest.mark.parametrize(
    "dp_id,method,label", [(12, "fingerprint", "Fingerprint"), (19, "ble", "BLE")]
)
async def test_accepted_hour_late_report_keeps_identity_without_recent_effects(
    hass, mapped_lock, named, dp_id, method, label
):
    identity = (
        {"name": "Emergency Fob"}
        if named
        else {"person_entity_id": "person.jorge", "person_name": "Jorge"}
    )
    configured = {
        "method": method,
        "user_id": 1,
        **(
            {"name": "Emergency Fob"} if named else {"person_entity_id": "person.jorge"}
        ),
    }
    env = await mapped_lock([configured])
    report(env.device, env.coordinator, env.entities, 15, 3, timestamp=1000)
    env.lock._lock_activity_relock(None)
    await hass.async_block_till_done()
    previous_event = env.event.state
    bus = []
    hass.bus.async_listen(EVENT_LOCK, bus.append)
    env.device.datapoints._update_from_device(
        dp_id, 1001, 0, TuyaBLEDataPointType.DT_VALUE, 1
    )
    env.coordinator.last_updates = [env.device.datapoints[dp_id]]
    with (
        patch("time.time", return_value=5002),
        patch("time.monotonic", return_value=500),
    ):
        for entity in reversed(env.entities):
            entity._handle_coordinator_update()
    await hass.async_block_till_done()
    assert (
        env.lock.changed_by
        == env.last.native_value
        == f"{'Emergency Fob' if named else 'Jorge'} · {label} 1"
    )
    assert env.device.last_unlock == {
        "method": method,
        "user_id": 1,
        "timestamp": 1001,
        **identity,
    }
    assert env.last.extra_state_attributes == env.device.last_unlock
    assert env.lock.extra_state_attributes == {
        "last_unlock_method": method,
        "last_unlock_user_id": 1,
        "last_unlock_time": 1001,
        **{f"last_unlock_{key}": value for key, value in identity.items()},
    }
    assert env.device._lock_activity_watermark == 1001
    assert not env.resolver.activities
    assert not bus
    assert env.event.state == previous_event
    assert env.lock._lock_activity_unlock_until == 0
    assert env.lock._lock_activity_relock_cancel is None
    assert env.lock.is_locked
    assert all(
        hass.states.get(entity.entity_id).context.user_id is None
        for entity in (env.lock, env.last)
    )
