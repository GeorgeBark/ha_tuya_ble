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
from custom_components.tuya_ble.lock import build_dappsdq4_unlock_command
from custom_components.tuya_ble.tuya_ble.security import (
    decode_dappsdq4_ble_unlock_check,
)


CHECK_PAYLOAD = b"\x00\x01\xff\xff12345678\x01\x01\x02\x03\x04\x00\x00"
CHECK_VALUE = base64.b64encode(CHECK_PAYLOAD).decode()


def test_dappsdq4_unlock_check_validation_and_command() -> None:
    """The cloud secret is validated and produces the documented DP71 payload."""
    assert decode_dappsdq4_ble_unlock_check(CHECK_VALUE) == CHECK_PAYLOAD
    assert build_dappsdq4_unlock_command(CHECK_VALUE, 0x6AD8EB64) == bytes.fromhex(
        "ffff00013132333435363738016ad8eb640001"
    )

    with pytest.raises(ValueError, match="invalid dappsdq4 format"):
        decode_dappsdq4_ble_unlock_check(base64.b64encode(b"invalid").decode())


def test_dappsdq4_unlock_check_is_extracted_from_status() -> None:
    """Only the validated BLE unlock check is selected from cloud status."""
    status = [
        {"code": "residual_electricity", "value": 28},
        {"code": "ble_unlock_check", "value": CHECK_VALUE},
    ]
    assert extract_ble_unlock_check(status) == CHECK_VALUE
    assert extract_ble_unlock_check([{"code": "residual_electricity", "value": 28}]) is None


def test_dappsdq4_device_info_payload_keeps_protocol_v4() -> None:
    """dappsdq4 sends its FD50 MTU payload without the legacy v2 framing."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.tuya_ble import TuyaBLEDevice

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50)
    )
    device._device_info = Mock(product_id="dappsdq4")
    device._characteristic_notify = "00000002-0000-1001-8001-00805f9b07d0"
    device._protocol_version = 4

    assert device._device_info_payload() == b"\x00\xf4"
    assert not device._requires_fd50_device_info_handshake()


async def test_dappsdq4_protocol_v4_examples() -> None:
    """Decode the documented 0x8006/0x8007 reports and encode DP33 writes."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
    )
    from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50)
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


