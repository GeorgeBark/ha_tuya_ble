"""Per-entry people mappings and single-write context attribution regressions."""

from itertools import permutations
import json
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.lock import LockState
from homeassistant.core import Context, State
from homeassistant.helpers.entity import Entity
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_ble import (
    _async_update_listener,
    async_setup_entry,
    lock as lock_platform,
    sensor,
    util,
)
from custom_components.tuya_ble.config_flow import TuyaBLEOptionsFlow
from custom_components.tuya_ble.const import (
    CONF_CATEGORY,
    CONF_LOCK_USERS,
    CONF_PRODUCT_ID,
    DOMAIN,
)
from custom_components.tuya_ble.lock import EVENT_LOCK
from custom_components.tuya_ble.lock_activity import LockActivity
from .test_dappsdq4 import CHECK_VALUE
from .test_lock_capabilities import make_entities, report


def mapping(method="fingerprint", user_id=1, person="person.jorge"):
    return {"method": method, "user_id": user_id, "person_entity_id": person}


@pytest.fixture
async def mapped_lock(hass, make_entities, monkeypatch):
    """Use genuine HA state writes and users, not just mocked context setters."""
    monkeypatch.setattr("homeassistant.helpers.entity.timer", lambda: time.time())
    monkeypatch.setattr(
        lock_platform, "async_call_later", Mock(side_effect=lambda *args: Mock())
    )
    user = await hass.auth.async_create_user("Account Jorge")
    other = await hass.auth.async_create_user("Account Ana")
    hass.states.async_set(
        "person.jorge", "home", {"friendly_name": "Jorge", "user_id": user.id}
    )
    hass.states.async_set(
        "person.ana", "not_home", {"friendly_name": "Ana", "user_id": other.id}
    )
    hass.states.async_set("person.guest", "unknown", {"friendly_name": "Guest"})
    resolvers = []

    async def make(mappings=None, product_id="dappsdq4", category="jtmspro"):
        device, coordinator, lock, sensors, events = await make_entities(
            product_id, category
        )
        mappings = [mapping()] if mappings is None else mappings
        resolver = None
        if (
            mappings
            and product_id in util.UNLOCK_ATTRIBUTION
            and category in util.LOCK_CATEGORIES
        ):
            resolver = coordinator.lock_activity = LockActivity(hass, device, mappings)
            await resolver.async_setup()
            resolvers.append(resolver)
        coordinator._disconnected = False
        last = sensors.get(sensor.LAST_UNLOCK_DP_ID)
        unlock = next(
            (event for event in events if event.entity_description.key == "unlocked"),
            None,
        )
        entities = [entity for entity in (lock, unlock, last) if entity is not None]
        for entity in entities:
            entity.hass = hass
            entity.platform = SimpleNamespace(
                domain=entity.platform.value,
                platform_name=DOMAIN,
                component_translations={},
                platform_translations={},
                object_id_platform_translations={},
                default_language_platform_translations={},
            )
            entity._verified_state_writable = True
            entity.async_write_ha_state = Mock(
                wraps=Entity.async_write_ha_state.__get__(entity)
            )
        if last:
            last.async_get_last_state = AsyncMock(return_value=None)
            lock.async_get_last_state = AsyncMock(return_value=None)
            with patch(
                "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
                new=AsyncMock(),
            ):
                await lock.async_added_to_hass()
                await unlock.async_added_to_hass()
                await last.async_added_to_hass()
        report(device, coordinator, entities, 33, True)
        return SimpleNamespace(
            device=device,
            coordinator=coordinator,
            lock=lock,
            last=last,
            event=unlock,
            entities=entities,
            resolver=resolver,
            user=user,
            other=other,
            sensors=sensors,
        )

    yield make
    for resolver in resolvers:
        resolver.async_close()


