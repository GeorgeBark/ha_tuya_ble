"""Tests for the jtmspro/dappsdq4 smart lock."""

import base64
import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble.cloud import extract_ble_unlock_check
from custom_components.tuya_ble.lock import build_timestamped_unlock_command
from custom_components.tuya_ble.tuya_ble.security import (
    decode_timestamped_ble_unlock_check,
)


CHECK_PAYLOAD = b"\x00\x01\xff\xff12345678\x01\x01\x02\x03\x04\x00\x00"
CHECK_VALUE = base64.b64encode(CHECK_PAYLOAD).decode()


def test_lock_activity_unlock_check_validation_and_command() -> None:
    """The cloud secret is validated and produces the documented DP71 payload."""
    assert decode_timestamped_ble_unlock_check(CHECK_VALUE) == CHECK_PAYLOAD
    assert build_timestamped_unlock_command(CHECK_VALUE, 0x6AD8EB64) == bytes.fromhex(
        "ffff00013132333435363738016ad8eb640001"
    )

    with pytest.raises(ValueError, match="invalid timestamped lock format"):
        decode_timestamped_ble_unlock_check(base64.b64encode(b"invalid").decode())


def test_lock_activity_unlock_check_is_extracted_from_status() -> None:
    """Only the validated BLE unlock check is selected from cloud status."""
    status = [
        {"code": "residual_electricity", "value": 28},
        {"code": "ble_unlock_check", "value": CHECK_VALUE},
    ]
    assert extract_ble_unlock_check(status) == CHECK_VALUE
    assert (
        extract_ble_unlock_check([{"code": "residual_electricity", "value": 28}])
        is None
    )


def test_lock_activity_device_info_payload_keeps_protocol_v4() -> None:
    """dappsdq4 sends its FD50 MTU payload without the legacy v2 framing."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.tuya_ble import TuyaBLEDevice

    device = TuyaBLEDevice(
        Mock(),
        BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50),
    )
    device._device_info = Mock(product_id="dappsdq4")
    device._characteristic_notify = "00000002-0000-1001-8001-00805f9b07d0"
    device._protocol_version = 4

    assert device._device_info_payload() == b"\x00\xf4"
    assert not device._requires_fd50_device_info_handshake()


async def test_lock_activity_protocol_v4_examples() -> None:
    """Decode the documented 0x8006/0x8007 reports and encode DP33 writes."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
    )
    from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode

    device = TuyaBLEDevice(
        Mock(),
        BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50),
    )
    device._protocol_version = 4
    device._send_response = AsyncMock()
    device._handle_command_or_response(
        1,
        0,
        TuyaBLECode.FUN_RECEIVE_DP_V4,
        bytes.fromhex("000000000e8000080200040000001c"),
    )
    device._handle_command_or_response(
        2,
        0,
        TuyaBLECode.FUN_RECEIVE_TIME_DP_V4,
        bytes.fromhex("000000000e0000016ad8e2c40c02000400000001"),
    )
    await asyncio.sleep(0)
    assert device.datapoints[8].value == 28
    assert device.datapoints[12].value == 1
    assert device.datapoints[12].timestamp == 0x6AD8E2C4

    device.datapoints.get_or_create(33, TuyaBLEDataPointType.DT_BOOL, False)
    device._get_seq_num = AsyncMock(return_value=0x0F)
    device._send_packet = AsyncMock()
    await device.set_multiple_values({33: True})
    device._send_packet.assert_awaited_once_with(
        TuyaBLECode.FUN_SENDER_DPS_V4,
        bytes.fromhex("000000000f2101000101"),
    )


