"""Regression and independent opt-in coverage for lock activity capabilities."""

from unittest.mock import AsyncMock, Mock, patch

from bleak.backends.device import BLEDevice
from homeassistant.core import State
from homeassistant.helpers.restore_state import RestoreEntity
import pytest

from custom_components.tuya_ble import event, lock, sensor, util
from custom_components.tuya_ble.const import DOMAIN
from custom_components.tuya_ble.devices import TuyaBLECoordinator, TuyaBLEProductInfo
from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice


CAPABILITIES = (
    "TIMESTAMPED_UNLOCK_REPORTS",
    "LOCAL_UNLOCK_WINDOW",
    "UNLOCK_ATTRIBUTION",
    "RESTORE_LOCK_ACTIVITY",
    "INTEGER_UNLOCK_USER_IDS",
    "INTEGER_LOCK_BATTERY",
    "TIMESTAMPED_LOCK_ALARMS",
    "DEVICE_NAMED_LOCK",
)
FAKE_PRODUCT = "test_lock_capability"


@pytest.fixture
async def make_entities(hass, monkeypatch):
    """Use real datapoints and platform setup, with only cloud metadata mocked."""
    created = []

    async def make(product_id="djrqe0q6", category="jtmspro"):
        device = TuyaBLEDevice(
            Mock(), BLEDevice(name="Test lock", address="02:00:00:00:00:02", details="")
        )
        device._device_info = Mock(
            category=category,
            product_id=product_id,
            device_id="capability-device",
            functions=[],
        )
        device._send_datapoints = AsyncMock()
        coordinator = TuyaBLECoordinator(hass, device)
        product = TuyaBLEProductInfo("Test lock", lock=1)
        monkeypatch.setattr(lock, "get_device_product_info", lambda _: product)
        entry = Mock(entry_id="capability-entry")
        hass.data[DOMAIN] = {
            entry.entry_id: Mock(
                device=device, coordinator=coordinator, product=product
            )
        }
        add_lock, add_sensors, add_events = Mock(), Mock(), Mock()
        await lock.async_setup_entry(hass, entry, add_lock)
        await sensor.async_setup_entry(hass, entry, add_sensors)
        await event.async_setup_entry(hass, entry, add_events)
        entity = add_lock.call_args.args[0][0]
        sensors = {s._mapping.dp_id: s for s in add_sensors.call_args.args[0]}
        events = add_events.call_args.args[0]
        for item in [entity, *sensors.values(), *events]:
            item.async_write_ha_state = Mock()
            created.append(item)
        return device, coordinator, entity, sensors, events

    yield make
    for entity in created:
        await entity.async_will_remove_from_hass()


def report(device, coordinator, entities, dp_id, value, timestamp=1000):
    """Deliver a device report at a fixed wall and monotonic time."""
    dp_type = (
        TuyaBLEDataPointType.DT_BOOL
        if dp_id == 33
        else (
            TuyaBLEDataPointType.DT_ENUM
            if dp_id == 21
            else TuyaBLEDataPointType.DT_VALUE
        )
    )
    device.datapoints._update_from_device(dp_id, timestamp, 0, dp_type, value)
    coordinator.last_updates = [device.datapoints[dp_id]]
    with (
        patch("time.time", return_value=1000),
        patch("time.monotonic", return_value=500),
    ):
        for entity in entities:
            entity._handle_coordinator_update()


def test_capabilities_are_explicit_opt_ins():
    """No existing lock besides the hardware-validated model opts in."""
    for name in CAPABILITIES:
        assert getattr(util, name) == frozenset({"dappsdq4"})
    assert RestoreEntity not in lock.TuyaBLELock.__mro__
    assert RestoreEntity not in sensor.TuyaBLESensor.__mro__
    assert RestoreEntity not in lock.TuyaBLEActivityLock.__mro__
    assert RestoreEntity not in sensor.TuyaBLEActivitySensor.__mro__