@pytest.mark.parametrize("order", list(permutations(("lock", "event", "last"))))
@pytest.mark.parametrize("open_state", [False, True])
async def test_same_person_context_for_all_three_rows_in_any_listener_order(
    hass, mapped_lock, order, open_state
):
    env = await mapped_lock()
    env.lock._report_momentary_releases_as_open = open_state
    changes, bus_events, manual_log = [], [], []
    hass.bus.async_listen("state_changed", changes.append)
    hass.bus.async_listen(EVENT_LOCK, bus_events.append)
    hass.bus.async_listen("logbook_entry", manual_log.append)
    await hass.async_block_till_done()
    changes.clear()
    report(env.device, env.coordinator, [getattr(env, name) for name in order], 12, 1)
    await hass.async_block_till_done()
    states = [hass.states.get(entity.entity_id) for entity in env.entities]
    assert all(state.context.user_id == env.user.id for state in states), [
        (state.entity_id, state.state, state.context.user_id, dict(state.attributes))
        for state in states
    ]
    assert states[0].context is states[1].context is states[2].context
    assert states[0].state == (LockState.OPEN if open_state else LockState.UNLOCKED)
    assert env.lock.changed_by == env.last.native_value == "Jorge · Fingerprint 1"
    assert env.event.state_attributes["person_name"] == "Jorge"
    assert env.last.extra_state_attributes["person_entity_id"] == "person.jorge"
    assert len(bus_events) == 1
    assert bus_events[0].context is states[0].context
    assert bus_events[0].data == {
        "device_id": env.device.device_id,
        "entity_id": env.lock.entity_id,
        "method": "Fingerprint",
        "user_id": 1,
        "person_entity_id": "person.jorge",
        "person_name": "Jorge",
    }
    assert not manual_log
    assert [
        event.data["entity_id"]
        for event in changes
        if event.data["entity_id"] in {entity.entity_id for entity in env.entities}
    ] == [env.lock.entity_id, env.last.entity_id, env.event.entity_id]
    for entity in env.entities:
        assert (
            len(
                [
                    event
                    for event in changes
                    if event.data["entity_id"] == entity.entity_id
                ]
            )
            == 1
        )
        assert entity._context.user_id is None


@pytest.mark.parametrize(
    "person,expected_name,with_user",
    [
        ("person.guest", "Guest", False),
        ("person.missing", None, False),
    ],
)
async def test_person_without_account_or_deleted_reference(
    hass, mapped_lock, person, expected_name, with_user
):
    env = await mapped_lock([mapping(person=person)])
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    assert env.lock.changed_by == (
        f"{expected_name} · Fingerprint 1" if expected_name else "Fingerprint 1"
    )
    assert all(
        hass.states.get(entity.entity_id).context.user_id is None
        for entity in env.entities
    )


async def test_renames_relinks_and_deleted_users_resolve_current_person(
    hass, mapped_lock
):
    env = await mapped_lock()
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    hass.states.async_set(
        "person.jorge", "home", {"friendly_name": "New name", "user_id": env.other.id}
    )
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=1001)
    await hass.async_block_till_done()
    assert env.last.native_value == "New name · Fingerprint 1"
    assert hass.states.get(env.lock.entity_id).context.user_id == env.other.id
    await hass.auth.async_remove_user(env.other)
    await hass.async_block_till_done()
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=1002)
    await hass.async_block_till_done()
    assert env.lock.changed_by == "New name · Fingerprint 1"
    assert hass.states.get(env.last.entity_id).context.user_id is None
    hass.states.async_remove("person.jorge")
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=1003)
    await hass.async_block_till_done()
    assert env.lock.changed_by == "Fingerprint 1"
    assert "person_name" not in env.device.last_unlock


async def test_same_id_for_fingerprint_password_and_separate_entries(hass, mapped_lock):
    first = await mapped_lock([mapping(), mapping("password", person="person.ana")])
    second = await mapped_lock([mapping(person="person.guest")])
    # Give the second simulated entry distinct state IDs as real devices have.
    for entity in second.entities:
        entity.entity_id += "_second"
    report(first.device, first.coordinator, first.entities, 12, 1)
    report(second.device, second.coordinator, second.entities, 12, 1)
    await hass.async_block_till_done()
    assert first.lock.changed_by == "Jorge · Fingerprint 1"
    assert second.lock.changed_by == "Guest · Fingerprint 1"
    report(first.device, first.coordinator, first.entities, 13, 1, timestamp=1001)
    await hass.async_block_till_done()
    assert first.lock.changed_by == "Ana · Password 1"
    assert hass.states.get(first.event.entity_id).context.user_id == first.other.id