async def test_lock_activity_lock_uses_dp33_and_open_requires_check(
    hass: HomeAssistant,
) -> None:
    """Lock/unlock work independently of the secret required by Open."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import (
        TuyaBLECoordinator,
        TuyaBLEProductInfo,
    )
    from custom_components.tuya_ble.lock import TuyaBLERestoreLock as TuyaBLELock
    from custom_components.tuya_ble.sensor import last_unlock_getter
    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
        TuyaBLEDeviceCredentials,
    )

    device = TuyaBLEDevice(
        Mock(),
        BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50),
    )
    device._device_info = TuyaBLEDeviceCredentials(
        uuid="uuid1234",
        local_key="0123456789abcdef",
        device_id="12345678901234567890",
        category="jtmspro",
        product_id="dappsdq4",
        device_name="8052F66",
        product_model="",
        product_name="8052F66",
        functions=[],
        status_range=[],
    )
    device._send_datapoints = AsyncMock()
    coordinator = TuyaBLECoordinator(hass, device)
    entity = TuyaBLELock(
        hass, coordinator, device, TuyaBLEProductInfo("8052F66 Smart Lock", lock=1)
    )
    entity.async_write_ha_state = Mock()
    assert entity.entity_description.name is None
    assert entity.is_locked is None
    await entity.async_unlock()
    assert device.datapoints[33].value is False
    assert entity.changed_by == "Home Assistant"
    assert device.last_unlock == {
        "method": "home_assistant",
        "user_id": None,
        "timestamp": None,
    }
    last_unlock_sensor = Mock(_device=device)
    last_unlock_getter(last_unlock_sensor)
    assert last_unlock_sensor._attr_native_value == "Home Assistant"
    last_unlock_before_lock = device.last_unlock.copy()
    await entity.async_lock()
    assert device.datapoints[33].value is True
    assert entity.is_locked
    assert entity.changed_by == "Home Assistant"
    assert device.last_unlock == last_unlock_before_lock

    with pytest.raises(HomeAssistantError, match="BLE unlock check is missing"):
        await entity.async_open()
    assert device._send_datapoints.await_count == 2

    device._device_info.ble_unlock_check = CHECK_VALUE
    await entity.async_open()
    last_unlock_getter(last_unlock_sensor)
    assert last_unlock_sensor._attr_native_value == "Home Assistant"
    device.datapoints._update_from_device(
        19, 0x6AD8E2C4, 0, TuyaBLEDataPointType.DT_VALUE, 0
    )
    coordinator.last_updates = [device.datapoints[19]]
    entity._handle_coordinator_update()
    assert entity.changed_by == "Home Assistant"

    timestamp = int(time.time())
    device.datapoints._update_from_device(
        12, timestamp, 0, TuyaBLEDataPointType.DT_VALUE, 1
    )
    coordinator.last_updates = [device.datapoints[12]]
    entity._handle_coordinator_update()
    assert not entity.is_locked
    assert entity.extra_state_attributes == {
        "last_unlock_method": "fingerprint",
        "last_unlock_user_id": 1,
        "last_unlock_time": timestamp,
    }
    assert entity.changed_by == "Fingerprint 1"
    assert device.last_unlock == {
        "method": "fingerprint",
        "user_id": 1,
        "timestamp": timestamp,
    }
    last_unlock_getter(last_unlock_sensor)
    assert last_unlock_sensor._attr_native_value == "Fingerprint 1"
    assert last_unlock_sensor._attr_extra_state_attributes == {
        "method": "fingerprint",
        "user_id": 1,
        "timestamp": timestamp,
    }

    entity._automatic_lock_previous = False

    device.datapoints._update_from_device(
        33, timestamp + 1, 0, TuyaBLEDataPointType.DT_BOOL, True
    )
    coordinator.last_updates = [device.datapoints[33]]
    entity._handle_coordinator_update()
    assert entity.is_locked
    assert entity.changed_by == "Fingerprint 1"
    if entity._lock_activity_relock_cancel:
        entity._lock_activity_relock_cancel()


def test_lock_activity_sensor_mappings_are_event_style() -> None:
    """Last-used and alarm sensors are disabled and restore their value."""
    from custom_components.tuya_ble.sensor import get_mapping_by_device

    device = Mock(category="jtmspro", product_id="dappsdq4")
    mappings = {mapping.dp_id: mapping for mapping in get_mapping_by_device(device)}

    assert mappings[8].description.suggested_display_precision == 0
    for dp_id in (12, 13, 14, 15, 19, 55, 62):
        description = mappings[dp_id].description
        assert description.native_unit_of_measurement is None
        assert description.state_class is None
        assert description.suggested_display_precision is None
        assert not description.entity_registry_enabled_default
        assert mappings[dp_id].restore_value
    assert mappings[21].restore_value
    assert not mappings[21].description.entity_registry_enabled_default
    last_unlock = mappings[-2]
    assert last_unlock.restore_value
    assert last_unlock.last_unlock
    assert last_unlock.description.entity_registry_enabled_default
    assert last_unlock.description.native_unit_of_measurement is None
    assert last_unlock.description.state_class is None

    from custom_components.tuya_ble.event import (
        get_mapping_by_device as get_event_mappings,
    )

    assert {mapping.dp_id for mapping in get_event_mappings(device)} == {12}


def test_lock_activity_unlock_formatting_is_shared() -> None:
    """Lock attribution and the aggregate sensor use one stable formatter."""
    from custom_components.tuya_ble.util import format_unlock_source

    assert format_unlock_source("fingerprint", 1) == "Fingerprint 1"
    assert format_unlock_source("home_assistant", None) == "Home Assistant"


async def test_lock_activity_last_unlock_sensor_restores_state(
    hass: HomeAssistant,
) -> None:
    """Last unlock restores its text and lock-event attributes after restart."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import (
        TuyaBLECoordinator,
        TuyaBLEProductInfo,
    )
    from custom_components.tuya_ble.sensor import (
        TuyaBLERestoreSensor as TuyaBLESensor,
        get_mapping_by_device,
    )
    from custom_components.tuya_ble.tuya_ble import TuyaBLEDevice

    device = TuyaBLEDevice(
        Mock(),
        BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50),
    )
    device._device_info = Mock(
        category="jtmspro", product_id="dappsdq4", device_id="device"
    )
    coordinator = TuyaBLECoordinator(hass, device)
    mapping = next(
        mapping for mapping in get_mapping_by_device(device) if mapping.last_unlock
    )
    sensor = TuyaBLESensor(
        hass, coordinator, device, TuyaBLEProductInfo("8052F66 Smart Lock"), mapping
    )
    sensor.async_get_last_state = AsyncMock(
        return_value=State(
            "sensor.last_unlock",
            "Fingerprint 1",
            {"method": "fingerprint", "user_id": 1, "timestamp": 1234},
        )
    )
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await sensor.async_added_to_hass()

    assert sensor.native_value == "Fingerprint 1"
    assert sensor.extra_state_attributes == {
        "method": "fingerprint",
        "user_id": 1,
        "timestamp": 1234,
    }
    await sensor.async_will_remove_from_hass()