async def test_dappsdq4_lock_uses_dp33_and_open_requires_check(
    hass: HomeAssistant,
) -> None:
    """Lock/unlock work independently of the secret required by Open."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import TuyaBLECoordinator, TuyaBLEProductInfo
    from custom_components.tuya_ble.lock import TuyaBLELock
    from custom_components.tuya_ble.sensor import dappsdq4_last_unlock_getter
    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
        TuyaBLEDeviceCredentials,
    )

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50)
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
    assert device.dappsdq4_last_unlock == {
        "method": "home_assistant",
        "user_id": None,
        "timestamp": None,
    }
    last_unlock_sensor = Mock(_device=device)
    dappsdq4_last_unlock_getter(last_unlock_sensor)
    assert last_unlock_sensor._attr_native_value == "Home Assistant"
    last_unlock_before_lock = device.dappsdq4_last_unlock.copy()
    await entity.async_lock()
    assert device.datapoints[33].value is True
    assert entity.is_locked
    assert entity.changed_by == "Home Assistant"
    assert device.dappsdq4_last_unlock == last_unlock_before_lock

    with pytest.raises(HomeAssistantError, match="BLE unlock check is missing"):
        await entity.async_open()
    assert device._send_datapoints.await_count == 2

    device._device_info.ble_unlock_check = CHECK_VALUE
    await entity.async_open()
    dappsdq4_last_unlock_getter(last_unlock_sensor)
    assert last_unlock_sensor._attr_native_value == "Home Assistant"
    device.datapoints._update_from_device(
        19, 0x6AD8E2C4, 0, TuyaBLEDataPointType.DT_VALUE, 0
    )
    coordinator.last_updates = [device.datapoints[19]]
    entity._handle_coordinator_update()
    assert entity.changed_by == "Home Assistant"

    timestamp = int(time.time())
    device.datapoints._update_from_device(12, timestamp, 0, TuyaBLEDataPointType.DT_VALUE, 1)
    coordinator.last_updates = [device.datapoints[12]]
    entity._handle_coordinator_update()
    assert not entity.is_locked
    assert entity.extra_state_attributes == {
        "last_unlock_method": "fingerprint",
        "last_unlock_user_id": 1,
        "last_unlock_time": timestamp,
    }
    assert entity.changed_by == "Fingerprint 1"
    assert device.dappsdq4_last_unlock == {
        "method": "fingerprint",
        "user_id": 1,
        "timestamp": timestamp,
    }
    dappsdq4_last_unlock_getter(last_unlock_sensor)
    assert last_unlock_sensor._attr_native_value == "Fingerprint 1"
    assert last_unlock_sensor._attr_extra_state_attributes == {
        "method": "fingerprint",
        "user_id": 1,
        "timestamp": timestamp,
    }

    device.datapoints._update_from_device(
        33, timestamp + 1, 0, TuyaBLEDataPointType.DT_BOOL, True
    )
    coordinator.last_updates = [device.datapoints[33]]
    entity._handle_coordinator_update()
    assert entity.is_locked
    assert entity.changed_by == "Fingerprint 1"
    if entity._dappsdq4_relock_cancel:
        entity._dappsdq4_relock_cancel()


def test_dappsdq4_sensor_mappings_are_event_style() -> None:
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

    from custom_components.tuya_ble.event import get_mapping_by_device as get_event_mappings

    assert {mapping.dp_id for mapping in get_event_mappings(device)} == {12}


def test_dappsdq4_unlock_formatting_is_shared() -> None:
    """Lock attribution and the aggregate sensor use one stable formatter."""
    from custom_components.tuya_ble.dappsdq4 import format_dappsdq4_unlock

    assert format_dappsdq4_unlock("fingerprint", 1) == "Fingerprint 1"
    assert format_dappsdq4_unlock("home_assistant", None) == "Home Assistant"


async def test_dappsdq4_last_unlock_sensor_restores_state(
    hass: HomeAssistant,
) -> None:
    """Last unlock restores its text and lock-event attributes after restart."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import TuyaBLECoordinator, TuyaBLEProductInfo
    from custom_components.tuya_ble.sensor import TuyaBLESensor, get_mapping_by_device
    from custom_components.tuya_ble.tuya_ble import TuyaBLEDevice

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50)
    )
    device._device_info = Mock(
        category="jtmspro", product_id="dappsdq4", device_id="device"
    )
    coordinator = TuyaBLECoordinator(hass, device)
    mapping = next(
        mapping
        for mapping in get_mapping_by_device(device)
        if mapping.last_unlock
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


async def test_dappsdq4_ignores_old_unlock_events(hass: HomeAssistant) -> None:
    """Replayed reports do not reopen the lock or trigger its event entity."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import TuyaBLECoordinator, TuyaBLEProductInfo
    from custom_components.tuya_ble.event import TuyaBLEEvent, get_mapping_by_device
    from custom_components.tuya_ble.lock import TuyaBLELock
    from custom_components.tuya_ble.tuya_ble import (
        TuyaBLEDataPointType,
        TuyaBLEDevice,
        TuyaBLEDeviceCredentials,
    )

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50)
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
    lock._dappsdq4_relock_cancel()


async def test_dappsdq4_alarm_and_battery_hardware_values(hass: HomeAssistant) -> None:
    """Invalid and replayed alarms are safe, while battery remains an integer."""
    from bleak.backends.device import BLEDevice

    from custom_components.tuya_ble.devices import TuyaBLECoordinator, TuyaBLEProductInfo
    from custom_components.tuya_ble.sensor import TuyaBLESensor, get_mapping_by_device
    from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="lock", address="11:22:33:44:55:66", details="", rssi=-50)
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
