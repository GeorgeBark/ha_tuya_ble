from __future__ import annotations

from typing import Any
import time

from homeassistant.components.lock import (
    LockEntity,
    LockEntityFeature,
    LockEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, DPCode
from .dappsdq4 import (
    DAPP_SDQ4_LAST_UNLOCK_UPDATED,
    DAPP_SDQ4_HOME_ASSISTANT_METHOD,
    DAPP_SDQ4_UNLOCK_METHODS,
    format_dappsdq4_unlock,
)
from .devices import (
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    TuyaBLECoordinator,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice
from .tuya_ble.security import decode_dappsdq4_ble_unlock_check

# Pulido PLD_P130 Smart Lever Lock: momentary lever lock with free passage mode.
PULIDO_PLD_P130 = "0qxp5u7s"

DP71_RAW_UNLOCK_PRODUCT_IDS = frozenset({"2hmqh0ty", "djrqe0q6"})
DAPP_SDQ4_PRODUCT_IDS = frozenset({"dappsdq4"})
DAPP_SDQ4_UNLOCK_DPS = frozenset({12, 13, 14, 15, 19, 55, 62})
DAPP_SDQ4_LOCAL_UNLOCK_DPS = DAPP_SDQ4_UNLOCK_DPS - {19}
DAPP_SDQ4_PENDING_TIMEOUT = 5


def build_dappsdq4_unlock_command(ble_unlock_check: str, timestamp: int) -> bytes:
    """Build the timestamped dappsdq4 DP71 BLE unlock command."""
    check = decode_dappsdq4_ble_unlock_check(ble_unlock_check)
    return (
        b"\xff\xff\x00\x01"
        + check[4:12]
        + b"\x01"
        + timestamp.to_bytes(4, "big")
        + b"\x00\x01"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE sensors."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    product = get_device_product_info(data.device)
    if product and product.lock:
        async_add_entities([TuyaBLELock(hass, data.coordinator, data.device, product)])


class TuyaBLELock(TuyaBLEEntity, RestoreEntity, LockEntity):
    platform = Platform.LOCK

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TuyaBLECoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
    ) -> None:
        super().__init__(
            hass,
            coordinator,
            device,
            product,
            LockEntityDescription(
                key="lock",
                name=(
                    None if device.product_id in DAPP_SDQ4_PRODUCT_IDS else product.name
                ),
            ),
        )
        self._attr_supported_features = LockEntityFeature.OPEN
        self._dappsdq4_unlock_until = 0.0
        self._dappsdq4_relock_cancel: CALLBACK_TYPE | None = None
        self._dappsdq4_pending_cancel: CALLBACK_TYPE | None = None
        self._dappsdq4_pending_dps: set[int] = set()
        self._dappsdq4_last_unlock: dict[str, Any] = {}
        automatic_lock = device.datapoints[33]
        self._automatic_lock_previous = (
            bool(automatic_lock.value) if automatic_lock else None
        )

    async def async_added_to_hass(self) -> None:
        """Restore the most recent local unlock timestamp."""
        await super().async_added_to_hass()
        if self._device.product_id not in DAPP_SDQ4_PRODUCT_IDS:
            return
        if last_state := await self.async_get_last_state():
            attributes = last_state.attributes
            self._attr_changed_by = attributes.get("changed_by")
            timestamp = attributes.get("last_unlock_time")
            if isinstance(timestamp, (int, float)):
                self._dappsdq4_last_unlock = {
                    "last_unlock_method": attributes.get("last_unlock_method"),
                    "last_unlock_user_id": attributes.get("last_unlock_user_id"),
                    "last_unlock_time": timestamp,
                }
                self._device.dappsdq4_last_unlock = {
                    "method": attributes.get("last_unlock_method"),
                    "user_id": attributes.get("last_unlock_user_id"),
                    "timestamp": timestamp,
                }

    def _is_dappsdq4_recently_unlocked(self) -> bool:
        return time.monotonic() < self._dappsdq4_unlock_until

    @callback
    def _dappsdq4_relock(self, _: None) -> None:
        self._dappsdq4_relock_cancel = None
        self._dappsdq4_unlock_until = 0.0
        self.async_write_ha_state()

    @callback
    def _dappsdq4_clear_pending(self, _: None = None) -> None:
        """Stop attributing later device reports to a Home Assistant command."""
        self._dappsdq4_pending_dps.clear()
        if self._dappsdq4_pending_cancel:
            self._dappsdq4_pending_cancel()
        self._dappsdq4_pending_cancel = None

    def _dappsdq4_note_ha_command(
        self, expected_dp: int, is_unlock: bool = False
    ) -> None:
        """Attribute an outgoing command until its report or a short timeout."""
        self._attr_changed_by = "Home Assistant"
        if is_unlock:
            self._dappsdq4_set_last_unlock(DAPP_SDQ4_HOME_ASSISTANT_METHOD, None, None)
        self._dappsdq4_pending_dps.add(expected_dp)
        if self._dappsdq4_pending_cancel:
            self._dappsdq4_pending_cancel()
        self._dappsdq4_pending_cancel = async_call_later(
            self._hass, DAPP_SDQ4_PENDING_TIMEOUT, self._dappsdq4_clear_pending
        )
        self.async_write_ha_state()

    def _dappsdq4_set_last_unlock(
        self, method: str, user_id: int | None, timestamp: float | None
    ) -> None:
        """Store and publish the most recent unlock source."""
        self._device.dappsdq4_last_unlock = {
            "method": method,
            "user_id": user_id,
            "timestamp": timestamp,
        }
        self._attr_changed_by = format_dappsdq4_unlock(method, user_id)
        self._hass.bus.async_fire(
            DAPP_SDQ4_LAST_UNLOCK_UPDATED,
            {"device_id": self._device.device_id},
        )

    def _dappsdq4_note_unlock(self, datapoint) -> None:
        """Hold the lock state open for the configured auto-lock interval."""
        auto_lock_time = self._device.datapoints[36]
        delay = int(auto_lock_time.value) if auto_lock_time else 5
        timestamp = datapoint.timestamp
        last_timestamp = self._dappsdq4_last_unlock.get("last_unlock_time")
        if last_timestamp is None and (
            last_unlock := getattr(self._device, "dappsdq4_last_unlock", None)
        ):
            last_timestamp = last_unlock.get("timestamp")
        if isinstance(last_timestamp, (int, float)) and timestamp <= last_timestamp:
            return
        method_key, _ = DAPP_SDQ4_UNLOCK_METHODS[datapoint.id]
        self._dappsdq4_last_unlock = {
            "last_unlock_method": method_key,
            "last_unlock_user_id": datapoint.value,
            "last_unlock_time": timestamp,
        }
        self._dappsdq4_set_last_unlock(method_key, datapoint.value, timestamp)
        remaining = timestamp + delay - time.time()
        if remaining <= 0:
            return
        self._dappsdq4_unlock_until = time.monotonic() + remaining
        if self._dappsdq4_relock_cancel:
            self._dappsdq4_relock_cancel()
        self._dappsdq4_relock_cancel = async_call_later(
            self._hass, remaining, self._dappsdq4_relock
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the transient dappsdq4 state timer when unloading."""
        if self._dappsdq4_relock_cancel:
            self._dappsdq4_relock_cancel()
            self._dappsdq4_relock_cancel = None
        if self._dappsdq4_pending_cancel:
            self._dappsdq4_pending_cancel()
            self._dappsdq4_pending_cancel = None
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the last reported dappsdq4 unlock details."""
        return self._dappsdq4_last_unlock or None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Record dappsdq4 unlock reports before refreshing state."""
        if self._device.product_id in DAPP_SDQ4_PRODUCT_IDS:
            for datapoint in self._coordinator.last_updates or []:
                if datapoint.id in self._dappsdq4_pending_dps:
                    self._dappsdq4_clear_pending()
                if datapoint.id == 33:
                    if self._automatic_lock_previous is False and datapoint.value:
                        self._dappsdq4_unlock_until = 0.0
                        if self._dappsdq4_relock_cancel:
                            self._dappsdq4_relock_cancel()
                            self._dappsdq4_relock_cancel = None
                    self._automatic_lock_previous = bool(datapoint.value)
                if datapoint.id in DAPP_SDQ4_LOCAL_UNLOCK_DPS:
                    self._dappsdq4_note_unlock(datapoint)
        super()._handle_coordinator_update()

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        if self._device.product_id == PULIDO_PLD_P130:
            # "Unlocked" on this lock is held by free passage mode (DP33),
            # which the lock re-reports on every connect. DP47 (motor state)
            # is only sent on change, so after an HA restart it is absent and
            # must not default the entity to locked.
            free_passage = self._device.datapoints[33]
            motor_state = self._device.datapoints[47]
            if free_passage is None and motor_state is None:
                return None
            if free_passage is not None and free_passage.value:
                return False
            if motor_state is not None and motor_state.value:
                return False  # momentary Open (DP6) in progress
            return True
        if self._device.product_id in DAPP_SDQ4_PRODUCT_IDS:
            automatic_lock = self._device.datapoints[33]
            if automatic_lock is None:
                return None
            return (
                bool(automatic_lock.value) and not self._is_dappsdq4_recently_unlocked()
            )
        dpid = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
        if dpid is None:
            dpid = DPCode.LOCK_MOTOR_STATE
        if motor_state := self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, False
        ):
            return not motor_state.value
        return None

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        if self._device.product_id == PULIDO_PLD_P130:
            # Pulido PLD_P130: DP46 (manual_lock) = True locks and also ends
            # free passage mode (DP33). DP46 = False does nothing on this lock.
            if manual_lock := self._device.datapoints.get_or_create(
                46, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await manual_lock.set_value(True)
            return
        if self._device.product_id in DAPP_SDQ4_PRODUCT_IDS:
            self._dappsdq4_note_ha_command(33)
            automatic_lock = self._device.datapoints.get_or_create(
                33, TuyaBLEDataPointType.DT_BOOL, True
            )
            await automatic_lock.set_value(True)
            self._automatic_lock_previous = True
            return
        manual_lock_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if manual_lock_id is not None:
            if manual_lock := self._device.datapoints.get_or_create(
                manual_lock_id, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await manual_lock.set_value(True)
        elif self.find_dpid(DPCode.LOCK_MOTOR_STATE) is not None:
            if motor_state := self._device.datapoints.get_or_create(
                self.find_dpid(DPCode.LOCK_MOTOR_STATE),
                TuyaBLEDataPointType.DT_BOOL,
                False,
            ):
                await motor_state.set_value(False)
        elif self._device.product_id == "wgv4haro":
            # Guard Dog Security Smart Lock locks automatically, locking command is no-op
            # NOTE: Other momentary locks in category ms/jtmspro (like okkyfgfs, k53ok3u9,
            # sidhzylo, a6nttc41, stugc8dl, xicdxood, rlyxv7pe, oyqux5vv, hs21i377, kholoaew)
            # may also need updating in the future.
            return
        else:
            if manual_lock := self._device.datapoints.get_or_create(
                DPCode.MANUAL_LOCK, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await manual_lock.set_value(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        if self._device.product_id == PULIDO_PLD_P130:
            # Pulido PLD_P130 relocks by itself a few seconds after a BLE unlock,
            # so "unlocked" is held by turning on free passage mode (DP33).
            # The lock reports DP47 (motor state) on while it is held open, so
            # is_locked stays accurate. Use Open for a momentary unlock.
            if free_passage := self._device.datapoints.get_or_create(
                33, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await free_passage.set_value(True)
            return
        if self._device.product_id in DAPP_SDQ4_PRODUCT_IDS:
            self._dappsdq4_note_ha_command(33, is_unlock=True)
            automatic_lock = self._device.datapoints.get_or_create(
                33, TuyaBLEDataPointType.DT_BOOL, False
            )
            await automatic_lock.set_value(False)
            self._automatic_lock_previous = False
            return
        if self._device.product_id in DP71_RAW_UNLOCK_PRODUCT_IDS:
            # EL605A knob lock: unlock is a DP71 (ble_unlock_check, Raw)
            # trigger; a zero-length payload is enough. It also exposes
            # manual_lock (DP46), so without this branch it would fall into
            # the generic manual_lock path below and write DP46=False, which
            # does not move the bolt. Verified on hardware.
            if ble_unlock := self._device.datapoints.get_or_create(
                71,
                TuyaBLEDataPointType.DT_RAW,
                b"",
            ):
                await ble_unlock.set_value(b"")
            return
        manual_lock_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if manual_lock_id is not None:
            if manual_lock := self._device.datapoints.get_or_create(
                manual_lock_id, TuyaBLEDataPointType.DT_BOOL, False
            ):
                await manual_lock.set_value(False)
        elif self.find_dpid(DPCode.LOCK_MOTOR_STATE) is not None:
            if motor_state := self._device.datapoints.get_or_create(
                self.find_dpid(DPCode.LOCK_MOTOR_STATE),
                TuyaBLEDataPointType.DT_BOOL,
                True,
            ):
                await motor_state.set_value(True)
        elif self._device.product_id == "wgv4haro":
            # Guard Dog Security Smart Lock uses DP 6 for bluetooth unlock
            # NOTE: Other momentary locks (e.g. okkyfgfs, k53ok3u9, sidhzylo, a6nttc41 on DP 6;
            # or stugc8dl, xicdxood, rlyxv7pe, oyqux5vv, hs21i377, kholoaew on DP 71)
            # may also need updating in the future.
            if bluetooth_unlock := self._device.datapoints.get_or_create(
                6, TuyaBLEDataPointType.DT_BOOL, False
            ):
                await bluetooth_unlock.set_value(True)
        else:
            if manual_lock := self._device.datapoints.get_or_create(
                DPCode.MANUAL_LOCK, TuyaBLEDataPointType.DT_BOOL, False
            ):
                await manual_lock.set_value(False)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the covering."""
        if self._device.product_id == PULIDO_PLD_P130:
            # Momentary BLE unlock (DP6); the lock relocks after ~3 seconds.
            if bluetooth_unlock := self._device.datapoints.get_or_create(
                6, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await bluetooth_unlock.set_value(True)
            return
        if self._device.product_id in DAPP_SDQ4_PRODUCT_IDS:
            # Smart Life sends an undocumented 0x0023 preamble before DP71.
            # DP71 is the verified command; keep this omitted until hardware
            # shows that the preamble is required.
            check = self._device._device_info.ble_unlock_check
            if not check:
                raise HomeAssistantError(
                    "BLE unlock check is missing; refresh Tuya cloud credentials or "
                    "enter ble_unlock_check in the integration options."
                )
            try:
                command = build_dappsdq4_unlock_command(check, int(time.time()))
            except ValueError as err:
                raise HomeAssistantError(
                    "BLE unlock check is invalid; refresh Tuya cloud credentials or "
                    "enter ble_unlock_check in the integration options."
                ) from err
            self._dappsdq4_note_ha_command(19, is_unlock=True)
            ble_unlock = self._device.datapoints.get_or_create(
                71, TuyaBLEDataPointType.DT_RAW, command
            )
            await ble_unlock.set_value(command)
            return
        await self.async_unlock(**kwargs)