async def test_lock_activity_ignores_old_unlock_events(hass: HomeAssistant) -> None:
    """Replayed reports do not reopen the lock or trigger its event entity."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import (
        TuyaBLECoordinator,
        TuyaBLEProductInfo,
    )
    from custom_components.tuya_ble.event import TuyaBLEEvent, get_mapping_by_device
    from custom_components.tuya_ble.lock import TuyaBLERestoreLock as TuyaBLELock
    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
        TuyaBLEDeviceCredentials,
    )

    device = TuyaBLEDevice(
        Mock(),
        BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50),
    )
    device._device_info = TuyaBLEDeviceCredentials(
        uuid="uuid1234",
        local_key="0123456789abcdef",
        device_id="device",
        category="jtmspro",
        product_id="dappsdq4",
        device_name="8052F66",
        product_model="",
        product_name="8052F66",
        functions=[],
        status_range=[],
    )
    coordinator = TuyaBLECoordinator(hass, device)
    product = TuyaBLEProductInfo("8052F66 Smart Lock", lock=1)
    lock = TuyaBLELock(hass, coordinator, device, product)
    lock.async_write_ha_state = Mock()
    event = TuyaBLEEvent(
        hass, coordinator, device, product, get_mapping_by_device(device)[0]
    )
    event._trigger_event = Mock()
    event.async_write_ha_state = Mock()
    device.datapoints._update_from_device(
        33, int(time.time()), 0, TuyaBLEDataPointType.DT_BOOL, True
    )

    device.datapoints._update_from_device(
        12, int(time.time()) - 16, 0, TuyaBLEDataPointType.DT_VALUE, 1
    )
    coordinator.last_updates = [device.datapoints[12]]
    lock._handle_coordinator_update()
    event._handle_coordinator_update()
    assert lock.is_locked
    event._trigger_event.assert_not_called()

    device.datapoints._update_from_device(
        12, int(time.time()), 0, TuyaBLEDataPointType.DT_VALUE, 1
    )
    coordinator.last_updates = [device.datapoints[12]]
    lock._handle_coordinator_update()
    event._handle_coordinator_update()
    assert not lock.is_locked
    event._trigger_event.assert_called_once_with(
        "fingerprint", {"method": "fingerprint", "user_id": 1}
    )
    device.datapoints._update_from_device(
        33, int(time.time()), 0, TuyaBLEDataPointType.DT_BOOL, True
    )
    coordinator.last_updates = [device.datapoints[33]]
    lock._handle_coordinator_update()
    assert not lock.is_locked
    lock._lock_activity_last_unlock.clear()
    device.last_unlock = None
    device.datapoints._update_from_device(
        36, 1000, 0, TuyaBLEDataPointType.DT_VALUE, 20
    )
    device.datapoints._update_from_device(12, 995, 0, TuyaBLEDataPointType.DT_VALUE, 1)
    coordinator.last_updates = [device.datapoints[12]]
    with (
        patch("time.time", return_value=1000),
        patch("time.monotonic", return_value=500),
    ):
        lock._handle_coordinator_update()
    assert lock._lock_activity_unlock_until == 515
    lock._lock_activity_relock_cancel()

    lock.async_get_last_state = AsyncMock(
        return_value=State(
            "lock.test",
            "locked",
            {
                "changed_by": "Fingerprint 1",
                "last_unlock_method": "fingerprint",
                "last_unlock_user_id": 1,
                "last_unlock_time": 995,
            },
        )
    )
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await lock.async_added_to_hass()
    assert lock.changed_by == "Fingerprint 1"
    assert lock.extra_state_attributes["last_unlock_time"] == 995
    assert lock.extra_state_attributes["last_unlock_method"] == "fingerprint"
    assert lock.extra_state_attributes["last_unlock_user_id"] == 1


async def test_lock_activity_alarm_and_battery_hardware_values(
    hass: HomeAssistant,
) -> None:
    """Invalid and replayed alarms are safe, while battery remains an integer."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import (
        TuyaBLECoordinator,
        TuyaBLEProductInfo,
    )
    from custom_components.tuya_ble.sensor import (
        TuyaBLERestoreSensor as TuyaBLESensor,
        get_mapping_by_device,
    )
    from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

    device = TuyaBLEDevice(
        Mock(),
        BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50),
    )
    device._device_info = Mock(
        category="jtmspro", product_id="dappsdq4", device_id="device"
    )
    coordinator = TuyaBLECoordinator(hass, device)
    mappings = {mapping.dp_id: mapping for mapping in get_mapping_by_device(device)}
    product = TuyaBLEProductInfo("8052F66 Smart Lock")
    alarm = TuyaBLESensor(hass, coordinator, device, product, mappings[21])
    alarm.async_write_ha_state = Mock()
    battery = TuyaBLESensor(hass, coordinator, device, product, mappings[8])
    battery.async_write_ha_state = Mock()

    alarm.async_get_last_state = AsyncMock(
        return_value=State("sensor.alarm_lock", "unknown")
    )
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await alarm.async_added_to_hass()
    assert alarm.native_value is None

    device.datapoints._update_from_device(21, 101, 0, TuyaBLEDataPointType.DT_ENUM, 15)
    alarm._handle_coordinator_update()
    assert alarm.native_value is None
    device.datapoints._update_from_device(21, 102, 0, TuyaBLEDataPointType.DT_ENUM, 0)
    alarm._handle_coordinator_update()
    assert alarm.native_value == "wrong_finger"
    device.datapoints._update_from_device(21, 101, 0, TuyaBLEDataPointType.DT_ENUM, 1)
    alarm._handle_coordinator_update()
    assert alarm.native_value == "wrong_finger"

    device.datapoints._update_from_device(8, 102, 0, TuyaBLEDataPointType.DT_VALUE, 22)
    battery._handle_coordinator_update()
    assert battery.native_value == 22
    assert isinstance(battery.native_value, int)
    last_used = TuyaBLESensor(hass, coordinator, device, product, mappings[12])
    last_used.async_write_ha_state = Mock()
    device.datapoints._update_from_device(12, 102, 0, TuyaBLEDataPointType.DT_VALUE, 1)
    last_used._handle_coordinator_update()
    assert last_used.native_value == 1
    assert isinstance(last_used.native_value, int)