@pytest.mark.parametrize("relock", ["timer", "dp33"])
async def test_relock_and_later_availability_never_inherit_local_person(
    hass, mapped_lock, relock
):
    env = await mapped_lock()
    # Keep well within HA's five-second entity-context TTL.
    report(env.device, env.coordinator, env.entities, 36, 1)
    report(env.device, env.coordinator, env.entities, 33, relock == "timer")
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    if relock == "timer":
        env.lock._lock_activity_relock_cancel()
        env.lock._lock_activity_relock(None)
    else:
        report(env.device, env.coordinator, env.entities, 33, True)
    assert hass.states.get(env.lock.entity_id).context.user_id is None
    env.coordinator.last_updates = None
    env.coordinator._disconnected = True
    for entity in env.entities:
        entity._handle_coordinator_update()
        assert hass.states.get(entity.entity_id).context.user_id is None


@pytest.mark.parametrize("age", [0, 1, 40])
async def test_replayed_reports_do_not_reassign_person_context(hass, mapped_lock, age):
    env = await mapped_lock()
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    event_before = env.event.state
    record_before = env.device.last_unlock.copy()
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=1000 - age)
    await hass.async_block_till_done()
    assert env.event.state == event_before
    assert env.device.last_unlock == record_before
    assert env.lock._context.user_id is None


@pytest.mark.parametrize("command", ["async_open", "async_unlock"])
async def test_ha_commands_and_dp19_keep_real_service_user(hass, mapped_lock, command):
    env = await mapped_lock([mapping(), mapping("ble", 0)])
    env.device._device_info.ble_unlock_check = CHECK_VALUE
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    service_context = Context(user_id=env.other.id)
    env.lock.async_set_context(service_context)
    with patch("time.monotonic", return_value=500):
        await getattr(env.lock, command)()
    await hass.async_block_till_done()
    assert hass.states.get(env.lock.entity_id).context.user_id == env.other.id
    assert hass.states.get(env.last.entity_id).context is service_context
    event_before = env.event.state
    report(env.device, env.coordinator, env.entities, 19, 0, timestamp=1001)
    await hass.async_block_till_done()
    assert env.event.state == event_before
    assert env.last.native_value == "Home Assistant"
    assert env.lock.changed_by == "Home Assistant"
    env.lock._lock_activity_clear_pending()
    report(env.device, env.coordinator, env.entities, 19, 0, timestamp=1002)
    await hass.async_block_till_done()
    assert env.lock.changed_by == env.last.native_value == "Jorge · BLE 0"
    assert hass.states.get(env.event.entity_id).context.user_id == env.user.id


@pytest.mark.parametrize("order", [(12, 13), (13, 12)])
async def test_batch_and_queued_notifications_use_newest_identity(
    hass, mapped_lock, order
):
    env = await mapped_lock([mapping(), mapping("password", person="person.ana")])
    for dp_id, timestamp in ((12, 1000), (13, 1001)):
        env.device.datapoints._update_from_device(
            dp_id, timestamp, 0, sensor.TuyaBLEDataPointType.DT_VALUE, 1
        )
    env.coordinator.last_updates = [env.device.datapoints[dp_id] for dp_id in order]
    with (
        patch("time.time", return_value=1001),
        patch("time.monotonic", return_value=500),
    ):
        for entity in env.entities:
            entity._handle_coordinator_update()
    await hass.async_block_till_done()
    assert env.last.native_value == env.lock.changed_by == "Ana · Password 1"
    assert env.event.state_attributes["method"] == "password"
    assert all(
        hass.states.get(entity.entity_id).context.user_id == env.other.id
        for entity in env.entities
    )


