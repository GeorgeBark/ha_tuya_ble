"""Free lock-credential names, legacy people mappings and lock-first writes."""

from itertools import permutations
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.components.lock import LockState
from homeassistant.core import Context, State
import pytest

from custom_components.tuya_ble.const import CONF_LOCK_USERS
from custom_components.tuya_ble.lock import EVENT_LOCK
from .test_lock_capabilities import make_entities, report
from .test_lock_users import mapped_lock, mapping, options_flow


def named_mapping(name="Emergency Fob"):
    return {"method": "card", "user_id": 3, "name": name}


@pytest.mark.parametrize("order", list(permutations(("lock", "last", "event"))))
@pytest.mark.parametrize("open_state", [False, True])
async def test_named_release_writes_lock_first_with_shared_anonymous_context(
    hass, mapped_lock, order, open_state
):
    env = await mapped_lock([named_mapping()])
    env.lock._report_momentary_releases_as_open = open_state
    # A free name must not accidentally retain a preceding HA service's user.
    env.lock.async_set_context(Context(user_id=env.user.id))
    changes, bus = [], []
    await hass.async_block_till_done()
    hass.bus.async_listen("state_changed", changes.append)
    hass.bus.async_listen(EVENT_LOCK, bus.append)
    report(env.device, env.coordinator, [getattr(env, name) for name in order], 15, 3)
    await hass.async_block_till_done()
    assert [event.data["entity_id"] for event in changes] == [
        env.lock.entity_id,
        env.last.entity_id,
        env.event.entity_id,
    ]
    states = [hass.states.get(entity.entity_id) for entity in env.entities]
    assert states[0].state == (LockState.OPEN if open_state else LockState.UNLOCKED)
    assert states[0].context is states[1].context is states[2].context is bus[0].context
    assert states[0].context.user_id is None
    assert env.lock.changed_by == env.last.native_value == "Emergency Fob · Card 3"
    assert env.lock.extra_state_attributes["last_unlock_name"] == "Emergency Fob"
    assert env.device.last_unlock == {
        "method": "card",
        "user_id": 3,
        "timestamp": 1000,
        "name": "Emergency Fob",
    }
    assert env.event.state_attributes == {
        "event_type": "card",
        "method": "card",
        "user_id": 3,
        "name": "Emergency Fob",
    }
    assert bus[0].data == {
        "device_id": env.device.device_id,
        "entity_id": env.lock.entity_id,
        "method": "Card",
        "user_id": 3,
        "name": "Emergency Fob",
    }
    assert "person_entity_id" not in env.last.extra_state_attributes
    for entity in env.entities:
        assert entity._context.user_id is None


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"name": "   "},
        {"person_entity_id": "person.jorge", "name": "Emergency Fob"},
    ],
)
@pytest.mark.parametrize("editing", [False, True])
async def test_add_and_edit_require_exactly_one_identity(hass, identity, editing):
    hass.states.async_set("person.jorge", "home")
    flow, _ = options_flow(hass, [mapping()] if editing else [])
    if editing:
        await flow.async_step_lock_user_edit({"mapping": "fingerprint:1"})
    step = (
        flow.async_step_lock_user_update if editing else flow.async_step_lock_user_add
    )
    result = await step({"method": "fingerprint", "user_id": 1, **identity})
    assert result["errors"]["base"] == "lock_user_identity_required"