@pytest.fixture
def activity_lock(hass: HomeAssistant):
    """Create a real lock and report helper with deterministic synthetic data."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import (
        TuyaBLECoordinator,
        TuyaBLEProductInfo,
    )
    from custom_components.tuya_ble.lock import TuyaBLERestoreLock as TuyaBLELock
    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
        TuyaBLEDeviceCredentials,
    )

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="test lock", address="02:00:00:00:00:01", details="")
    )
    device._device_info = TuyaBLEDeviceCredentials(
        uuid="testuuid00000001",
        local_key="0123456789abcdef",
        device_id="testlock000000001",
        category="jtmspro",
        product_id="dappsdq4",
        device_name="Test lock",
        product_model="",
        product_name="Test lock",
        functions=[],
        status_range=[],
    )
    coordinator = TuyaBLECoordinator(hass, device)
    lock = TuyaBLELock(
        hass, coordinator, device, TuyaBLEProductInfo("Test lock", lock=1)
    )
    lock.async_write_ha_state = Mock()

    def report(dp_id, value, timestamp=1000):
        dp_type = (
            TuyaBLEDataPointType.DT_BOOL
            if dp_id == 33
            else TuyaBLEDataPointType.DT_VALUE
        )
        device.datapoints._update_from_device(dp_id, timestamp, 0, dp_type, value)
        coordinator.last_updates = [device.datapoints[dp_id]]
        with (
            patch("time.time", return_value=1000),
            patch("time.monotonic", return_value=500),
        ):
            lock._handle_coordinator_update()

    yield lock, report
    if lock._lock_activity_relock_cancel:
        lock._lock_activity_relock_cancel()
    if lock._lock_activity_pending_cancel:
        lock._lock_activity_pending_cancel()


def test_reconnect_true_snapshot_keeps_unlock_window(activity_lock) -> None:
    """A true-to-true snapshot does not relock before the physical handle."""
    lock, report = activity_lock
    report(36, 20)
    report(33, True)
    report(12, 1)
    cancel_timer = lock._lock_activity_relock_cancel
    report(33, True)
    assert lock._lock_activity_relock_cancel is cancel_timer
    assert lock._lock_activity_unlock_until == 520
    with patch("time.monotonic", return_value=519.999):
        assert lock.is_locked is False
    with patch("time.monotonic", return_value=520):
        assert lock.is_locked is True
    assert lock.changed_by == "Fingerprint 1"


def test_passage_false_to_true_closes_window_preserving_source(activity_lock) -> None:
    """A real DP33 transition closes the window, not its local attribution."""
    lock, report = activity_lock
    report(36, 20)
    report(33, False)
    report(12, 1)
    with patch("time.monotonic", return_value=500):
        assert lock.is_locked is False
    report(33, True)
    assert lock._lock_activity_relock_cancel is None
    with patch("time.monotonic", return_value=500):
        assert lock.is_locked is True
    assert lock.changed_by == "Fingerprint 1"


@pytest.mark.parametrize("age,remaining", [(5, 15), (20, 0), (25, 0)])
def test_unlock_window_uses_device_timestamp(activity_lock, age, remaining) -> None:
    """Delivery delay reduces the window; expired reports never open it."""
    lock, report = activity_lock
    report(36, 20)
    report(33, True)
    report(12, 1, timestamp=1000 - age)
    with patch("time.monotonic", return_value=500):
        assert lock.is_locked is (remaining == 0)
    if remaining:
        assert lock._lock_activity_unlock_until == 500 + remaining
    else:
        assert lock._lock_activity_relock_cancel is None


async def test_lock_restores_attribution_and_ignores_older_report(
    activity_lock,
) -> None:
    """Restore the lock attributes and retain them across replayed reports."""
    lock, report = activity_lock
    attributes = {
        "changed_by": "Password 2",
        "last_unlock_method": "password",
        "last_unlock_user_id": 2,
        "last_unlock_time": 999,
    }
    lock.async_get_last_state = AsyncMock(
        return_value=State("lock.test", "locked", attributes)
    )
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await lock.async_added_to_hass()
    assert lock.changed_by == "Password 2"
    assert lock.extra_state_attributes == {
        key: value for key, value in attributes.items() if key != "changed_by"
    }
    report(33, True)
    report(12, 1, timestamp=998)
    assert lock.changed_by == "Password 2"
    assert lock.extra_state_attributes["last_unlock_time"] == 999


@pytest.mark.parametrize("dp_id", [12, 13, 14, 15, 19, 55, 62])
def test_last_used_native_value_is_integer(activity_lock, dp_id) -> None:
    """Every last-used ID is an integer, not a scaled floating-point value."""
    from custom_components.tuya_ble.sensor import (
        TuyaBLERestoreSensor as TuyaBLESensor,
        get_mapping_by_device,
    )

    lock, report = activity_lock
    mapping = next(m for m in get_mapping_by_device(lock.device) if m.dp_id == dp_id)
    sensor = TuyaBLESensor(
        lock._hass, lock._coordinator, lock.device, lock._product, mapping
    )
    sensor.async_write_ha_state = Mock()
    # Updating the transport only keeps this test independent of local unlock handling.
    from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType

    lock.device.datapoints._update_from_device(
        dp_id, 1000, 0, TuyaBLEDataPointType.DT_VALUE, 1
    )
    sensor._handle_coordinator_update()
    assert sensor.native_value == 1
    assert type(sensor.native_value) is int


async def test_lock_dp33_polarity_and_timestamped_open(hass: HomeAssistant) -> None:
    """DP33 polarity and DP71 Open are independent of other locks' commands."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import (
        TuyaBLECoordinator,
        TuyaBLEProductInfo,
    )
    from custom_components.tuya_ble.lock import TuyaBLELock
    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDevice,
        TuyaBLEDeviceCredentials,
    )

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="Test lock", address="02:00:00:00:00:01", details="")
    )
    device._device_info = TuyaBLEDeviceCredentials(
        uuid="testuuid00000001",
        local_key="0123456789abcdef",
        device_id="testlock000000001",
        category="jtmspro",
        product_id="dappsdq4",
        device_name="Test lock",
        product_model="",
        product_name="Test lock",
        functions=[],
        status_range=[],
    )
    device._send_datapoints = AsyncMock()
    lock = TuyaBLELock(
        hass,
        TuyaBLECoordinator(hass, device),
        device,
        TuyaBLEProductInfo("Test lock", lock=1),
    )
    assert lock.is_locked is None
    await lock.async_unlock()
    assert lock.is_locked is False
    assert device.datapoints[33].value is False
    await lock.async_lock()
    assert lock.is_locked is True
    assert device.datapoints[33].value is True
    with pytest.raises(HomeAssistantError, match="BLE unlock check is missing"):
        await lock.async_open()
    assert device._send_datapoints.await_count == 2
    device._device_info.ble_unlock_check = "invalid"
    with pytest.raises(HomeAssistantError, match="BLE unlock check is invalid"):
        await lock.async_open()
    assert device._send_datapoints.await_count == 2
    device._device_info.ble_unlock_check = CHECK_VALUE
    with patch("time.time", return_value=0x6AD8EB64):
        await lock.async_open()
    assert device.datapoints[71].value == bytes.fromhex(
        "ffff00013132333435363738016ad8eb640001"
    )