async def test_unmapped_credentials_and_empty_mappings_keep_original_data(
    hass, mapped_lock
):
    for mappings in ([], [mapping("password")]):
        env = await mapped_lock(mappings)
        report(env.device, env.coordinator, env.entities, 12, 1)
        await hass.async_block_till_done()
        assert env.lock.changed_by == env.last.native_value == "Fingerprint 1"
        assert env.device.last_unlock == {
            "method": "fingerprint",
            "user_id": 1,
            "timestamp": 1000,
        }
        assert env.event.state_attributes == {
            "event_type": "fingerprint",
            "method": "fingerprint",
            "user_id": 1,
        }


@pytest.mark.parametrize("dp_id", list(util.UNLOCK_METHODS))
async def test_each_method_can_resolve_person_and_preserve_numeric_id(
    hass, mapped_lock, dp_id
):
    method = util.UNLOCK_METHODS[dp_id][0]
    env = await mapped_lock([mapping(method, 999)])
    report(env.device, env.coordinator, env.entities, dp_id, 999)
    await hass.async_block_till_done()
    assert env.event.state_attributes["method"] == method
    assert env.event.state_attributes["user_id"] == 999
    assert env.event.state_attributes["person_name"] == "Jorge"
    assert env.device.last_unlock["user_id"] == 999
    contexts = [hass.states.get(entity.entity_id).context for entity in env.entities]
    assert contexts[0] is contexts[1] is contexts[2]


async def test_account_created_after_entry_setup_is_recognized(hass, mapped_lock):
    env = await mapped_lock()
    new_user = await hass.auth.async_create_user("New account")
    hass.states.async_set(
        "person.jorge", "home", {"friendly_name": "Jorge", "user_id": new_user.id}
    )
    await hass.async_block_till_done()
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    assert hass.states.get(env.event.entity_id).context.user_id == new_user.id
    env.resolver.async_close()
    assert not env.resolver._unsubscribers


async def test_disabled_user_is_not_used_for_context(hass, mapped_lock):
    env = await mapped_lock([mapping(person="person.ana")])
    await hass.auth.async_update_user(env.other, is_active=False)
    await hass.async_block_till_done()
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    assert env.lock.changed_by == "Ana · Fingerprint 1"
    assert all(
        hass.states.get(entity.entity_id).context.user_id is None
        for entity in env.entities
    )


async def test_restore_keeps_person_activity_but_not_context_or_unlock_window(
    hass, mapped_lock
):
    from homeassistant.components.event import EventExtraStoredData
    from homeassistant.helpers.restore_state import RestoreEntity

    env = await mapped_lock()
    before = {
        "changed_by": "Jorge · Fingerprint 1",
        "last_unlock_method": "fingerprint",
        "last_unlock_user_id": 1,
        "last_unlock_time": 999,
        "last_unlock_person_entity_id": "person.jorge",
        "last_unlock_person_name": "Jorge",
    }
    env.lock.async_get_last_state = AsyncMock(
        return_value=State(
            env.lock.entity_id, "open", before, context=Context(user_id=env.user.id)
        )
    )
    last_record = {
        "method": "fingerprint",
        "user_id": 1,
        "timestamp": 999,
        "person_entity_id": "person.jorge",
        "person_name": "Jorge",
    }
    env.last.async_get_last_state = AsyncMock(
        return_value=State(env.last.entity_id, "Jorge · Fingerprint 1", last_record)
    )
    await env.last.async_will_remove_from_hass()
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await env.lock.async_added_to_hass()
        await env.last.async_added_to_hass()
    event_time = "2026-10-23T10:00:00.000+00:00"
    env.event.async_get_last_state = AsyncMock(
        return_value=State(
            env.event.entity_id, event_time, context=Context(user_id=env.user.id)
        )
    )
    env.event.async_get_last_event_data = AsyncMock(
        return_value=EventExtraStoredData(
            "fingerprint",
            {
                "method": "fingerprint",
                "user_id": 1,
                "person_entity_id": "person.jorge",
                "person_name": "Jorge",
            },
        )
    )
    with patch.object(RestoreEntity, "async_internal_added_to_hass", new=AsyncMock()):
        await env.event.async_internal_added_to_hass()
    assert env.lock.changed_by == env.last.native_value == "Jorge · Fingerprint 1"
    assert env.device.last_unlock == last_record
    assert env.lock.state == LockState.LOCKED
    assert env.lock._lock_activity_relock_cancel is None
    assert env.lock._context is None or env.lock._context.user_id is None
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=998)
    await hass.async_block_till_done()
    assert env.lock.changed_by == "Jorge · Fingerprint 1"
    assert env.event.state == event_time
    assert env.event.state_attributes["person_name"] == "Jorge"
    assert env.event._context is None or env.event._context.user_id is None
    assert env.last.extra_state_attributes == last_record