@pytest.mark.parametrize("category", ["ms", "jtmspro"])
def test_all_other_lock_sensor_mappings_remain_unchanged(category):
    """All existing lock mappings retain getters, descriptions and lifecycle flags."""
    for product_id, original in sensor.mapping[category].products.items():
        if product_id == "dappsdq4":
            continue
        device = Mock(category=category, product_id=product_id)
        assert sensor.get_mapping_by_device(device) == original
        assert not any(
            m.restore_value or m.last_unlock or m.timestamped_alarm for m in original
        )


@pytest.mark.parametrize(
    "product_id,category,expected_dps,expected_events",
    [
        ("djrqe0q6", "jtmspro", {-1, 8, 12, 13, 15, 19, 21, 55, 62, 63}, [24]),
        ("0qxp5u7s", "ms", {-1, 8, 12, 19, 21}, []),
    ],
)
async def test_other_locks_keep_entities_attributes_and_state(
    make_entities, product_id, category, expected_dps, expected_events
):
    device, coordinator, entity, sensors, events = await make_entities(
        product_id, category
    )
    assert type(entity) is lock.TuyaBLELock
    assert entity.entity_description.name == "Test lock"
    assert set(sensors) == expected_dps
    assert [e._mapping.dp_id for e in events] == expected_events
    assert all(type(s) is sensor.TuyaBLESensor for s in sensors.values())
    assert not any(isinstance(e, RestoreEntity) for e in [entity, *sensors.values()])
    assert not hasattr(entity, "async_get_last_state")
    before = entity.is_locked
    report(device, coordinator, [entity, sensors[12]], 12, 7)
    assert entity.is_locked is before
    assert entity.changed_by is None
    assert entity.extra_state_attributes is None
    assert not hasattr(device, "last_unlock")
    assert type(sensors[12].native_value) is float
    assert sensors[12].extra_state_attributes is None
    # Old alarm reports continue to update the ordinary enum sensor.
    report(device, coordinator, [sensors[21]], 21, 0, timestamp=1000)
    report(device, coordinator, [sensors[21]], 21, 1, timestamp=900)
    assert sensors[21].native_value == "wrong_password"
    assert sensors[21].extra_state_attributes is None
    await entity.async_unlock()
    await entity.async_lock()
    assert entity.changed_by is None
    assert entity.extra_state_attributes is None