@pytest.mark.parametrize(
    "product_id,uses_restore", [("dappsdq4", True), ("djrqe0q6", False)]
)
async def test_restore_subclasses_selected_only_for_supported_product(
    hass: HomeAssistant, activity_lock, product_id, uses_restore
) -> None:
    """Ordinary lock/sensor entities keep their original classes and lifecycle."""
    from custom_components.tuya_ble.const import DOMAIN
    from custom_components.tuya_ble.lock import (
        TuyaBLELock,
        TuyaBLERestoreLock,
        async_setup_entry as setup_locks,
    )
    from custom_components.tuya_ble.sensor import (
        TuyaBLESensor,
        TuyaBLERestoreSensor,
        async_setup_entry as setup_sensors,
    )

    lock, _ = activity_lock
    lock.device._device_info.product_id = product_id
    entry = Mock(entry_id="test-entry")
    hass.data[DOMAIN] = {
        entry.entry_id: Mock(
            device=lock.device, coordinator=lock._coordinator, product=lock._product
        )
    }
    add_locks = Mock()
    await setup_locks(hass, entry, add_locks)
    selected = add_locks.call_args.args[0][0]
    assert type(selected) is (TuyaBLERestoreLock if uses_restore else TuyaBLELock)
    add_sensors = Mock()
    await setup_sensors(hass, entry, add_sensors)
    sensors = add_sensors.call_args.args[0]
    assert (
        any(isinstance(sensor, TuyaBLERestoreSensor) for sensor in sensors)
        is uses_restore
    )
    if not uses_restore:
        assert all(type(sensor) is TuyaBLESensor for sensor in sensors)