async def test_passage_release_updates_activity_without_a_second_lock_transition(
    hass, mapped_lock
):
    env = await mapped_lock()
    await env.lock.async_unlock()
    # Publish the mode before observing the fingerprint, as its device report does.
    report(env.device, env.coordinator, env.entities, 33, False)
    before = hass.states.get(env.lock.entity_id).last_changed
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    assert hass.states.get(env.lock.entity_id).state == LockState.UNLOCKED
    assert hass.states.get(env.lock.entity_id).last_changed == before
    assert env.last.native_value == "Jorge · Fingerprint 1"
    assert hass.states.get(env.event.entity_id).context.user_id == env.user.id


@pytest.mark.parametrize(
    "product_id,category", [("djrqe0q6", "jtmspro"), ("0qxp5u7s", "ms")]
)
async def test_unsupported_locks_do_not_acquire_person_mappings(
    hass, mapped_lock, product_id, category
):
    env = await mapped_lock(product_id=product_id, category=category)
    report(env.device, env.coordinator, env.entities, 12, 1)
    assert env.resolver is None
    assert env.event is env.last is None
    assert env.lock.changed_by is None


@pytest.mark.parametrize(
    "bad_entry",
    [{}, {"method": "unknown"}, mapping(user_id=True), mapping(person="sensor.bad")],
)
async def test_malformed_stored_mappings_are_ignored(hass, mapped_lock, bad_entry):
    env = await mapped_lock([bad_entry])
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    assert env.lock.changed_by == "Fingerprint 1"


@pytest.mark.parametrize(
    "bad_input,key",
    [
        (mapping("unsupported"), "method"),
        (mapping(person="person.missing"), "person_entity_id"),
        (mapping(person=None), "person_entity_id"),
    ],
)
async def test_mapping_form_rejects_invalid_method_and_person(hass, bad_input, key):
    flow, _ = options_flow(hass)
    result = await flow.async_step_lock_user_add(bad_input)
    assert key in result["errors"]


async def test_unmapped_ble_report_does_not_advance_local_replay_watermark(
    hass, mapped_lock
):
    env = await mapped_lock()
    report(env.device, env.coordinator, env.entities, 12, 1)
    report(env.device, env.coordinator, env.entities, 19, 0, timestamp=1002)
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=1001)
    await hass.async_block_till_done()
    assert env.device.last_unlock["timestamp"] == 1001
    assert env.event.state_attributes["method"] == "fingerprint"
    assert env.event.state_attributes["person_name"] == "Jorge"


async def test_first_report_from_old_history_has_no_person_context(hass, mapped_lock):
    env = await mapped_lock()
    report(env.device, env.coordinator, env.entities, 12, 1, timestamp=900)
    await hass.async_block_till_done()
    assert env.lock.state == LockState.LOCKED
    assert env.event.state is None
    assert env.last.native_value == "Fingerprint 1"
    assert hass.states.get(env.last.entity_id).context.user_id is None


async def test_rapid_reports_do_not_replay_queued_sensor_notifications(
    hass, mapped_lock
):
    env = await mapped_lock([mapping(), mapping("password", person="person.ana")])
    changes = []
    await hass.async_block_till_done()
    hass.bus.async_listen("state_changed", changes.append)
    report(env.device, env.coordinator, env.entities, 12, 1)
    report(env.device, env.coordinator, env.entities, 13, 1, timestamp=1001)
    await hass.async_block_till_done()
    assert env.last.native_value == "Ana · Password 1"
    last_changes = [
        event for event in changes if event.data["entity_id"] == env.last.entity_id
    ]
    assert [event.data["new_state"].state for event in last_changes] == [
        "Jorge · Fingerprint 1",
        "Ana · Password 1",
    ]
    assert all(
        hass.states.get(entity.entity_id).context.user_id == env.other.id
        for entity in env.entities
    )


