"""Opt-in Open presentation without changing passage mode or legacy locks."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.lock import LockState
from homeassistant.core import State
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_ble import _async_update_listener, lock, util
from custom_components.tuya_ble.config_flow import TuyaBLEOptionsFlow
from custom_components.tuya_ble.const import (
    CONF_CATEGORY,
    CONF_PRODUCT_ID,
    CONF_KEEP_CONNECTION,
    CONF_IDLE_DISCONNECT_DELAY,
    CONF_REPORT_MOMENTARY_RELEASES_AS_OPEN as OPTION,
    DOMAIN,
)
from .test_dappsdq4 import CHECK_VALUE
from .test_lock_capabilities import make_entities, report


@pytest.fixture
async def make_open_lock(hass, make_entities):
    """Set up the lock using a real per-entry options dictionary."""
    created = []

    async def make(enabled=None, product_id="dappsdq4", category="jtmspro"):
        device, coordinator, original, sensors, events = await make_entities(
            product_id, category
        )
        options = {CONF_CATEGORY: category, CONF_PRODUCT_ID: product_id}
        if enabled is not None:
            options[OPTION] = enabled
        entry = MockConfigEntry(domain=DOMAIN, options=options, title="Test lock")
        hass.data[DOMAIN][entry.entry_id] = SimpleNamespace(
            device=device, coordinator=coordinator, product=original._product
        )
        add_entities = Mock()
        await lock.async_setup_entry(hass, entry, add_entities)
        entity = add_entities.call_args.args[0][0]
        entity.async_write_ha_state = Mock()
        created.append(entity)
        return device, coordinator, entity, sensors, events

    yield make
    for entity in created:
        await entity.async_will_remove_from_hass()


@pytest.mark.parametrize("enabled", [None, False, True])
@pytest.mark.parametrize("dp_id", [12, 13, 14, 15, 55, 62])
async def test_local_release_uses_original_window_in_both_modes(
    make_open_lock, enabled, dp_id
):
    device, coordinator, entity, _, events = await make_open_lock(enabled)
    events[0]._trigger_event = Mock()
    report(device, coordinator, [entity], 36, 20)
    report(device, coordinator, [entity], 33, True)
    report(device, coordinator, [entity, events[0]], dp_id, 7, timestamp=995)
    with patch("time.monotonic", return_value=500):
        assert entity.is_locked is False
        assert bool(entity.is_open) is bool(enabled)
        assert entity.state == (LockState.OPEN if enabled else LockState.UNLOCKED)
    assert entity._lock_activity_unlock_until == 515
    method, label = util.UNLOCK_METHODS[dp_id]
    assert entity.changed_by == f"{label} 7"
    assert entity.extra_state_attributes == {
        "last_unlock_method": method,
        "last_unlock_user_id": 7,
        "last_unlock_time": 995,
    }
    events[0]._trigger_event.assert_called_once_with(
        method, {"method": method, "user_id": 7}
    )
    with patch("time.monotonic", return_value=515):
        assert entity.is_locked is True
        assert not entity.is_open
        assert entity.state == LockState.LOCKED


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("delay", [None, 20])
async def test_ha_open_only_starts_open_window_after_success(
    make_open_lock, enabled, delay
):
    device, coordinator, entity, _, _ = await make_open_lock(enabled)
    device._device_info.ble_unlock_check = CHECK_VALUE
    report(device, coordinator, [entity], 33, True)
    if delay is not None:
        report(device, coordinator, [entity], 36, delay)
    clock = [500]

    async def send(*args, **kwargs):
        assert entity.state == LockState.LOCKED  # No optimistic Open before acceptance.
        clock[0] = 502

    device._send_datapoints.side_effect = send
    with patch("time.monotonic", side_effect=lambda: clock[0]):
        await entity.async_open()
        assert entity.state == (LockState.OPEN if enabled else LockState.LOCKED)
        assert entity.is_locked is (not enabled)
        assert entity.changed_by == "Home Assistant"
        assert device.last_unlock == {
            "method": "home_assistant",
            "user_id": None,
            "timestamp": None,
        }
        if enabled:
            # Acceptance latency does not extend the interval beyond send + DP36.
            assert entity._lock_activity_unlock_until == 500 + (delay or 5)
            clock[0] = 500 + (delay or 5)
            assert entity.state == LockState.LOCKED
        else:
            assert entity._lock_activity_relock_cancel is None


async def test_failed_ha_open_does_not_mark_open(make_open_lock):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    device._device_info.ble_unlock_check = CHECK_VALUE
    report(device, coordinator, [entity], 33, True)
    device._send_datapoints.side_effect = RuntimeError("Command rejected")
    with pytest.raises(RuntimeError, match="Command rejected"):
        await entity.async_open()
    assert entity.state == LockState.LOCKED
    assert not entity.is_open
    assert entity._lock_activity_relock_cancel is None


async def test_expired_ha_open_does_not_start_a_fresh_window(make_open_lock):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    device._device_info.ble_unlock_check = CHECK_VALUE
    report(device, coordinator, [entity], 33, True)
    clock = [500]

    async def send(*args, **kwargs):
        clock[0] = 510

    device._send_datapoints.side_effect = send
    with patch("time.monotonic", side_effect=lambda: clock[0]):
        await entity.async_open()
        assert entity.state == LockState.LOCKED
        assert entity._lock_activity_relock_cancel is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_passage_mode_and_false_to_true_transition(make_open_lock, enabled):
    device, coordinator, entity, _, _ = await make_open_lock(enabled)
    report(device, coordinator, [entity], 36, 20)
    report(device, coordinator, [entity], 33, True)
    report(device, coordinator, [entity], 12, 7)
    await entity.async_unlock()
    with patch("time.monotonic", return_value=500):
        assert entity.state == LockState.UNLOCKED
        assert not entity.is_open
        assert entity.is_locked is False
    report(device, coordinator, [entity], 33, True)
    with patch("time.monotonic", return_value=500):
        assert entity.state == LockState.LOCKED
        assert entity._lock_activity_relock_cancel is None


async def test_reconnect_snapshot_does_not_close_open_window(make_open_lock):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    report(device, coordinator, [entity], 33, True)
    report(device, coordinator, [entity], 12, 7)
    report(device, coordinator, [entity], 33, True)
    with patch("time.monotonic", return_value=500):
        assert entity.state == LockState.OPEN
    entity._lock_activity_relock_cancel()
    entity._lock_activity_relock(None)
    assert entity.state == LockState.LOCKED


@pytest.mark.parametrize("source", ["local", "ha"])
async def test_passage_mode_stays_unlocked_through_release_until_real_relock(
    make_open_lock, source
):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    device._device_info.ble_unlock_check = CHECK_VALUE
    await entity.async_unlock()
    assert entity.state == LockState.UNLOCKED
    with patch("time.monotonic", return_value=500):
        if source == "local":
            report(device, coordinator, [entity], 12, 7)
        else:
            await entity.async_open()
        assert entity.state == LockState.UNLOCKED
        assert entity.is_open is False
        assert entity.is_locked is False
    changed_by = entity.changed_by
    last_unlock = device.last_unlock.copy()
    # Neither the active release window nor its expiry may insert Open into
    # Unlock HA -> fingerprint -> DP33=true, even with a delayed mode report.
    for now in (519, 520, 522):
        with patch("time.monotonic", return_value=now):
            assert entity.state == LockState.UNLOCKED
            assert entity.is_open is False
    report(device, coordinator, [entity], 33, True, timestamp=1022)
    assert entity.state == LockState.LOCKED
    assert entity._lock_activity_relock_cancel is None
    assert entity.changed_by == changed_by
    assert device.last_unlock == last_unlock


@pytest.mark.parametrize(
    "lock_class", [lock.TuyaBLEActivityLock, lock.TuyaBLERestoreLock]
)
@pytest.mark.parametrize("enabled", [None, False, True])
async def test_open_option_is_a_constructor_argument(
    make_entities, lock_class, enabled
):
    device, coordinator, original, _, _ = await make_entities("dappsdq4")
    options = {} if enabled is None else {"report_momentary_releases_as_open": enabled}
    entity = lock_class(
        original._hass, coordinator, device, original._product, **options
    )
    entity.async_write_ha_state = Mock()
    try:
        report(device, coordinator, [entity], 33, True)
        report(device, coordinator, [entity], 12, 7)
        with patch("time.monotonic", return_value=500):
            assert entity.state == (LockState.OPEN if enabled else LockState.UNLOCKED)
    finally:
        await entity.async_will_remove_from_hass()


async def test_open_requires_confirmed_automatic_lock_mode(make_open_lock):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    report(device, coordinator, [entity], 12, 7)
    with patch("time.monotonic", return_value=500):
        assert entity.is_open is False
        assert entity.is_locked is None
        report(device, coordinator, [entity], 33, True)
        assert entity.state == LockState.OPEN


@pytest.mark.parametrize("command", ["async_lock", "async_unlock"])
async def test_explicit_mode_change_ends_open_without_resurrecting_window(
    make_open_lock, command
):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    report(device, coordinator, [entity], 33, True)
    report(device, coordinator, [entity], 12, 7)
    with patch("time.monotonic", return_value=500):
        assert entity.state == LockState.OPEN
        await getattr(entity, command)()
        assert entity.state == (
            LockState.LOCKED if command == "async_lock" else LockState.UNLOCKED
        )
        assert entity._lock_activity_relock_cancel is None
        await entity.async_lock()
        assert entity.state == LockState.LOCKED


async def test_restore_never_reopens_momentary_window(make_open_lock):
    device, coordinator, entity, _, _ = await make_open_lock(True)
    report(device, coordinator, [entity], 33, True)
    attributes = {
        "changed_by": "Fingerprint 7",
        "last_unlock_method": "fingerprint",
        "last_unlock_user_id": 7,
        "last_unlock_time": 995,
    }
    entity.async_get_last_state = AsyncMock(
        return_value=State(entity.entity_id, LockState.OPEN, attributes)
    )
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await entity.async_added_to_hass()
    assert entity.state == LockState.LOCKED
    assert entity.changed_by == "Fingerprint 7"
    assert entity.extra_state_attributes["last_unlock_time"] == 995
    assert entity._lock_activity_relock_cancel is None


@pytest.mark.parametrize(
    "product_id,category", [("djrqe0q6", "jtmspro"), ("0qxp5u7s", "ms")]
)
async def test_other_locks_ignore_even_a_manually_stored_option(
    make_open_lock, product_id, category
):
    device, coordinator, entity, _, _ = await make_open_lock(True, product_id, category)
    assert type(entity) is lock.TuyaBLELock
    original = entity.state
    report(device, coordinator, [entity], 12, 7)
    assert entity.state == original
    assert not entity.is_open


@pytest.mark.parametrize(
    "product_id,category,visible",
    [
        ("dappsdq4", "jtmspro", True),
        ("djrqe0q6", "jtmspro", False),
        ("0qxp5u7s", "ms", False),
        ("dappsdq4", "wxkg", False),
    ],
)
async def test_options_flow_visibility_and_default(hass, product_id, category, visible):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PRODUCT_ID: product_id,
            CONF_CATEGORY: category,
            "untouched": "keep",
        },
    )
    flow = TuyaBLEOptionsFlow(entry)
    flow.hass = hass
    form = await flow.async_step_settings()
    schema = form["data_schema"]
    assert (OPTION in schema.schema) is visible
    defaults = schema({})
    if visible:
        assert defaults[OPTION] is False
        defaults[OPTION] = True
    result = await flow.async_step_settings(defaults)
    assert result["data"]["untouched"] == "keep"
    assert result["data"].get(OPTION, False) is visible
    # Reopening the settings retains the saved selection.
    saved = MockConfigEntry(domain=DOMAIN, options=result["data"])
    form = await TuyaBLEOptionsFlow(saved).async_step_settings()
    assert form["data_schema"]({}).get(OPTION, False) is visible


@pytest.mark.parametrize(
    "product_id,changed,expected_reload",
    [
        ("dappsdq4", True, True),
        ("dappsdq4", False, False),
        ("djrqe0q6", True, False),
        ("0qxp5u7s", True, False),
    ],
)
async def test_open_option_changes_reload_only_capable_entries(
    hass, product_id, changed, expected_reload
):
    old_options = {
        CONF_PRODUCT_ID: product_id,
        CONF_CATEGORY: "ms" if product_id == "0qxp5u7s" else "jtmspro",
    }
    entry = MockConfigEntry(domain=DOMAIN, options={**old_options, OPTION: changed})
    hass.data[DOMAIN] = {
        entry.entry_id: SimpleNamespace(
            title=entry.title,
            device=SimpleNamespace(
                product_id=product_id,
                category=old_options[CONF_CATEGORY],
                keep_connection=True,
                idle_disconnect_delay=30,
            ),
            manager=SimpleNamespace(data=old_options),
        )
    }
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as reload_entry:
        await _async_update_listener(hass, entry)
    assert reload_entry.await_count == int(expected_reload)


async def test_disabling_open_option_also_reloads_entry(hass):
    old_options = {CONF_PRODUCT_ID: "dappsdq4", CONF_CATEGORY: "jtmspro", OPTION: True}
    entry = MockConfigEntry(domain=DOMAIN, options={**old_options, OPTION: False})
    hass.data[DOMAIN] = {
        entry.entry_id: SimpleNamespace(
            title=entry.title,
            device=SimpleNamespace(
                product_id="dappsdq4",
                category="jtmspro",
                keep_connection=True,
                idle_disconnect_delay=30,
            ),
            manager=SimpleNamespace(data=old_options),
        )
    }
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as reload_entry:
        await _async_update_listener(hass, entry)
    reload_entry.assert_awaited_once_with(entry.entry_id)


def test_open_option_label_and_description_exist_in_every_language():
    root = Path(__file__).parents[1] / "custom_components" / "tuya_ble"
    for path in [root / "strings.json", *(root / "translations").glob("*.json")]:
        settings = json.loads(path.read_text())["options"]["step"]["settings"]
        assert settings["data"][OPTION]
        assert settings["data_description"][OPTION]
