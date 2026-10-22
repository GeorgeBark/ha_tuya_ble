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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, DPCode, CONF_REPORT_MOMENTARY_RELEASES_AS_OPEN
from . import util
from .util import (
    LAST_UNLOCK_UPDATED,
    HOME_ASSISTANT_METHOD,
    UNLOCK_METHODS,
    format_unlock_source,
)
from .devices import (
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    TuyaBLECoordinator,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice
from .tuya_ble.security import decode_timestamped_ble_unlock_check

# Pulido PLD_P130 Smart Lever Lock: momentary lever lock with free passage mode.
PULIDO_PLD_P130 = "0qxp5u7s"

DP71_RAW_UNLOCK_PRODUCT_IDS = frozenset({"2hmqh0ty", "djrqe0q6"})
DP33_AUTOMATIC_LOCK_PRODUCT_IDS = frozenset({"dappsdq4"})
DP71_TIMESTAMPED_UNLOCK_PRODUCT_IDS = frozenset({"dappsdq4"})
# BLE reports acknowledge HA Open; only the other methods open a local window.
LOCAL_UNLOCK_DPS = UNLOCK_METHODS.keys() - {19}
LOCK_COMMAND_PENDING_TIMEOUT = 5


def build_timestamped_unlock_command(ble_unlock_check: str, timestamp: int) -> bytes:
    """Build a DP71 command using the check code and the current timestamp."""
    check = decode_timestamped_ble_unlock_check(ble_unlock_check)
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
        lock_class = TuyaBLELock
        if data.device.category in util.LOCK_CATEGORIES:
            product_id = data.device.product_id
            if product_id in util.RESTORE_LOCK_ACTIVITY:
                lock_class = TuyaBLERestoreLock
            elif any(
                product_id in capability
                for capability in (
                    util.LOCAL_UNLOCK_WINDOW,
                    util.UNLOCK_ATTRIBUTION,
                    util.DEVICE_NAMED_LOCK,
                )
            ):
                lock_class = TuyaBLEActivityLock
        lock_options = {}
        if lock_class is not TuyaBLELock:
            lock_options["report_momentary_releases_as_open"] = (
                data.device.product_id in util.LOCAL_UNLOCK_WINDOW
                and entry.options.get(CONF_REPORT_MOMENTARY_RELEASES_AS_OPEN, False)
                is True
            )
        entity = lock_class(
            hass, data.coordinator, data.device, product, **lock_options
        )
        async_add_entities([entity])


class TuyaBLELock(TuyaBLEEntity, LockEntity):
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
            LockEntityDescription(key="lock", name=product.name),
        )
        self._attr_supported_features = LockEntityFeature.OPEN

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        if self._device.product_id in DP33_AUTOMATIC_LOCK_PRODUCT_IDS:
            automatic_lock = self._device.datapoints[33]
            return bool(automatic_lock.value) if automatic_lock else None
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
        if self._device.product_id in DP33_AUTOMATIC_LOCK_PRODUCT_IDS:
            automatic_lock = self._device.datapoints.get_or_create(
                33, TuyaBLEDataPointType.DT_BOOL, True
            )
            await automatic_lock.set_value(True)
            return
        if self._device.product_id == PULIDO_PLD_P130:
            # Pulido PLD_P130: DP46 (manual_lock) = True locks and also ends
            # free passage mode (DP33). DP46 = False does nothing on this lock.
            if manual_lock := self._device.datapoints.get_or_create(
                46, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await manual_lock.set_value(True)
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
        if self._device.product_id in DP33_AUTOMATIC_LOCK_PRODUCT_IDS:
            automatic_lock = self._device.datapoints.get_or_create(
                33, TuyaBLEDataPointType.DT_BOOL, False
            )
            await automatic_lock.set_value(False)
            return
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
        if self._device.product_id in DP71_TIMESTAMPED_UNLOCK_PRODUCT_IDS:
            check = self._device._device_info.ble_unlock_check
            if not check:
                raise HomeAssistantError(
                    "BLE unlock check is missing; refresh Tuya cloud credentials or "
                    "enter ble_unlock_check in the integration options."
                )
            try:
                command = build_timestamped_unlock_command(check, int(time.time()))
            except ValueError as err:
                raise HomeAssistantError(
                    "BLE unlock check is invalid; refresh Tuya cloud credentials or "
                    "enter ble_unlock_check in the integration options."
                ) from err
            # The app's undocumented 0x0023 preamble is intentionally omitted.
            ble_unlock = self._device.datapoints.get_or_create(
                71, TuyaBLEDataPointType.DT_RAW, command
            )
            await ble_unlock.set_value(command)
            return
        if self._device.product_id == PULIDO_PLD_P130:
            # Momentary BLE unlock (DP6); the lock relocks after ~3 seconds.
            if bluetooth_unlock := self._device.datapoints.get_or_create(
                6, TuyaBLEDataPointType.DT_BOOL, True
            ):
                await bluetooth_unlock.set_value(True)
            return
        await self.async_unlock(**kwargs)


class TuyaBLEActivityLock(TuyaBLELock):
    """Opt-in activity handling, independent of state restoration."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TuyaBLECoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        *,
        report_momentary_releases_as_open: bool = False,
    ) -> None:
        super().__init__(hass, coordinator, device, product)
        self._report_momentary_releases_as_open = report_momentary_releases_as_open
        if device.product_id in util.DEVICE_NAMED_LOCK:
            self.entity_description = LockEntityDescription(key="lock", name=None)
        self._lock_activity_unlock_until = 0.0
        self._lock_activity_relock_cancel: CALLBACK_TYPE | None = None
        self._lock_activity_pending_cancel: CALLBACK_TYPE | None = None
        self._lock_activity_pending_dps: set[int] = set()
        self._lock_activity_last_unlock: dict[str, Any] = {}
        self._lock_activity_last_report_time: float | None = None
        automatic_lock = device.datapoints[33]
        self._automatic_lock_previous = (
            bool(automatic_lock.value) if automatic_lock else None
        )

    def _is_recently_unlocked(self) -> bool:
        return time.monotonic() < self._lock_activity_unlock_until

    @callback
    def _lock_activity_relock(self, _: None) -> None:
        self._lock_activity_relock_cancel = None
        self._lock_activity_unlock_until = 0.0
        self.async_write_ha_state()

    @callback
    def _lock_activity_clear_pending(self, _: None = None) -> None:
        """Stop attributing later device reports to a Home Assistant command."""
        self._lock_activity_pending_dps.clear()
        self._device._lock_activity_pending_until = 0.0
        if self._lock_activity_pending_cancel:
            self._lock_activity_pending_cancel()
        self._lock_activity_pending_cancel = None

    def _lock_activity_note_ha_command(
        self, expected_dp: int, is_unlock: bool = False
    ) -> None:
        """Attribute an outgoing command until its report or a short timeout."""
        if self._device.product_id not in util.UNLOCK_ATTRIBUTION:
            return
        self._attr_changed_by = "Home Assistant"
        if is_unlock:
            self._lock_activity_set_last_unlock(HOME_ASSISTANT_METHOD, None, None)
        self._lock_activity_pending_dps.add(expected_dp)
        self._device._lock_activity_pending_until = (
            time.monotonic() + LOCK_COMMAND_PENDING_TIMEOUT
        )
        if self._lock_activity_pending_cancel:
            self._lock_activity_pending_cancel()
        self._lock_activity_pending_cancel = async_call_later(
            self._hass, LOCK_COMMAND_PENDING_TIMEOUT, self._lock_activity_clear_pending
        )
        self.async_write_ha_state()

    def _lock_activity_set_last_unlock(
        self, method: str, user_id: int | None, timestamp: float | None
    ) -> None:
        """Store and publish the most recent unlock source."""
        self._device.last_unlock = {
            "method": method,
            "user_id": user_id,
            "timestamp": timestamp,
        }
        self._attr_changed_by = format_unlock_source(method, user_id)
        self._hass.bus.async_fire(
            LAST_UNLOCK_UPDATED,
            {"device_id": self._device.device_id},
        )

    def _lock_activity_note_unlock(self, datapoint) -> None:
        """Hold the lock state open for the configured auto-lock interval."""
        auto_lock_time = self._device.datapoints[36]
        delay = int(auto_lock_time.value) if auto_lock_time else 5
        timestamp = (
            datapoint.timestamp
            if self._device.product_id in util.TIMESTAMPED_UNLOCK_REPORTS
            else time.time()
        )
        last_timestamp = self._lock_activity_last_unlock.get("last_unlock_time")
        if self._device.product_id not in util.UNLOCK_ATTRIBUTION:
            last_timestamp = self._lock_activity_last_report_time
        if last_timestamp is None and (
            last_unlock := getattr(self._device, "last_unlock", None)
        ):
            last_timestamp = last_unlock.get("timestamp")
        if (
            self._device.product_id in util.TIMESTAMPED_UNLOCK_REPORTS
            and isinstance(last_timestamp, (int, float))
            and timestamp <= last_timestamp
        ):
            return
        self._lock_activity_last_report_time = timestamp
        method_key, _ = UNLOCK_METHODS[datapoint.id]
        if self._device.product_id in util.UNLOCK_ATTRIBUTION:
            self._lock_activity_last_unlock = {
                "last_unlock_method": method_key,
                "last_unlock_user_id": datapoint.value,
                "last_unlock_time": timestamp,
            }
            self._lock_activity_set_last_unlock(method_key, datapoint.value, timestamp)
        if self._device.product_id not in util.LOCAL_UNLOCK_WINDOW:
            return
        remaining = timestamp + delay - time.time()
        if remaining <= 0:
            return
        self._lock_activity_unlock_until = time.monotonic() + remaining
        if self._lock_activity_relock_cancel:
            self._lock_activity_relock_cancel()
        self._lock_activity_relock_cancel = async_call_later(
            self._hass, remaining, self._lock_activity_relock
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the transient unlock state timer when unloading."""
        if self._lock_activity_relock_cancel:
            self._lock_activity_relock_cancel()
            self._lock_activity_relock_cancel = None
        if self._lock_activity_pending_cancel:
            self._lock_activity_pending_cancel()
            self._lock_activity_pending_cancel = None
        self._device._lock_activity_pending_until = 0.0
        self._device._lock_activity_ha_command_updates = None
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the last reported unlock details."""
        return self._lock_activity_last_unlock or None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Record local unlock reports before refreshing state."""
        if self._lock_activity_pending_dps and time.monotonic() < getattr(
            self._device, "_lock_activity_pending_until", 0.0
        ):
            # Keep the batch classification after the acknowledgement clears
            # pending state, regardless of lock/event listener ordering.
            self._device._lock_activity_ha_command_updates = (
                self._coordinator.last_updates
            )
        for datapoint in self._coordinator.last_updates or []:
            if datapoint.id in self._lock_activity_pending_dps:
                self._lock_activity_clear_pending()
            if (
                self._device.product_id in util.LOCAL_UNLOCK_WINDOW
                and self._device.product_id in DP33_AUTOMATIC_LOCK_PRODUCT_IDS
                and datapoint.id == 33
            ):
                if self._automatic_lock_previous is False and datapoint.value:
                    self._lock_activity_unlock_until = 0.0
                    if self._lock_activity_relock_cancel:
                        self._lock_activity_relock_cancel()
                        self._lock_activity_relock_cancel = None
                self._automatic_lock_previous = bool(datapoint.value)
            if datapoint.id in LOCAL_UNLOCK_DPS and (
                self._device.product_id in util.LOCAL_UNLOCK_WINDOW
                or self._device.product_id in util.UNLOCK_ATTRIBUTION
            ):
                self._lock_activity_note_unlock(datapoint)
        super()._handle_coordinator_update()

    @property
    def is_open(self) -> bool | None:
        """Optionally distinguish a brief handle release from passage mode."""
        if not self._use_open_state:
            return super().is_open
        return super().is_locked is True and self._is_recently_unlocked()

    @property
    def _use_open_state(self) -> bool:
        return (
            self._device.product_id in util.LOCAL_UNLOCK_WINDOW
            and self._report_momentary_releases_as_open
        )

    @property
    def is_locked(self) -> bool | None:
        """Combine automatic-lock mode with the remaining local-unlock window."""
        if self.is_open:
            return False
        locked = super().is_locked
        if self._device.product_id not in util.LOCAL_UNLOCK_WINDOW:
            return locked
        return (
            locked and not self._is_recently_unlocked() if locked is not None else None
        )

    async def async_lock(self, **kwargs: Any) -> None:
        """Attribute the command without replacing Last unlock."""
        self._lock_activity_note_ha_command(33)
        await super().async_lock(**kwargs)
        self._automatic_lock_previous = True
        if self._use_open_state:
            self._end_open_window()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Attribute an HA passage-mode unlock."""
        self._lock_activity_note_ha_command(33, is_unlock=True)
        await super().async_unlock(**kwargs)
        self._automatic_lock_previous = False
        if self._use_open_state:
            self._end_open_window()

    def _end_open_window(self) -> None:
        """Publish an explicit mode change without a lingering Open window."""
        if self._lock_activity_relock_cancel:
            self._lock_activity_relock_cancel()
        self._lock_activity_relock(None)

    async def async_open(self, **kwargs: Any) -> None:
        """Attribute an HA Open after validating its credential."""
        if self._device.product_id not in DP71_TIMESTAMPED_UNLOCK_PRODUCT_IDS:
            await super().async_open(**kwargs)
            return
        check = self._device._device_info.ble_unlock_check
        if not check:
            raise HomeAssistantError(
                "BLE unlock check is missing; refresh Tuya cloud credentials or "
                "enter ble_unlock_check in the integration options."
            )
        try:
            command = build_timestamped_unlock_command(check, int(time.time()))
        except ValueError as err:
            raise HomeAssistantError(
                "BLE unlock check is invalid; refresh Tuya cloud credentials or "
                "enter ble_unlock_check in the integration options."
            ) from err
        self._lock_activity_note_ha_command(19, is_unlock=True)
        ble_unlock = self._device.datapoints.get_or_create(
            71, TuyaBLEDataPointType.DT_RAW, command
        )
        open_until = 0.0
        if self._use_open_state:
            auto_lock_time = self._device.datapoints[36]
            delay = int(auto_lock_time.value) if auto_lock_time else 5
            open_until = time.monotonic() + delay
        await ble_unlock.set_value(command)
        if self._use_open_state and (remaining := open_until - time.monotonic()) > 0:
            self._lock_activity_unlock_until = open_until
            if self._lock_activity_relock_cancel:
                self._lock_activity_relock_cancel()
            self._lock_activity_relock_cancel = async_call_later(
                self._hass, remaining, self._lock_activity_relock
            )
            self.async_write_ha_state()
        return


class TuyaBLERestoreLock(TuyaBLEActivityLock, RestoreEntity):
    """Restore activity, never a transient unlocked state or a pending command."""

    async def async_added_to_hass(self) -> None:
        """Restore the most recent local unlock timestamp."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            attributes = last_state.attributes
            self._attr_changed_by = attributes.get("changed_by")
            timestamp = attributes.get("last_unlock_time")
            if isinstance(timestamp, (int, float)):
                self._lock_activity_last_unlock = {
                    "last_unlock_method": attributes.get("last_unlock_method"),
                    "last_unlock_user_id": attributes.get("last_unlock_user_id"),
                    "last_unlock_time": timestamp,
                }
                self._device.last_unlock = {
                    "method": attributes.get("last_unlock_method"),
                    "user_id": attributes.get("last_unlock_user_id"),
                    "timestamp": timestamp,
                }