@pytest.mark.parametrize(
    "order", [("last", "event", "lock"), ("lock", "event", "last")]
)
async def test_batch_with_newer_unmapped_method_does_not_inherit_person(
    hass, mapped_lock, order
):
    env = await mapped_lock()
    for dp_id, timestamp in ((12, 1000), (13, 1001)):
        env.device.datapoints._update_from_device(
            dp_id, timestamp, 0, sensor.TuyaBLEDataPointType.DT_VALUE, 1
        )
    env.coordinator.last_updates = [
        env.device.datapoints[12],
        env.device.datapoints[13],
    ]
    with (
        patch("time.time", return_value=1001),
        patch("time.monotonic", return_value=500),
    ):
        for name in order:
            getattr(env, name)._handle_coordinator_update()
    await hass.async_block_till_done()
    assert env.lock.changed_by == env.last.native_value == "Password 1"
    assert env.event.state_attributes["method"] == "password"
    assert "person_name" not in env.event.state_attributes
    assert all(
        hass.states.get(entity.entity_id).context.user_id is None
        for entity in env.entities
    )


@pytest.mark.parametrize(
    "product,mappings,enabled",
    [
        ("dappsdq4", [mapping()], True),
        ("dappsdq4", [], False),
        ("djrqe0q6", [mapping()], False),
    ],
)
async def test_entry_setup_and_unload_only_install_configured_capable_resolver(
    hass, make_entities, product, mappings, enabled
):
    device, _, _, _, _ = await make_entities(product)
    device.initialize = AsyncMock()
    device.update = AsyncMock()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"address": "02:00:00:00:00:02"},
        options={
            CONF_PRODUCT_ID: product,
            CONF_CATEGORY: "jtmspro",
            CONF_LOCK_USERS: mappings,
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.tuya_ble.bluetooth.async_ble_device_from_address",
            return_value=Mock(),
        ),
        patch("custom_components.tuya_ble.TuyaBLEDevice", return_value=device),
        patch(
            "custom_components.tuya_ble.bluetooth.async_register_callback",
            return_value=Mock(),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id].coordinator
    resolver = getattr(coordinator, "lock_activity", None)
    assert isinstance(resolver, LockActivity) is enabled
    await entry._async_process_on_unload(hass)
    if enabled:
        assert not resolver._unsubscribers


@pytest.mark.parametrize("mappings", [[], [mapping()]])
async def test_lock_user_changes_reload_capable_entry(hass, mappings):
    options = {
        CONF_PRODUCT_ID: "dappsdq4",
        CONF_CATEGORY: "jtmspro",
        CONF_LOCK_USERS: mappings,
    }
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    previous = {**options, CONF_LOCK_USERS: [] if mappings else [mapping()]}
    hass.data[DOMAIN] = {
        entry.entry_id: SimpleNamespace(
            title=entry.title,
            device=SimpleNamespace(
                product_id="dappsdq4",
                category="jtmspro",
                keep_connection=True,
                idle_disconnect_delay=30,
            ),
            manager=SimpleNamespace(data=previous),
        )
    }
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as reload_entry:
        await _async_update_listener(hass, entry)
    reload_entry.assert_awaited_once_with(entry.entry_id)


def options_flow(hass, mappings=(), product="dappsdq4", category="jtmspro"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PRODUCT_ID: product,
            CONF_CATEGORY: category,
            CONF_LOCK_USERS: list(mappings),
            "preserved": True,
        },
    )
    flow = TuyaBLEOptionsFlow(entry)
    flow.hass = hass
    return flow, entry