async def test_optional_fields_allow_switching_legacy_person_to_name_and_back(hass):
    hass.states.async_set("person.jorge", "home", {"friendly_name": "Jorge"})
    original = [mapping("card", 3), mapping("password", 1)]
    flow, entry = options_flow(hass, original)
    form = await flow.async_step_lock_user_edit({"mapping": "card:3"})
    assert not form["data_schema"].schema["person_entity_id"].config.get("multiple")
    # Suggested values must not be schema defaults: clearing a field must not
    # silently re-insert the old person/name in the submitted data.
    validated = form["data_schema"](named_mapping("  Emergency Fob  "))
    assert "person_entity_id" not in validated
    result = await flow.async_step_lock_user_update(validated)
    assert result["data"][CONF_LOCK_USERS] == [mapping("password", 1), named_mapping()]
    assert entry.options[CONF_LOCK_USERS] == original
    flow, _ = options_flow(hass, result["data"][CONF_LOCK_USERS])
    selection = await flow.async_step_lock_user_edit()
    assert (
        "Emergency Fob"
        in selection["data_schema"].schema["mapping"].container["card:3"]
    )
    form = await flow.async_step_lock_user_edit({"mapping": "card:3"})
    validated = form["data_schema"](mapping("card", 3))
    assert "name" not in validated
    result = await flow.async_step_lock_user_update(validated)
    assert result["data"][CONF_LOCK_USERS] == [
        mapping("password", 1),
        mapping("card", 3),
    ]


async def test_legacy_person_with_blank_name_is_preserved(hass):
    hass.states.async_set("person.jorge", "home")
    flow, _ = options_flow(hass)
    result = await flow.async_step_lock_user_add({**mapping(), "name": "  "})
    assert result["data"][CONF_LOCK_USERS] == [mapping()]


async def test_named_activity_restore_keeps_name_but_not_context_or_window(
    hass, mapped_lock
):
    env = await mapped_lock([named_mapping()])
    attributes = {
        "changed_by": "Emergency Fob · Card 3",
        "last_unlock_method": "card",
        "last_unlock_user_id": 3,
        "last_unlock_time": 999,
        "last_unlock_name": "Emergency Fob",
    }
    record = {"method": "card", "user_id": 3, "timestamp": 999, "name": "Emergency Fob"}
    env.lock.async_get_last_state = AsyncMock(
        return_value=State(env.lock.entity_id, "open", attributes)
    )
    env.last.async_get_last_state = AsyncMock(
        return_value=State(env.last.entity_id, "Emergency Fob · Card 3", record)
    )
    await env.last.async_will_remove_from_hass()
    with patch(
        "custom_components.tuya_ble.devices.TuyaBLEEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await env.lock.async_added_to_hass()
        await env.last.async_added_to_hass()
    assert env.lock.changed_by == env.last.native_value == "Emergency Fob · Card 3"
    assert env.device.last_unlock == record
    assert env.lock.state == LockState.LOCKED
    assert env.lock._lock_activity_relock_cancel is None
    assert env.last.extra_state_attributes == record


async def test_person_followed_by_named_release_does_not_reuse_user(hass, mapped_lock):
    env = await mapped_lock([mapping(), named_mapping()])
    report(env.device, env.coordinator, env.entities, 12, 1)
    await hass.async_block_till_done()
    previous = hass.states.get(env.lock.entity_id).context
    assert previous.user_id == env.user.id
    report(env.device, env.coordinator, env.entities, 15, 3, timestamp=1001)
    await hass.async_block_till_done()
    current = hass.states.get(env.lock.entity_id).context
    assert current is not previous and current.user_id is None
    assert all(
        hass.states.get(entity.entity_id).context is current for entity in env.entities
    )
    assert env.lock.changed_by == "Emergency Fob · Card 3"
    assert "person_entity_id" not in env.event.state_attributes


@pytest.mark.parametrize(
    "identity",
    [{}, {"name": " "}, {"person_entity_id": "person.jorge", "name": "Emergency Fob"}],
)
async def test_invalid_stored_identity_is_ignored_safely(hass, mapped_lock, identity):
    env = await mapped_lock([{"method": "card", "user_id": 3, **identity}])
    report(env.device, env.coordinator, env.entities, 15, 3)
    await hass.async_block_till_done()
    assert env.lock.changed_by == "Card 3"
    assert "name" not in env.device.last_unlock


def test_name_fields_and_identity_error_are_translated_everywhere():
    root = Path(__file__).parents[1] / "custom_components" / "tuya_ble"
    for path in [root / "strings.json", *(root / "translations").glob("*.json")]:
        options = json.loads(path.read_text())["options"]
        assert options["error"]["lock_user_identity_required"]
        for step in ("lock_user_add", "lock_user_update"):
            assert options["step"][step]["data"]["name"]