@pytest.mark.parametrize("category", ["ms", "jtmspro"])
@pytest.mark.parametrize("capability", CAPABILITIES)
async def test_one_capability_activates_only_its_feature(
    make_entities, monkeypatch, hass, capability, category
):
    """Adding a known-layout product to one set never enables other features."""
    monkeypatch.setattr(util, capability, getattr(util, capability) | {FAKE_PRODUCT})
    # A future device still needs its ordinary product/DP mapping, as in PR 1.
    monkeypatch.setitem(
        sensor.mapping[category].products,
        FAKE_PRODUCT,
        sensor.mapping["jtmspro"].products["djrqe0q6"],
    )
    device, coordinator, entity, sensors, events = await make_entities(
        FAKE_PRODUCT, category
    )
    attribution = capability == "UNLOCK_ATTRIBUTION"
    restore = capability == "RESTORE_LOCK_ACTIVITY"
    window = capability == "LOCAL_UNLOCK_WINDOW"
    assert isinstance(entity, RestoreEntity) is restore
    assert any(isinstance(s, RestoreEntity) for s in sensors.values()) is restore
    assert (entity.entity_description.name is None) is (
        capability == "DEVICE_NAMED_LOCK"
    )
    assert (sensor.LAST_UNLOCK_DP_ID in sensors) is attribution
    assert len(events) == int(attribution)
    assert sensors[12].entity_description.entity_registry_enabled_default
    assert sensors[12].entity_description.suggested_display_precision == 0
    assert sensors[21]._mapping.timestamped_alarm is (
        capability == "TIMESTAMPED_LOCK_ALARMS"
    )

    report(device, coordinator, [entity, sensors[12]], 12, 7, timestamp=900)
    with patch("time.monotonic", return_value=500):
        assert entity.is_locked is (not window)
    assert (entity.changed_by == "Fingerprint 7") is attribution
    assert bool(entity.extra_state_attributes) is attribution
    assert hasattr(device, "last_unlock") is attribution
    assert type(sensors[12].native_value) is (
        int if capability == "INTEGER_UNLOCK_USER_IDS" else float
    )
    report(device, coordinator, [sensors[8]], 8, 22)
    assert type(sensors[8].native_value) is (
        int if capability == "INTEGER_LOCK_BATTERY" else float
    )
    report(device, coordinator, [sensors[21]], 21, 0)
    report(device, coordinator, [sensors[21]], 21, 1, timestamp=900)
    assert sensors[21].native_value == (
        "wrong_finger" if capability == "TIMESTAMPED_LOCK_ALARMS" else "wrong_password"
    )
    assert bool(sensors[21].extra_state_attributes) is (
        capability == "TIMESTAMPED_LOCK_ALARMS"
    )

    if attribution:
        # Activity works without inheriting RestoreEntity or needing cloud Open credentials.
        last_unlock = sensors[sensor.LAST_UNLOCK_DP_ID]
        with patch.object(sensor.TuyaBLESensor, "async_added_to_hass", new=AsyncMock()):
            await last_unlock.async_added_to_hass()
        await entity.async_open()
        await hass.async_block_till_done()
        assert entity.changed_by == "Home Assistant"
        assert last_unlock.native_value == "Home Assistant"
        assert (
            device.datapoints[71] is None
        )  # No timestamped DP71 command for a fake model.
        events[0]._trigger_event = Mock()
        report(device, coordinator, events, 12, 8, timestamp=1)
        events[0]._trigger_event.assert_called_once_with(
            "fingerprint", {"method": "fingerprint", "user_id": 8}
        )
    if restore:
        entity.async_get_last_state = AsyncMock(
            return_value=State(
                "lock.test",
                "unlocked",
                {
                    "changed_by": "Password 2",
                    "last_unlock_method": "password",
                    "last_unlock_user_id": 2,
                    "last_unlock_time": 999,
                },
            )
        )
        sensors[12].async_get_last_state = AsyncMock(
            return_value=State("sensor.test", "2.0")
        )
        sensors[21].async_get_last_state = AsyncMock(
            return_value=State("sensor.alarm", "wrong_finger")
        )
        with patch(
            "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await entity.async_added_to_hass()
            await sensors[12].async_added_to_hass()
            await sensors[21].async_added_to_hass()
        assert entity.changed_by == "Password 2"
        assert (
            entity.is_locked is True
        )  # Restoring activity never restores an open window.
        assert sensors[12].native_value == 2.0
        assert type(sensors[12].native_value) is float
        assert sensors[21].native_value == "wrong_finger"
    # No copies or descriptions have leaked changes into the original shared mapping.
    original = sensor.mapping["jtmspro"].products["djrqe0q6"]
    assert all(m.getter is None and not m.restore_value for m in original)


async def test_timestamp_filter_can_be_added_without_other_features(
    make_entities, monkeypatch
):
    """Report age filtering is separate from attribution and local state windows."""
    monkeypatch.setattr(
        util, "UNLOCK_ATTRIBUTION", util.UNLOCK_ATTRIBUTION | {FAKE_PRODUCT}
    )
    device, coordinator, entity, sensors, events = await make_entities(FAKE_PRODUCT)
    unlock_event = events[0]
    unlock_event._trigger_event = Mock()
    report(device, coordinator, events, 12, 1, timestamp=900)
    assert unlock_event._trigger_event.call_count == 1
    monkeypatch.setattr(
        util,
        "TIMESTAMPED_UNLOCK_REPORTS",
        util.TIMESTAMPED_UNLOCK_REPORTS | {FAKE_PRODUCT},
    )
    report(device, coordinator, events, 12, 1, timestamp=900)
    assert unlock_event._trigger_event.call_count == 1
    report(device, coordinator, events, 12, 1, timestamp=1000)
    assert unlock_event._trigger_event.call_count == 2
    assert entity.is_locked is True
    assert not isinstance(entity, RestoreEntity)


async def test_window_timestamp_filter_does_not_require_attribution(
    make_entities, monkeypatch
):
    """Timestamped windows reject replay without exposing last-unlock attributes."""
    for capability in ("LOCAL_UNLOCK_WINDOW", "TIMESTAMPED_UNLOCK_REPORTS"):
        monkeypatch.setattr(
            util, capability, getattr(util, capability) | {FAKE_PRODUCT}
        )
    device, coordinator, entity, _, _ = await make_entities(FAKE_PRODUCT)
    report(device, coordinator, [entity], 36, 20)
    report(device, coordinator, [entity], 12, 1, timestamp=995)
    assert entity._lock_activity_unlock_until == 515
    timer = entity._lock_activity_relock_cancel
    report(device, coordinator, [entity], 12, 1, timestamp=994)
    assert entity._lock_activity_relock_cancel is timer
    assert entity._lock_activity_unlock_until == 515
    assert entity.extra_state_attributes is None
    assert entity.changed_by is None


async def test_capabilities_do_not_apply_outside_lock_categories(
    make_entities, monkeypatch
):
    for capability in CAPABILITIES:
        monkeypatch.setattr(
            util, capability, getattr(util, capability) | {FAKE_PRODUCT}
        )
    _, _, entity, sensors, events = await make_entities(FAKE_PRODUCT, "wxkg")
    assert type(entity) is lock.TuyaBLELock
    assert set(sensors) == {-1}
    assert events == []


@pytest.mark.parametrize("dp_id", list(util.UNLOCK_METHODS))
async def test_dappsdq4_event_methods_share_the_common_table(make_entities, dp_id):
    device, coordinator, entity, _, events = await make_entities("dappsdq4")
    unlock_event = events[0]
    unlock_event._trigger_event = Mock()
    report(device, coordinator, events, dp_id, 3)
    method, _ = util.UNLOCK_METHODS[dp_id]
    unlock_event._trigger_event.assert_called_once_with(
        method, {"method": method, "user_id": 3}
    )
    assert type(entity) is lock.TuyaBLERestoreLock


@pytest.mark.parametrize("event_first", [False, True])
async def test_ha_open_acknowledgement_is_not_an_unlock_event(
    make_entities, event_first
):
    """Suppress HA DP19 in either listener order, but not a later app unlock."""
    from tests.test_dappsdq4 import CHECK_VALUE

    device, coordinator, entity, _, events = await make_entities("dappsdq4")
    device._device_info.ble_unlock_check = CHECK_VALUE
    unlock_event = events[0]
    unlock_event._trigger_event = Mock()
    with patch("time.monotonic", return_value=500):
        await entity.async_open()
    entities = [unlock_event, entity] if event_first else [entity, unlock_event]
    report(device, coordinator, entities, 19, 0)
    unlock_event._trigger_event.assert_not_called()
    assert entity.changed_by == "Home Assistant"
    assert device.last_unlock["method"] == "home_assistant"
    assert not entity._lock_activity_pending_dps
    # A new batch must not inherit the previous batch's acknowledgement marker,
    # even if the same mutable datapoint object/timestamp/value is reused.
    report(device, coordinator, entities, 19, 0)
    unlock_event._trigger_event.assert_called_once_with(
        "ble", {"method": "ble", "user_id": 0}
    )


@pytest.mark.parametrize("pending", [False, True])
async def test_ble_unlock_without_pending_ha_command_emits_event(
    make_entities, pending
):
    """Smart Life reports are accepted with no command or after its timeout."""
    device, coordinator, entity, _, events = await make_entities("dappsdq4")
    unlock_event = events[0]
    unlock_event._trigger_event = Mock()
    if pending:
        # An expired deadline is enough even if the timeout callback is delayed.
        with patch("time.monotonic", return_value=494):
            await entity.async_unlock()
    report(device, coordinator, [entity, unlock_event], 19, 0)
    unlock_event._trigger_event.assert_called_once_with(
        "ble", {"method": "ble", "user_id": 0}
    )


async def test_pending_ha_command_does_not_suppress_local_unlock(make_entities):
    device, coordinator, entity, _, events = await make_entities("dappsdq4")
    unlock_event = events[0]
    unlock_event._trigger_event = Mock()
    with patch("time.monotonic", return_value=500):
        await entity.async_unlock()
    report(device, coordinator, [entity, unlock_event], 12, 7)
    unlock_event._trigger_event.assert_called_once_with(
        "fingerprint", {"method": "fingerprint", "user_id": 7}
    )


async def test_unlock_event_restores_and_refreshes_availability(
    make_entities,
):
    """Reconnect publishes the restored event without generating a new event."""
    from homeassistant.components.event import EventExtraStoredData

    device, coordinator, _, _, events = await make_entities("dappsdq4")
    unlock_event = events[0]
    assert type(unlock_event) is event.TuyaBLEUnlockEvent
    assert not unlock_event.available
    timestamp = "2026-10-22T10:00:00.000+00:00"
    unlock_event.async_get_last_state = AsyncMock(
        return_value=State(unlock_event.entity_id, timestamp)
    )
    unlock_event.async_get_last_event_data = AsyncMock(
        return_value=EventExtraStoredData(
            "fingerprint", {"method": "fingerprint", "user_id": 7}
        )
    )
    with patch.object(RestoreEntity, "async_internal_added_to_hass", new=AsyncMock()):
        await unlock_event.async_internal_added_to_hass()
    assert unlock_event.state == timestamp
    attributes = unlock_event.state_attributes.copy()
    unlock_event._trigger_event = Mock()
    coordinator.last_updates = None
    coordinator._disconnected = False
    unlock_event._handle_coordinator_update()
    assert unlock_event.available
    unlock_event.async_write_ha_state.assert_called_once()
    assert unlock_event.state == timestamp
    assert unlock_event.state_attributes == attributes
    unlock_event._trigger_event.assert_not_called()
    # The opposite transition must be published too, with no new unlock DP.
    coordinator._disconnected = True
    unlock_event._handle_coordinator_update()
    assert not unlock_event.available
    assert unlock_event.async_write_ha_state.call_count == 2
    unlock_event._handle_coordinator_update()
    assert unlock_event.async_write_ha_state.call_count == 2


async def test_doorbell_keeps_original_availability_update_behavior(make_entities):
    device, coordinator, _, _, events = await make_entities("djrqe0q6")
    doorbell = events[0]
    assert type(doorbell) is event.TuyaBLEEvent
    coordinator._disconnected = False
    coordinator.last_updates = None
    doorbell._handle_coordinator_update()
    doorbell.async_write_ha_state.assert_not_called()


@pytest.mark.parametrize("timestamp", [1000, 1000.5, None, "1000"])
async def test_alarm_restores_numeric_timestamp_attribute(make_entities, timestamp):
    device, coordinator, _, sensors, _ = await make_entities("dappsdq4")
    alarm = sensors[21]
    alarm.async_get_last_state = AsyncMock(
        return_value=State("sensor.alarm", "wrong_finger", {"timestamp": timestamp})
    )
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await alarm.async_added_to_hass()
    assert alarm.native_value == "wrong_finger"
    if isinstance(timestamp, (int, float)):
        assert alarm.extra_state_attributes == {"timestamp": timestamp}
        report(device, coordinator, [alarm], 21, 1, timestamp=999)
        assert alarm.native_value == "wrong_finger"
        assert alarm.extra_state_attributes == {"timestamp": timestamp}
    else:
        assert alarm.extra_state_attributes is None
    report(device, coordinator, [alarm], 21, 1, timestamp=1001)
    assert alarm.native_value == "wrong_password"
    assert alarm.extra_state_attributes == {"timestamp": 1001}