@pytest.mark.parametrize(
    "product,category,supported",
    [
        ("dappsdq4", "jtmspro", True),
        ("djrqe0q6", "jtmspro", False),
        ("0qxp5u7s", "ms", False),
        ("dappsdq4", "wxkg", False),
    ],
)
async def test_lock_users_menu_is_capability_gated(hass, product, category, supported):
    flow, _ = options_flow(hass, product=product, category=category)
    result = await flow.async_step_init()
    assert ("lock_users" in result["menu_options"]) is supported
    result = await flow.async_step_lock_users()
    if not supported:
        assert result["type"] == "abort"
        assert (await flow.async_step_lock_user_add(mapping()))["type"] == "abort"
    else:
        assert result["menu_options"] == ["lock_user_add"]


async def test_lock_users_add_edit_delete_and_duplicate_validation(hass):
    hass.states.async_set("person.jorge", "home", {"friendly_name": "Jorge"})
    hass.states.async_set("person.ana", "home", {"friendly_name": "Ana"})
    flow, entry = options_flow(hass)
    form = await flow.async_step_lock_user_add()
    assert form["data_schema"].schema["person_entity_id"].config["domain"] == ["person"]
    result = await flow.async_step_lock_user_add(form["data_schema"](mapping()))
    assert result["data"]["preserved"]
    assert result["data"][CONF_LOCK_USERS] == [mapping()]
    assert entry.options[CONF_LOCK_USERS] == []
    flow, _ = options_flow(hass, [mapping()])
    assert len((await flow.async_step_lock_users())["menu_options"]) == 3
    duplicate = await flow.async_step_lock_user_add(mapping(person="person.ana"))
    assert duplicate["errors"]["base"] == "duplicate_lock_user"
    different_method = await flow.async_step_lock_user_add(
        mapping("password", person="person.ana")
    )
    assert len(different_method["data"][CONF_LOCK_USERS]) == 2
    assert (await flow.async_step_lock_user_edit({"mapping": "fingerprint:1"}))[
        "step_id"
    ] == "lock_user_update"
    edited = await flow.async_step_lock_user_update(
        mapping("password", 1, "person.ana")
    )
    assert edited["data"][CONF_LOCK_USERS] == [mapping("password", 1, "person.ana")]
    flow, _ = options_flow(hass, [mapping()])
    deleted = await flow.async_step_lock_user_delete({"mapping": "fingerprint:1"})
    assert deleted["data"][CONF_LOCK_USERS] == []


@pytest.mark.parametrize("number", [-1, 1000, 1.5, True, "1"])
async def test_lock_user_ids_are_whole_numbers_between_zero_and_999(hass, number):
    hass.states.async_set("person.jorge", "home")
    flow, _ = options_flow(hass)
    result = await flow.async_step_lock_user_add(mapping(user_id=number))
    assert result["errors"]["user_id"] == "invalid_lock_user_id"


@pytest.mark.parametrize("number", [0, 999])
async def test_lock_user_id_bounds_are_accepted(hass, number):
    hass.states.async_set("person.jorge", "home")
    flow, _ = options_flow(hass)
    result = await flow.async_step_lock_user_add(mapping(user_id=number))
    assert result["data"][CONF_LOCK_USERS][0]["user_id"] == number


def test_all_lock_user_strings_exist_in_all_languages():
    root = Path(__file__).parents[1] / "custom_components" / "tuya_ble"
    for path in [root / "strings.json", *(root / "translations").glob("*.json")]:
        options = json.loads(path.read_text())["options"]
        assert options["step"]["init"]["menu_options"]["lock_users"]
        for step in (
            "lock_users",
            "lock_user_add",
            "lock_user_edit",
            "lock_user_update",
            "lock_user_delete",
        ):
            assert options["step"][step]["title"]
        assert options["error"]["duplicate_lock_user"]


async def test_diagnostics_redact_person_credential_associations(hass):
    from custom_components.tuya_ble.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain=DOMAIN, options={CONF_LOCK_USERS: [mapping()]})
    with patch(
        "custom_components.tuya_ble.diagnostics.bluetooth.async_scanner_count",
        return_value=0,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["entry"]["options"][CONF_LOCK_USERS] == "**REDACTED**"
